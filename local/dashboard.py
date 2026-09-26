"""Coordinator deployment: the doctor-facing dashboard.

It never sees a raw record, feature, label, weight, or residual. It only ever holds:
  - wire items / refusals disclosed by the three sources (re-validated on arrival)
  - summed, pairwise-masked risk-model scores and aggregate metrics / dataset aggregates
Reviews run on Flower SuperGrid:
  live   the full federated AgentApp (`flwr run`), streamed step by step
  demo   Fast Demo Mode: the same steps against the local sources, with a prepared
         coordinator assessment, paced to finish in about 20 seconds
The engines `supergrid-ledger` and `local` remain for tests and scripting.
The verdict is always decided by code, and nothing is released without the doctor's decision.

Usage: uv run python -m local.dashboard --port 8100 --site gp=http://127.0.0.1:8101 ...
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from preventnet import verifier, vfl
from preventnet.agent_app import validate_fl_config
from preventnet.policy import FL_FACT, QUESTION_CATALOG, VALUE_LABELS, display_labels
from preventnet.wire import Ledger, Refusal, strip_for_wire

from .net import ApiError, JsonHandler, get_json, post_json
from .site import PATIENT_ID_RE

HERE = Path(__file__).parent
REPO = HERE.parent
ROLES = ("gp", "pharmacy", "lab")
PATIENTS = ("P001", "P002", "P003")
ENGINES = ("local", "supergrid", "supergrid-ledger")
RESULT_PREFIX = "PREVENTNET_RESULT "
# The dashboard stops waiting after this. Real full-mode runs have taken up to ~6 min.
SUPERGRID_TIMEOUT_S = 420
MAX_JOB_LOG_LINES = 400
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
RUN_ID_RE = re.compile(r"started run (\d+)")
PHASE_PREFIX = "PREVENTNET_PHASE "
FL_PREFIX = "PREVENTNET_FL "
DEMO_RESPONSES = json.loads((HERE / "demo_responses.json").read_text())
# (step, seconds after start). Fast Demo Mode walks through every step of a real run.
DEMO_TIMELINE = (
    ("submitted", 0.0), ("started", 1.5), ("installing", 3.5), ("coordinator_plan", 5.5),
    ("facet_gp", 7.5), ("facet_pharmacy", 9.5), ("facet_lab", 11.5), ("federated_training", 13.5),
    ("coordinator_reason", 15.5), ("verifier", 18.5), ("done", 20.0),
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class RemoteSite:
    """HTTP handle to a source deployment, with the same interface as vfl.LocalSite."""

    def __init__(self, url: str):
        self.url = url

    def reset(self) -> None:
        post_json(f"{self.url}/fl/reset", {})

    def alignment(self) -> str:
        return get_json(f"{self.url}/fl/alignment")["digest"]

    def summary(self) -> dict:
        return get_json(f"{self.url}/fl/summary")

    def forward(self, split: str, nonce: str, patient_id: str | None = None) -> list[float]:
        body = {"split": split, "nonce": nonce, **({"patient_id": patient_id} if patient_id else {})}
        return post_json(f"{self.url}/fl/forward", body)["masked_scores"]

    def residual(self, z: list[float], lr: float) -> dict:
        return post_json(f"{self.url}/fl/residual", {"z": z, "lr": lr})

    def evaluate(self, z: list[float]) -> dict:
        return post_json(f"{self.url}/fl/evaluate", {"z": z})


def local_summary(ledger: Ledger, patient_id: str) -> str:
    """Offline coordinator: deterministic, citation-carrying risk summary over ledger facts only."""
    by_field = {item.field: item for item in ledger.items}

    def val(field):
        return by_field[field].value if field in by_field else None

    def say(field, default):
        v = val(field)
        return VALUE_LABELS.get(v, v).lower() if v is not None else default

    def cite(*fields):
        return " ".join(f"[{by_field[f].id}]" for f in fields if f in by_field)

    reduced = val("egfr_band") in ("mildly_reduced", "moderately_reduced", "severely_reduced")
    contrast = val("contrast_imaging_planned_90d") is True
    nsaid = val("patient_reported_otc_nsaid") is True or val("nsaid_dispensed_90d") is True

    renal = []
    if reduced and contrast:
        renal.append(f"- Reduced kidney function (eGFR {say('egfr_band', 'unknown')}, {say('egfr_trend_24m', 'trend unknown')}) "
                     f"ahead of planned contrast imaging {cite('egfr_band', 'egfr_trend_24m', 'contrast_imaging_planned_90d')}")
    if val("metformin_active") is True and (contrast or reduced):
        renal.append("- Active metformin with " + ("planned contrast imaging " if contrast else "reduced kidney function ")
                     + cite("metformin_active", "contrast_imaging_planned_90d" if contrast else "egfr_band"))
    if nsaid and reduced:
        renal.append("- NSAID exposure alongside reduced kidney function "
                     + cite("patient_reported_otc_nsaid", "nsaid_dispensed_90d", "egfr_band"))
    if val("interacting_dispense_flag") is True:
        renal.append("- A drug-interaction alert was raised when medicines were dispensed " + cite("interacting_dispense_flag"))

    metabolic = []
    if val("hba1c_band") in ("prediabetic", "diabetic") or val(FL_FACT["field"]) in ("moderate", "high"):
        fl_part = (f"diabetes risk is {say(FL_FACT['field'], '')}" if val(FL_FACT["field"])
                   else "the risk model is not trained yet")
        metabolic.append(f"- Blood sugar {say('hba1c_band', 'not reported')}; {fl_part} "
                         + cite("hba1c_band", FL_FACT["field"]))

    risks = renal + metabolic
    if renal:
        rec = ("Book a medication and kidney safety review before the contrast scan: re-check kidney function "
               "and review NSAID and metformin use.")
    elif metabolic:
        rec = "Offer a diabetes prevention check: an HbA1c test and lifestyle advice."
    else:
        rec = "No preventive action needed now; continue routine care."
    confidence = "high" if len(risks) >= 2 else "medium" if risks else "low"
    risk_text = "\n".join(risks) if risks else "None found in the shared records."
    return f"RISKS\n{risk_text}\n\nRECOMMENDATION\n{rec}\n\nCONFIDENCE\n{confidence}"


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return json.dumps(v)  # a JSON string literal is a valid TOML basic string (ASCII, standard escapes)


def supergrid_command(superlink: str, overrides: dict[str, dict]) -> tuple[list[str], str]:
    """Build the `flwr run` command; overrides are written to a temporary run-config TOML file."""
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
        for table, values in overrides.items():
            f.write(f"[{table}]\n" + "".join(f"{k} = {_toml_value(v)}\n" for k, v in values.items()))
        path = f.name
    flwr = Path(sys.executable).parent / "flwr"
    return [str(flwr), "run", str(REPO), superlink, "--run-config", path, "--stream"], path


class _Progress:
    """Shared progress bookkeeping for live and demo jobs (what the progress feed shows)."""

    def _init_progress(self, engine: str, patient_id: str, config: dict) -> None:
        self.engine, self.patient_id, self.config = engine, patient_id, config
        self.status, self.error, self.run_id = "running", None, None
        self.started = time.time()
        self.log: list[str] = []
        self.steps: list[str] = []
        self.step_times: list[float] = []
        self.details: dict[str, str] = {}
        self.fl_progress: str | None = None
        self._step("submitted")

    def _step(self, name: str) -> None:
        self.steps.append(name)
        self.step_times.append(round(time.time() - self.started, 1))

    def view(self) -> dict:
        return {"id": f"{self.started:.3f}", "engine": self.engine, "patient_id": self.patient_id, "status": self.status,
                "error": self.error, "run_id": self.run_id, "elapsed_s": round(time.time() - self.started),
                "steps": self.steps, "step_times": self.step_times, "details": self.details,
                "fl_progress": self.fl_progress, "config": self.config,
                # The raw log is only surfaced when something went wrong.
                "log": self.log[-40:] if self.status == "failed" else []}


class Job(_Progress):
    """One live SuperGrid run, streamed line by line so the dashboard can show it."""

    def __init__(self, engine: str, patient_id: str, cmd: list[str], config_path: str, on_result,
                 config: dict | None = None):
        self._init_progress(engine, patient_id, config or {})
        self._on_result = on_result
        self._config_path = config_path
        # PYTHONUNBUFFERED: without it flwr's piped output only arrives when the process
        # exits, so the progress feed would stay empty for the whole run.
        self._proc = subprocess.Popen(cmd, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, bufsize=1, env={**os.environ, "PYTHONUNBUFFERED": "1"})
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        timer = threading.Timer(SUPERGRID_TIMEOUT_S, self._proc.kill)
        timer.start()
        result = None
        try:
            for raw in self._proc.stdout:
                line = ANSI_RE.sub("", raw.rstrip("\n"))
                if (m := RUN_ID_RE.search(line)) and not self.run_id:
                    self.run_id = m.group(1)
                    self._step("started")
                if "Installing application dependencies" in line:
                    self._step("installing")
                if line.startswith(PHASE_PREFIX):
                    self._step(line[len(PHASE_PREFIX):].split()[0])  # "name t=1.23"
                if line.startswith(FL_PREFIX):
                    self.fl_progress = line[len(FL_PREFIX):].strip()
                idx = line.find(RESULT_PREFIX)
                if idx != -1:
                    result = json.loads(line[idx + len(RESULT_PREFIX):])
                    line = "PREVENTNET_RESULT {…} (result received)"
                self.log.append(line)
                del self.log[:-MAX_JOB_LOG_LINES]
            code = self._proc.wait()
            if result is None and code == -9:
                raise RuntimeError(f"Stopped waiting after {SUPERGRID_TIMEOUT_S}s. SuperGrid may still finish run "
                                   f"{self.run_id}; check it with `uv run flwr ls supergrid`.")
            if result is None:
                raise RuntimeError(f"SuperGrid run ended (exit {code}) without a PREVENTNET_RESULT line")
            self._on_result(self, result)
            self._step("done")
            self.status = "done"
        except Exception as e:  # reported to the dashboard, never swallowed
            self.status, self.error = "failed", f"{type(e).__name__}: {e}"
        finally:
            timer.cancel()
            os.unlink(self._config_path)


class DemoJob(_Progress):
    """Fast Demo Mode: every step of a run, paced over ~20 s, with a prepared assessment.

    The three sources are really asked (so their pages update), the risk model really
    scores the patient, and the verifier really checks the prepared text. Only the
    coordinator's wording comes from demo_responses.json.
    """

    def __init__(self, coord: "Coordinator", patient_id: str, sabotage: bool,
                 timeline: tuple = DEMO_TIMELINE):
        self._init_progress("supergrid", patient_id, {"fast": True, "demo": True, "sabotage": sabotage})
        self._coord, self._sabotage, self._timeline = coord, sabotage, timeline
        variants = DEMO_RESPONSES[patient_id]
        self._response = variants["sabotage"] if sabotage and "sabotage" in variants else variants["normal"]
        threading.Thread(target=self._play, daemon=True).start()

    def _play(self) -> None:
        coord, pid = self._coord, self.patient_id
        ledger, state, fl_risk = Ledger(emit=lambda _e: None), coord._new_state(), None
        try:
            for name, at in self._timeline:
                if name == "submitted":
                    continue
                time.sleep(max(0.0, at - (time.time() - self.started)))
                self._step(name)
                if name == "coordinator_plan":
                    coord._set_pharmacy_sabotage(self._sabotage)
                elif name.startswith("facet_"):
                    answer = coord._ask_site(name.removeprefix("facet_"), pid, ledger, state)
                    shared, declined = len(answer["items"]), len(answer["refusals"])
                    self.details[name] = f"{shared} fact{'s' if shared != 1 else ''} shared" + (
                        f", {declined} question declined" if declined else "")
                elif name == "federated_training":
                    if not coord.fl:
                        coord._train(vfl.DEFAULT_ROUNDS, vfl.DEFAULT_LR)
                    fl_risk = coord._add_fl_fact(ledger, pid)
                    self.details[name] = f"Diabetes risk: {VALUE_LABELS[fl_risk['band']].lower()}"
                elif name == "done":
                    coord._finish_demo(pid, ledger, state, self._response, fl_risk)
            self.status = "done"
        except Exception as e:  # reported to the dashboard, never swallowed
            self.status, self.error = "failed", f"{type(e).__name__}: {e}"


class Coordinator:
    def __init__(self, site_urls: dict[str, str], superlink: str):
        missing = set(ROLES) - set(site_urls)
        if missing:
            raise ValueError(f"missing site URLs for {sorted(missing)}")
        self.sites = site_urls
        # Order matters only for readability; masks cancel regardless.
        self.handles = {role: RemoteSite(site_urls[role]) for role in ("gp", "lab", "pharmacy")}
        self.superlink = superlink
        self.latest: dict | None = None
        self.reviews: dict[str, dict] = {}
        self.decisions: list[dict] = []
        self.fl: dict | None = None
        self.job: Job | DemoJob | None = None
        self.lock = threading.Lock()

    # --- status / explainers ---------------------------------------------------------
    def site_status(self, _body) -> dict:
        out = {}
        for role in ROLES:
            url = self.sites[role]
            try:
                get_json(f"{url}/health")
                out[role] = {"url": url, "online": True, "alignment": self.handles[role].alignment()[:12]}
            except RuntimeError as e:
                out[role] = {"url": url, "online": False, "error": str(e)}
        digests = {s.get("alignment") for s in out.values()}
        return {"sites": out, "aligned": len(digests) == 1 and None not in digests}

    def catalog(self, _body) -> dict:
        keys = ("id", "label", "role", "field", "purpose", "text", "value_type", "derived_from", "why")
        return {"questions": [{k: q[k] for k in keys} | {"allowed_values": q.get("enum", [True, False])}
                              for q in QUESTION_CATALOG], "fl_fact": FL_FACT, "labels": display_labels()}

    def dataset(self, _body) -> dict:
        """Aggregate statistics each source computes locally (federated analytics)."""
        return {"sites": [h.summary() for h in self.handles.values()]}

    def today(self, _body) -> dict:
        """Today's patient list with the doctor-facing status of each."""
        rows, counts = [], {"patients": len(PATIENTS), "reviewed": 0, "approved": 0, "declined": 0,
                            "awaiting": 0, "conflicts": 0}
        for pid in PATIENTS:
            r = self.reviews.get(pid)
            if not r:
                status = "not_reviewed"
            elif r["decision"]:
                status = r["decision"]
            elif r["verdict"] == "APPROVED_FOR_REVIEW":
                status = "awaiting"
            else:
                status = "conflict" if r["verdict"] == "ESCALATE_CONFLICT" else "blocked"
            if r:
                counts["reviewed"] += 1
            key = {"awaiting": "awaiting", "conflict": "conflicts", "blocked": "conflicts"}.get(status)
            if key:
                counts[key] += 1
            rows.append({"patient_id": pid, "status": status, "reviewed_at": r and r["reviewed_at"]})
        # Approvals and declines count every decision made today, even if a patient was reviewed again.
        counts["approved"] = sum(d["action"] == "approved" for d in self.decisions)
        counts["declined"] = sum(d["action"] == "declined" for d in self.decisions)
        return {"patients": rows, "counts": counts,
                "on_screen": self.latest["patient_id"] if self.latest else None}

    # --- risk model over the local sources ------------------------------------------
    def _train(self, rounds: int, lr: float) -> dict:
        try:
            history = vfl.train_federated(self.handles, rounds, lr)
        except (ValueError, RuntimeError) as e:
            raise ApiError(409, str(e)) from e
        self.fl = vfl.fl_report(self.handles, history, rounds, lr, source="local deployments") | {
            "trained_at": _now()}
        return self.fl

    def train(self, body: dict) -> dict:
        rounds, lr = validate_fl_config({"fl.rounds": body.get("rounds", vfl.DEFAULT_ROUNDS),
                                         "fl.lr": body.get("lr", vfl.DEFAULT_LR)})
        with self.lock:
            if self.job and self.job.status == "running":
                raise ApiError(409, "A review is running. Update the model once it has finished.")
            return self._train(rounds, lr)

    def fl_state(self, _body) -> dict:
        return {"fl": self.fl}

    # --- asking the sources ------------------------------------------------------------
    @staticmethod
    def _new_state() -> dict:
        return {"facet_status": {}, "local_views": {}, "sabotaged_roles": []}

    def _ask_site(self, role: str, patient_id: str, ledger: Ledger, state: dict) -> dict:
        qids = [q["id"] for q in QUESTION_CATALOG if q["role"] == role]
        try:
            answer = post_json(f"{self.sites[role]}/ask", {"patient_id": patient_id, "question_ids": qids})
        except RuntimeError as e:
            # A source that is down is reported, never treated as "no findings".
            raise ApiError(502, f"{role} site failed: {e}") from e
        for item in answer["items"]:
            ledger.append(strip_for_wire(item))  # trust nothing: re-validate on arrival
        for r in answer["refusals"]:
            ledger.refuse(Refusal(**r))
        state["facet_status"][role] = "ok"
        state["local_views"][role] = {"records_scanned": answer["records_scanned"]}
        if answer["sabotaged"]:
            state["sabotaged_roles"].append(role)
        return answer

    def _add_fl_fact(self, ledger: Ledger, patient_id: str) -> dict:
        fl_risk = vfl.score_patient(self.handles, patient_id)
        ledger.append(strip_for_wire({"field": FL_FACT["field"], "value": fl_risk["band"],
                                      "purpose": FL_FACT["purpose"], "source": FL_FACT["role"], "ts": _now()}))
        return fl_risk

    def _set_pharmacy_sabotage(self, on: bool) -> None:
        post_json(f"{self.sites['pharmacy']}/admin/sabotage", {"on": on})

    @staticmethod
    def _review_dict(patient_id: str, ledger: Ledger, state: dict) -> dict:
        return {
            "patient_id": patient_id,
            "sabotage": bool(state["sabotaged_roles"]),
            "facet_status": state["facet_status"],
            "local_views": state["local_views"],
            "ledger": [i.as_wire_dict() for i in ledger.items],
            "refusals": [asdict(r) for r in ledger.refusals],
        }

    def _collect_ledger(self, patient_id: str) -> tuple[Ledger, dict]:
        ledger, state = Ledger(emit=lambda _e: None), self._new_state()
        for role in ROLES:
            self._ask_site(role, patient_id, ledger, state)
        fl_risk = self._add_fl_fact(ledger, patient_id) if self.fl else None
        return ledger, {"review": self._review_dict(patient_id, ledger, state), "fl_risk": fl_risk,
                        "sabotaged_roles": state["sabotaged_roles"]}

    # --- reviews ---------------------------------------------------------------------
    def _publish(self, payload: dict, engine: str, extra: dict) -> dict:
        payload |= {"engine": engine, "reviewed_at": _now(), "decision": None, "approval": None, **extra}
        self.latest = payload
        self.reviews[payload["patient_id"]] = {"verdict": payload["verdict"], "reviewed_at": payload["reviewed_at"],
                                               "decision": None}
        return payload

    def _assessed(self, text: str, ledger: Ledger, review: dict, model: str) -> dict:
        result = verifier.verify(text, ledger, verifier.plain_note)
        return {
            **review,
            "model_used": model,
            "coordinator_context_items": [i.id for i in ledger.items],
            "context_item_count": result["context_item_count"],
            "coordinator_raw_md": text,
            "risk_summary_md": result["annotated_summary_md"],
            "verdict": result["verdict"],
            "conflicts": result["conflicts"],
            "struck_claims": result["struck_claims"],
            "verifier_note": result["verifier_note"],
            "recommendation": result["recommendation"],
            "labels": display_labels(),
        }

    def _finish_demo(self, patient_id: str, ledger: Ledger, state: dict, response: dict,
                     fl_risk: dict | None) -> None:
        review = self._review_dict(patient_id, ledger, state)
        payload = self._assessed(response["summary"], ledger, review, response["model"])
        with self.lock:
            self._publish(payload, "supergrid", {"fl": self.fl, "fl_risk": fl_risk, "demo": True, "fast": True,
                                                 "sabotaged_roles": state["sabotaged_roles"]})

    def review(self, body: dict) -> dict:
        patient_id = body.get("patient_id")
        if not isinstance(patient_id, str) or not PATIENT_ID_RE.match(patient_id):
            raise ValueError("patient_id must match ^P\\d{3}$")
        engine = body.get("engine", "local")
        if engine not in ENGINES:
            raise ValueError(f"engine must be one of {ENGINES}")
        fast = body.get("fast", True)
        sabotage = body.get("sabotage", False)
        if not isinstance(fast, bool) or not isinstance(sabotage, bool):
            raise ValueError("fast and sabotage must be booleans")

        with self.lock:
            if self.job and self.job.status == "running":
                raise ApiError(409, "a review is already running")

            if engine == "supergrid" and fast:
                if patient_id not in DEMO_RESPONSES:
                    raise ValueError(f"no prepared demo response for {patient_id}")
                self.job = DemoJob(self, patient_id, sabotage)
                return {"job": self.job.view()}

            if engine == "supergrid":
                rounds, lr = validate_fl_config({"fl.rounds": body.get("rounds", vfl.DEFAULT_ROUNDS),
                                                 "fl.lr": body.get("lr", vfl.DEFAULT_LR)})
                # Mirror the request to the local sources so their pages show it too.
                try:
                    self._set_pharmacy_sabotage(sabotage)
                    self._collect_ledger(patient_id)
                except (ApiError, RuntimeError):
                    pass  # the cloud run does not depend on the local mirrors
                cmd, path = supergrid_command(self.superlink, {
                    "patient": {"id": patient_id}, "demo": {"sabotage": sabotage, "fast": False},
                    "fl": {"rounds": rounds, "lr": lr}})
                self.job = Job(engine, patient_id, cmd, path, self._finish_full_run,
                               {"fast": False, "sabotage": sabotage, "rounds": rounds})
                return {"job": self.job.view()}

            ledger, collected = self._collect_ledger(patient_id)
            extra = {"fl": self.fl, "fl_risk": collected["fl_risk"], "sabotaged_roles": collected["sabotaged_roles"]}

            if engine == "supergrid-ledger":
                cmd, path = supergrid_command(self.superlink, {"review": {"ledger": json.dumps(collected["review"])},
                                                               "demo": {"fast": fast}})
                self.job = Job(engine, patient_id, cmd, path,
                               lambda job, result: self._finish_ledger_run(job, result, ledger, extra),
                               {"fast": fast})
                return {"job": self.job.view()}

            payload = self._assessed(local_summary(ledger, patient_id), ledger, collected["review"],
                                     "local deterministic rules (offline)")
            return self._publish(payload, engine, extra)

    def _finish_full_run(self, job: Job, result: dict) -> None:
        with self.lock:
            self._publish(result, job.engine, {"run_id": job.run_id, "duration_s": round(time.time() - job.started, 1),
                                               "sabotaged_roles": ["pharmacy"] if result.get("sabotage") else []})

    def _finish_ledger_run(self, job: Job, result: dict, ledger: Ledger, extra: dict) -> None:
        # Deterministic cross-check: never let a remote verdict hide a conflict we can see.
        local_conflicts = verifier.check_contradictions(ledger)
        if local_conflicts and result.get("verdict") != "ESCALATE_CONFLICT":
            result |= {"verdict": "ESCALATE_CONFLICT", "conflicts": local_conflicts,
                       "recommendation": verifier.ESCALATE_RECOMMENDATION}
        with self.lock:
            self._publish(result, job.engine, {**extra, "run_id": job.run_id})

    def job_status(self, _body) -> dict:
        return {"job": self.job.view() if self.job else None}

    def latest_payload(self, _body) -> dict:
        if self.latest is None:
            raise ApiError(404, "no review has been run yet")
        return self.latest

    # --- the doctor's decision ----------------------------------------------------------
    def _decide(self, body: dict, action: str) -> dict:
        with self.lock:
            latest = self.latest
            if latest is None or body.get("patient_id") != latest["patient_id"]:
                raise ApiError(409, "the decision must target the review currently on screen")
            if action == "approved" and latest["verdict"] != "APPROVED_FOR_REVIEW":
                raise ApiError(409, f"cannot approve a {latest['verdict']} review; resolve it first")
            if latest["decision"]:
                return latest["decision"]
            clinician = body.get("clinician") or "Doctor"
            if not isinstance(clinician, str) or len(clinician) > 60:
                raise ValueError("clinician must be a short string")
            at = _now()
            decision = {"patient_id": latest["patient_id"], "action": action, "clinician": clinician,
                        "decided_at": at, "recommendation": latest["recommendation"], "engine": latest["engine"]}
            latest["decision"] = decision
            if action == "approved":
                latest["approval"] = decision | {"approved_at": at}
            self.reviews[latest["patient_id"]]["decision"] = action
            self.decisions.append(decision)
            return latest["approval"] or decision

    def approve(self, body: dict) -> dict:
        return self._decide(body, "approved")

    def decline(self, body: dict) -> dict:
        return self._decide(body, "declined")

    def audit(self, _body) -> dict:
        return {"decisions": list(reversed(self.decisions))}


def make_server(site_urls: dict[str, str], port: int, superlink: str = "supergrid",
                host: str = "127.0.0.1") -> ThreadingHTTPServer:
    coord = Coordinator(site_urls, superlink)

    class Handler(JsonHandler):
        get_routes = {"/api/sites": coord.site_status, "/latest.json": coord.latest_payload,
                      "/api/audit": coord.audit, "/api/fl": coord.fl_state, "/api/job": coord.job_status,
                      "/api/catalog": coord.catalog, "/api/dataset": coord.dataset, "/api/today": coord.today}
        post_routes = {"/api/review": coord.review, "/api/fl/train": coord.train, "/api/approve": coord.approve,
                       "/api/decline": coord.decline}
        static_files = {"/": (HERE / "dashboard.html", "text/html; charset=utf-8"),
                        "/ui.css": (HERE / "ui.css", "text/css; charset=utf-8"),
                        "/icons.js": (HERE / "icons.js", "text/javascript; charset=utf-8"),
                        "/viewer.html": (REPO / "viewer" / "viewer.html", "text/html; charset=utf-8")}

    server = ThreadingHTTPServer((host, port), Handler)
    server.coordinator = coord
    return server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--site", action="append", default=[], help="role=url, once per site")
    parser.add_argument("--superlink", default="supergrid")
    args = parser.parse_args()
    if os.environ.get("PREVENTNET_FL_SECRET"):
        raise SystemExit("the coordinator must not hold the sites' FL mask secret")
    sites = dict(s.split("=", 1) for s in args.site)
    server = make_server(sites, args.port, args.superlink)
    print(f"[coordinator] dashboard on http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
