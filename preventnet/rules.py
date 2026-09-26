"""Deterministic data-steward rules: answer catalog questions from one department's own records.

Used by the local site deployments (which have no model runtime) and by the
SuperGrid AgentApp in fast mode. No model ever reads the raw records on this
path; only the categorical answer leaves. Returning None means "records don't
determine an answer" (nothing is disclosed).
"""

from __future__ import annotations

import os
from datetime import date

NSAIDS = {"ibuprofen", "naproxen", "diclofenac", "aspirin_high_dose"}
EGFR_ORDER = ["normal", "mildly_reduced", "moderately_reduced", "severely_reduced"]
LOOKBACK_DAYS = 90

# Synthetic records are dated around mid-2026; reviews are evaluated "as of" this date.
AS_OF = date.fromisoformat(os.environ.get("PREVENTNET_AS_OF", "2026-06-20"))


def _within_lookback(iso: str) -> bool:
    return 0 <= (AS_OF - date.fromisoformat(iso)).days <= LOOKBACK_DAYS


def _latest(records: list[dict], **match) -> dict | None:
    hits = [r for r in records if all(r.get(k) == v for k, v in match.items())]
    return max(hits, key=lambda r: r["date"]) if hits else None


def _gp(qid: str, records: list[dict]):
    if qid == "q_gp_conditions":
        conditions = {r["condition"] for r in records if r["kind"] == "diagnosis"}
        for c in ("type2_diabetes", "hypertension"):
            if c in conditions:
                return c
        return "none_relevant"
    if qid == "q_gp_planned_imaging":
        return any(r["kind"] == "referral" and "contrast" in r.get("referral_type", "") for r in records)
    if qid == "q_gp_reported_otc":
        notes = " ".join(r.get("note", "").lower() for r in records if r["kind"] == "visit_note")
        return any(drug in notes for drug in NSAIDS | {"nsaid"})
    return None


def _pharmacy(qid: str, records: list[dict]):
    dispenses = [r for r in records if r["kind"] == "dispense"]
    if qid == "q_ph_nsaid_90d":
        return any(r["drug"] in NSAIDS and _within_lookback(r["date"]) for r in dispenses)
    if qid == "q_ph_metformin_active":
        return any(r["drug"] == "metformin" and _within_lookback(r["date"]) for r in dispenses)
    if qid == "q_ph_interacting_pairs":
        return any(r["kind"] == "interaction_flag" and r.get("triggered") for r in records)
    return None


def _lab(qid: str, records: list[dict]):
    egfr = sorted((r for r in records if r.get("test") == "egfr"), key=lambda r: r["date"])
    if qid == "q_lab_egfr_band":
        return egfr[-1]["egfr_band"] if egfr else None
    if qid == "q_lab_egfr_trend":
        if len(egfr) < 2:
            return None
        delta = EGFR_ORDER.index(egfr[-1]["egfr_band"]) - EGFR_ORDER.index(egfr[0]["egfr_band"])
        return "declining" if delta > 0 else "improving" if delta < 0 else "stable"
    if qid == "q_lab_hba1c_band":
        latest = _latest(records, test="hba1c")
        return latest["hba1c_band"] if latest else None
    return None


RULES = {"gp": _gp, "pharmacy": _pharmacy, "lab": _lab}


def answer(role: str, questions: list[dict], records: list[dict]) -> dict:
    return {q["id"]: RULES[role](q["id"], records) for q in questions}
