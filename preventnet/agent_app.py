"""PreventNet AgentApp entry point: federated preventive-risk review.

Phases: plan -> GP / Pharmacy / Lab agents -> federated diabetes model across the
three departments -> coordinator reasoning -> verifier. Each agent phase is an
isolated model conversation; only wire-format ledger items (and masked, summed
federated scores) ever reach the coordinator.
"""

from __future__ import annotations
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx
import openai
from flwr.agentapp import AgentApp, AgentSession
from flwr.app import ConfigRecord, Context
from openai import OpenAI

from . import coordinator, verifier, vfl
from .facets import DATA_DIR, facet_answer, facet_rules
from .policy import FL_FACT, QUESTION_CATALOG, display_labels
from .wire import Ledger, Refusal, strip_for_wire

MAX_MODEL_CALLS = 8
MAX_REVIEW_ITEMS = 40
MAX_FL_ROUNDS = 200
MAX_FL_LR = 5.0
ENDEAVOR_SENTINEL = "ENDEAVOR_MODEL_REF_TBD"
PATIENT_ID_RE = re.compile(r"^P\d{3}$")
ROLES = ("gp", "pharmacy", "lab")

app = AgentApp()


def _validate_run_config(run_config: dict) -> tuple[str, str, bool]:
    prompt = run_config.get("agent.input")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("agent.input must be a non-empty string")

    patient_id = run_config.get("patient.id")
    if not isinstance(patient_id, str) or not PATIENT_ID_RE.match(patient_id):
        raise ValueError("patient.id must match ^P\\d{3}$")

    sabotage = run_config.get("demo.sabotage", False)
    if not isinstance(sabotage, bool):
        raise ValueError("demo.sabotage must be a bool")

    return prompt.strip(), patient_id, sabotage


def validate_fl_config(run_config: dict) -> tuple[int, float]:
    rounds = run_config.get("fl.rounds", vfl.DEFAULT_ROUNDS)
    lr = run_config.get("fl.lr", vfl.DEFAULT_LR)
    if isinstance(rounds, bool) or not isinstance(rounds, int) or not 1 <= rounds <= MAX_FL_ROUNDS:
        raise ValueError(f"fl.rounds must be an int in [1, {MAX_FL_ROUNDS}]")
    if isinstance(lr, bool) or not isinstance(lr, (int, float)) or not 0 < lr <= MAX_FL_LR:
        raise ValueError(f"fl.lr must be a number in (0, {MAX_FL_LR}]")
    return rounds, float(lr)


def run_federated_phase(agent, ledger: Ledger, patient_id: str, rounds: int, lr: float) -> tuple[dict, dict]:
    """Train the diabetes model across the departments, then add one risk fact to the ledger."""
    departments = vfl.local_federation(DATA_DIR)

    def on_round(point: dict) -> None:
        if "test_auc" in point:
            agent.events.emit({"type": "preventnet.fl_round", "preventnet": "fl_round", **point})
            print(f"PREVENTNET_FL round {point['round']}/{rounds} train_loss={point['train_loss']:.3f} "
                  f"test_acc={point['test_accuracy']:.3f} test_auc={point['test_auc']:.3f}", flush=True)

    history = vfl.train_federated(departments, rounds, lr, on_round)
    fl = vfl.fl_report(departments, history, rounds, lr, source="supergrid")
    risk = vfl.score_patient(departments, patient_id)
    ledger.append(strip_for_wire({"field": FL_FACT["field"], "value": risk["band"], "purpose": FL_FACT["purpose"],
                                  "source": FL_FACT["role"], "ts": _now_ts()}))
    return fl, risk


def run_facets_parallel(caller, ledger: Ledger, emit, patient_id: str, questions: list[dict],
                        sabotage: bool) -> tuple[dict, dict]:
    """Run the three department agents at once; merge their answers in a fixed order.

    Each agent still gets its own isolated model conversation and only its own
    records. Results go into per-agent scratch ledgers and are appended to the
    real ledger in ROLES order, so fact IDs (L1, L2, ...) match the sequential run.
    """
    def one(role: str):
        events: list[dict] = []
        scratch = Ledger(emit=lambda _e: None)
        scanned = facet_answer(role=role, patient_id=patient_id, questions=questions, model_call=caller.call,
                               ledger=scratch, emit=events.append, sabotage=sabotage)
        return scratch, events, scanned

    with ThreadPoolExecutor(max_workers=len(ROLES)) as pool:
        results = list(pool.map(one, ROLES))  # re-raises the first agent failure

    facet_status, local_views = {}, {}
    for role, (scratch, events, scanned) in zip(ROLES, results):
        for event in events:  # run events are emitted from this thread only
            emit(event)
        for refusal in scratch.refusals:
            ledger.refuse(refusal)
        for item in scratch.items:
            ledger.append(item)
        facet_status[role] = "ok"
        local_views[role] = {"records_scanned": scanned}
    return facet_status, local_views


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_RUN_T0 = time.monotonic()


def _phase_event(name: str) -> dict:
    # Printed too, so the dashboard can show progress from the streamed run log.
    elapsed = round(time.monotonic() - _RUN_T0, 2)
    print(f"PREVENTNET_PHASE {name} t={elapsed}", flush=True)
    return {"type": "preventnet.phase", "preventnet": "phase", "name": name, "t": elapsed}


def _looks_like_model_not_found(err: Exception) -> bool:
    if isinstance(err, (openai.NotFoundError, openai.BadRequestError)):
        return "model" in str(err).lower()
    return False


class ModelCaller:
    """Wraps client.responses.create with a call cap and Endeavor→fallback switch."""

    def __init__(self, client: OpenAI, agent: AgentSession, model_ref: str, model_fallback: str):
        self._client = client
        self._agent = agent
        self._fallback = model_fallback
        self.model = model_fallback if model_ref == ENDEAVOR_SENTINEL else model_ref
        self.count = 0
        self.fallback_reason: str | None = None
        self._lock = threading.Lock()

    def _bump(self) -> None:
        with self._lock:  # facets may call the model from parallel threads
            self.count += 1
            if self.count > MAX_MODEL_CALLS:
                raise RuntimeError(f"exceeded MAX_MODEL_CALLS={MAX_MODEL_CALLS}")

    def _switch_to_fallback(self, err: Exception) -> None:
        self._agent.events.emit(
            {
                "type": "preventnet.model_fallback",
                "preventnet": "model_fallback",
                "from": self.model,
                "to": self._fallback,
                "reason": str(err),
            }
        )
        self.model = self._fallback

    def call(self, instructions: str, input_text: str) -> str:
        """One non-streamed, isolated model call. Returns output_text."""
        self._bump()
        try:
            response = self._client.responses.create(
                model=self.model, input=input_text, instructions=instructions
            )
        except Exception as err:
            if self.count == 1 and self.model != self._fallback and _looks_like_model_not_found(err):
                self._switch_to_fallback(err)
                response = self._client.responses.create(
                    model=self.model, input=input_text, instructions=instructions
                )
            else:
                raise
        return response.output_text

    def call_streamed(self, instructions: str, input_text: str, budget_s: float | None = None,
                      emit_tokens: bool = True) -> str:
        """One streamed, isolated model call; returns the concatenated text.

        budget_s: if the primary model hasn't finished in time, switch once to the
        fallback model (reported as a preventnet.model_fallback event).
        emit_tokens: forward every stream event to SuperGrid. Off in fast mode:
        hundreds of token events are published in small batches and slow the run.
        """
        self._bump()

        def run(model: str, deadline: float | None) -> str:
            chunks: list[str] = []
            extra = {"timeout": max(1.0, deadline - time.monotonic())} if deadline else {}
            stream = self._client.responses.create(
                model=model, input=input_text, instructions=instructions, stream=True, **extra
            )
            for event in stream:
                if emit_tokens or event.type != "response.output_text.delta":
                    self._agent.events.emit(event.to_dict())
                if event.type in {"error", "response.failed", "response.incomplete"}:
                    raise RuntimeError(f"model response failed: {event}")
                if event.type == "response.output_text.delta":
                    chunks.append(event.delta)
                if deadline and time.monotonic() > deadline:
                    raise TimeoutError(f"{model} did not finish within {budget_s:g}s")
            return "".join(chunks)

        deadline = time.monotonic() + budget_s if budget_s and self.model != self._fallback else None
        try:
            return run(self.model, deadline)
        except Exception as err:
            # During streaming the SDK surfaces httpx's own timeout, not APITimeoutError.
            timed_out = deadline is not None and isinstance(
                err, (TimeoutError, openai.APITimeoutError, httpx.TimeoutException))
            not_found = self.count == 1 and self.model != self._fallback and _looks_like_model_not_found(err)
            if not (timed_out or not_found):
                raise
            self._bump()  # the retry on the fallback model is a second call
            self._switch_to_fallback(err)
            self.fallback_reason = "timeout" if timed_out else "not_found"
            return run(self.model, None)


def _fast_facets(ledger: Ledger, emit, patient_id: str, questions: list[dict], sabotage: bool) -> tuple[dict, dict]:
    """Fast mode: each department answers with its own auditable rules (no model call)."""
    facet_status, local_views = {}, {}
    for role in ROLES:
        local_views[role] = {"records_scanned": facet_rules(role, patient_id, questions, ledger, emit, sabotage)}
        facet_status[role] = "ok"
    return facet_status, local_views


def parse_review_ledger(raw: str, emit) -> tuple[Ledger, dict]:
    """Rebuild a Ledger from wire items disclosed by the local site deployments.

    Every item is re-validated through the allowlist: the SuperGrid side trusts
    nothing the local coordinator sends it.
    """
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("review.ledger must be a JSON object")
    patient_id = data.get("patient_id")
    if not isinstance(patient_id, str) or not PATIENT_ID_RE.match(patient_id):
        raise ValueError("review.ledger patient_id must match ^P\\d{3}$")
    items, refusals = data.get("ledger", []), data.get("refusals", [])
    if not isinstance(items, list) or len(items) > MAX_REVIEW_ITEMS:
        raise ValueError(f"review.ledger must carry a list of at most {MAX_REVIEW_ITEMS} items")
    if not isinstance(refusals, list) or len(refusals) > MAX_REVIEW_ITEMS:
        raise ValueError(f"review.ledger refusals must be a list of at most {MAX_REVIEW_ITEMS}")

    ledger = Ledger(emit=emit)
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("review.ledger items must be objects")
        ledger.append(strip_for_wire({k: v for k, v in item.items() if k != "id"}))
    for r in refusals:
        if not isinstance(r, dict) or set(r) != {"question_id", "role", "reason"}:
            raise ValueError("review.ledger refusals must have exactly question_id, role, reason")
        if not all(isinstance(v, str) and len(v) <= 40 for v in r.values()):
            raise ValueError("review.ledger refusal values must be short strings")
        ledger.refuse(Refusal(**r))

    meta = {
        "patient_id": patient_id,
        "sabotage": bool(data.get("sabotage", False)),
        "facet_status": data.get("facet_status", {}),
        "local_views": data.get("local_views", {}),
    }
    return ledger, meta


@app.main()
def main(agent: AgentSession, context: Context) -> None:
    global _RUN_T0
    _RUN_T0 = time.monotonic()
    prompt, patient_id, sabotage = _validate_run_config(context.run_config)
    review_ledger = context.run_config.get("review.ledger", "")
    if not isinstance(review_ledger, str):
        raise ValueError("review.ledger must be a string")
    fl_rounds, fl_lr = validate_fl_config(context.run_config)
    fast = context.run_config.get("demo.fast", False)
    if not isinstance(fast, bool):
        raise ValueError("demo.fast must be a bool")
    model_budget = context.run_config.get("model.budget_s", 12)
    if isinstance(model_budget, bool) or not isinstance(model_budget, (int, float)) or not 1 <= model_budget <= 240:
        raise ValueError("model.budget_s must be a number of seconds between 1 and 240")
    fl, fl_risk = None, None

    model_ref = context.run_config.get("model.ref", ENDEAVOR_SENTINEL)
    model_fallback = context.run_config.get("model.fallback", "openai/gpt-5.6-sol")

    client = OpenAI(
        base_url=os.environ["FLWR_RUNTIME_BASE_URL"],
        api_key=os.environ["FLWR_RUNTIME_API_KEY"],
        max_retries=0,
    )
    caller = ModelCaller(client, agent, model_ref, model_fallback)

    try:
        if review_ledger:
            # Ledger-only mode: facts were disclosed by the local site
            # deployments; no raw record is read anywhere in this run.
            agent.events.emit(_phase_event("ledger_from_local_sites"))
            ledger, meta = parse_review_ledger(review_ledger, agent.events.emit)
            patient_id, sabotage = meta["patient_id"], meta["sabotage"]
            facet_status, local_views = meta["facet_status"], meta["local_views"]
        else:
            ledger = Ledger(emit=agent.events.emit)
            if fast:
                # Fast demo mode (one model call in total): no plan call, and the
                # departments answer with their own rules, so no model reads records.
                questions = list(QUESTION_CATALOG)
                agent.events.emit({"type": "preventnet.plan", "preventnet": "plan",
                                   "model_picked": [], "used": [q["id"] for q in questions], "skipped": True})
                agent.events.emit(_phase_event("facets"))
                facet_status, local_views = _fast_facets(ledger, agent.events.emit, patient_id, questions, sabotage)
            else:
                # Phase 0: Coordinator plans which questions to ask (model call #1).
                agent.events.emit(_phase_event("coordinator_plan"))
                planned_ids, model_picked_ids = coordinator.plan_questions(caller.call, patient_id)
                agent.events.emit(
                    {
                        "type": "preventnet.plan",
                        "preventnet": "plan",
                        "model_picked": model_picked_ids,
                        "used": planned_ids,
                    }
                )
                questions = [q for q in QUESTION_CATALOG if q["id"] in set(planned_ids)]

                # Phases 1-3: the three department agents (model calls #2-4), each an
                # isolated conversation over its own records, run in parallel.
                for role in ROLES:
                    agent.events.emit(_phase_event(f"facet_{role}"))
                facet_status, local_views = run_facets_parallel(caller, ledger, agent.events.emit,
                                                                patient_id, questions, sabotage)

            # Federated phase (no model calls): each department trains its own slice
            # of the diabetes model; the coordinator sees only masked, summed scores.
            agent.events.emit(_phase_event("federated_training"))
            fl, fl_risk = run_federated_phase(agent, ledger, patient_id, fl_rounds, fl_lr)
            agent.events.emit({"type": "preventnet.fl_done", "preventnet": "fl_done",
                               "test_auc": fl["test_auc"], "risk_band": fl_risk["band"]})

        # Phase 4: Coordinator reasons over the ledger only (model call #5, streamed).
        agent.events.emit(_phase_event("coordinator_reason"))
        stream = (lambda i, t: caller.call_streamed(i, t, budget_s=model_budget, emit_tokens=False)) if fast \
            else caller.call_streamed
        risk_summary_md = coordinator.reason_over_ledger(stream, ledger, concise=fast)
        coordinator_raw_md = risk_summary_md
        agent.events.emit({"type": "preventnet.coordinator_output", "preventnet": "coordinator_output",
                           "model": caller.model, "text": risk_summary_md})

        # Phase 5: Verifier: deterministic checks decide the verdict; one
        # model call writes prose only (model call #6).
        agent.events.emit(_phase_event("verifier"))
        # In fast mode the verdict's plain-language note is written by code, not a model.
        verdict_result = verifier.verify(risk_summary_md, ledger, verifier.plain_note if fast else caller.call)
        risk_summary_md = verdict_result["annotated_summary_md"]
        verdict = verdict_result["verdict"]
        conflicts = verdict_result["conflicts"]
        struck_claims = verdict_result["struck_claims"]
        verifier_note = verdict_result["verifier_note"]
        recommendation = verdict_result["recommendation"]
        agent.events.emit(
            {
                "type": "preventnet.verdict",
                "preventnet": "verdict",
                "verdict": verdict,
                "conflicts": len(conflicts),
            }
        )

    except Exception as err:
        agent.events.emit({"type": "preventnet.failed", "preventnet": "failed", "reason": str(err)})
        raise

    payload = {
        "patient_id": patient_id,
        "model_used": caller.model,
        "sabotage": sabotage,
        "facet_status": facet_status,
        "local_views": local_views,
        "ledger": [item.as_wire_dict() for item in ledger.items],
        "refusals": [
            {"question_id": r.question_id, "role": r.role, "reason": r.reason} for r in ledger.refusals
        ],
        "coordinator_context_items": [item.id for item in ledger.items],
        "context_item_count": len(ledger.items),
        "risk_summary_md": risk_summary_md,
        "verdict": verdict,
        "conflicts": conflicts,
        "struck_claims": struck_claims,
        "verifier_note": verifier_note,
        "recommendation": recommendation,
        "coordinator_raw_md": coordinator_raw_md,
        "fl": fl,
        "fl_risk": fl_risk,
        "labels": display_labels(),
        "fast": fast,
        "model_calls": caller.count,
        "model_fallback": caller.fallback_reason,
        "app_seconds": round(time.monotonic() - _RUN_T0, 2),
    }

    context.state.config_records["preventnet"] = ConfigRecord({"result": json.dumps(payload)})

    print("PREVENTNET_RESULT " + json.dumps(payload, separators=(",", ":")))
    print(risk_summary_md)
