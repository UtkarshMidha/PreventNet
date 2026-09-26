"""One institutional deployment (GP, Pharmacy or Lab).

Holds its own raw records and its own Pima columns. Exposes:
  GET  /               this site's own overview page (raw data shown LOCALLY only)
  GET  /api/overview   data for that page, never called by the coordinator
  POST /ask            answer purpose-tagged catalog questions -> wire items/refusals only
  POST /fl/*           its half of vertical federated learning (see vfl.py)
  POST /admin/sabotage demo switch (pharmacy only)

Usage: PREVENTNET_FL_SECRET=... uv run python -m local.site --role gp --port 8101
"""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from preventnet.facets import SABOTAGE_QUESTION_IDS, SABOTAGE_ROLE, disclose, split_by_policy
from preventnet import rules
from preventnet.policy import QUESTION_CATALOG, ROLE_LABELS, display_labels
from preventnet.vfl import SiteModel

from .net import ApiError, JsonHandler, post_json

HERE = Path(__file__).parent
DATA_DIR = HERE.parent / "preventnet" / "data"
PATIENT_ID_RE = re.compile(r"^P\d{3}$")
SABOTAGED_FIELDS = {q["field"] for q in QUESTION_CATALOG if q["id"] in SABOTAGE_QUESTION_IDS}
MAX_QUESTIONS = 20
MAX_AUDIT = 200
MAX_LR = 5.0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_patient(pid) -> str:
    if not isinstance(pid, str) or not PATIENT_ID_RE.match(pid):
        raise ValueError("patient_id must match ^P\\d{3}$")
    return pid


def _check_lr(lr) -> float:
    if not isinstance(lr, (int, float)) or not 0 < lr <= MAX_LR:
        raise ValueError(f"lr must be in (0, {MAX_LR}]")
    return float(lr)


class Site:
    def __init__(self, role: str, data_dir: Path, secret: str, peers: list[str]):
        self.role = role
        self.peers = peers
        self.records = [
            json.loads(line) for line in (data_dir / "records.jsonl").read_text().splitlines() if line.strip()
        ]
        self.model = SiteModel(role, data_dir / "cohort.jsonl", secret)
        with (data_dir / "cohort.jsonl").open() as f:
            self.cohort_columns = json.loads(f.readline())["columns"]
            self.cohort_preview = [json.loads(next(f)) for _ in range(8)]
        self.sabotage = False
        self.audit: list[dict] = []
        self.lock = threading.Lock()

    def _log(self, entry: dict) -> None:
        self.audit.append({"at": _now(), **entry})
        del self.audit[:-MAX_AUDIT]

    # --- purpose-tagged Q&A -------------------------------------------------
    def ask(self, body: dict) -> dict:
        patient_id = _check_patient(body.get("patient_id"))
        qids = body.get("question_ids")
        if not isinstance(qids, list) or len(qids) > MAX_QUESTIONS:
            raise ValueError(f"question_ids must be a list of at most {MAX_QUESTIONS}")
        questions = [q for q in QUESTION_CATALOG if q["id"] in set(qids)]

        with self.lock:
            permitted, refusals = split_by_policy(self.role, questions)
            records = [r for r in self.records if r["patient_id"] == patient_id]
            answers = rules.answer(self.role, permitted, records)
            items, sabotaged = disclose(self.role, permitted, answers, self.sabotage)
            for item in items:
                self._log({"patient_id": patient_id, "disclosed": item.field, "value": item.value,
                           "purpose": item.purpose, "sabotaged": sabotaged and item.field in SABOTAGED_FIELDS})
            for r in refusals:
                self._log({"patient_id": patient_id, "refused": r.question_id, "reason": r.reason})

        wire = []
        for item in items:
            d = item.as_wire_dict()
            d.pop("id")
            wire.append(d)
        return {
            "role": self.role,
            "records_scanned": len(records),
            "items": wire,
            "refusals": [asdict(r) for r in refusals],
            "sabotaged": sabotaged,
        }

    def set_sabotage(self, body: dict) -> dict:
        if self.role != SABOTAGE_ROLE:
            raise ApiError(403, f"sabotage demo only applies to the {SABOTAGE_ROLE} site")
        if not isinstance(body.get("on"), bool):
            raise ValueError("'on' must be a bool")
        with self.lock:
            self.sabotage = body["on"]
            self._log({"admin": f"sabotage {'ON' if self.sabotage else 'OFF'}"})
        return {"sabotage": self.sabotage}

    # --- vertical FL ----------------------------------------------------------
    def fl_reset(self, _body) -> dict:
        with self.lock:
            self.model.reset()
        return {"ok": True}

    def fl_summary(self, _body) -> dict:
        return self.model.summary()

    def fl_alignment(self, _body) -> dict:
        return {"role": self.role, "digest": self.model.alignment_digest()}

    def fl_forward(self, body: dict) -> dict:
        split, nonce = body.get("split"), body.get("nonce")
        if not isinstance(nonce, str) or len(nonce) < 16:
            raise ValueError("nonce must be a string of at least 16 chars")
        entity = _check_patient(body.get("patient_id")) if split == "review" else None
        with self.lock:
            return {"masked_scores": self.model.forward(split, nonce, entity)}

    def fl_backward(self, body: dict) -> dict:
        with self.lock:
            self.model.backward(body["residual"], _check_lr(body.get("lr")))
        return {"ok": True}

    def fl_residual(self, body: dict) -> dict:
        lr = _check_lr(body.get("lr"))
        with self.lock:
            residual, metrics = self.model.residual_step(body["z"], lr)
        # Residuals go peer-to-peer; the coordinator only gets aggregate metrics.
        for peer in self.peers:
            post_json(f"{peer}/fl/backward", {"residual": residual, "lr": lr})
        return metrics

    def fl_evaluate(self, body: dict) -> dict:
        with self.lock:
            return self.model.evaluate(body["z"])

    # --- local-only overview ---------------------------------------------------
    def overview(self, _body) -> dict:
        m = self.model
        with self.lock:
            return {
                "role": self.role,
                "role_label": ROLE_LABELS[self.role],
                "sabotage": self.sabotage,
                "labels": display_labels(),
                "records": self.records,
                "cohort": {"columns": self.cohort_columns, "preview": self.cohort_preview,
                           "counts": m.summary()["splits"],
                    "holds_labels": m.is_label_holder, "summary": m.summary()},
                "audit": list(reversed(self.audit)),
            }


def make_server(role: str, port: int, data_dir: Path, secret: str, peers: list[str],
                host: str = "127.0.0.1") -> ThreadingHTTPServer:
    site = Site(role, data_dir, secret, peers)

    class Handler(JsonHandler):
        get_routes = {"/health": lambda _b: {"role": role, "status": "ok"},
                      "/api/overview": site.overview,
                      "/fl/alignment": site.fl_alignment, "/fl/summary": site.fl_summary}
        post_routes = {"/ask": site.ask, "/admin/sabotage": site.set_sabotage,
                       "/fl/reset": site.fl_reset, "/fl/forward": site.fl_forward,
                       "/fl/backward": site.fl_backward}
        if site.model.is_label_holder:
            post_routes |= {"/fl/residual": site.fl_residual, "/fl/evaluate": site.fl_evaluate}
        static_files = {"/": (HERE / "site.html", "text/html; charset=utf-8"),
                        "/ui.css": (HERE / "ui.css", "text/css; charset=utf-8"),
                        "/icons.js": (HERE / "icons.js", "text/javascript; charset=utf-8")}

    server = ThreadingHTTPServer((host, port), Handler)
    server.site = site
    return server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", required=True, choices=["gp", "pharmacy", "lab"])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--peers", default="", help="comma-separated site URLs (label holder pushes residuals)")
    args = parser.parse_args()
    secret = os.environ.get("PREVENTNET_FL_SECRET", "")
    if not secret:
        raise SystemExit("PREVENTNET_FL_SECRET must be set (shared by the sites, never by the coordinator)")
    data_dir = args.data_dir or DATA_DIR / args.role
    peers = [p for p in args.peers.split(",") if p]
    server = make_server(args.role, args.port, data_dir, secret, peers)
    print(f"[{args.role}] local deployment on http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
