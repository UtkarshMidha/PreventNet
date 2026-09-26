"""Coordinator phases: plan which questions to ask, then reason over the ledger only.

Neither function ever touches raw patient records. plan_questions only sees
the closed question catalog; reason_over_ledger only sees wire-format ledger
items and refusals, never the facet agents' raw model conversations.
"""

from __future__ import annotations

import json
import re
from typing import Callable

from . import prompts
from .policy import QUESTION_CATALOG
from .wire import Ledger


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def plan_questions(model_call: Callable[[str, str], str], patient_id: str) -> tuple[list[str], list[str]]:
    """Ask the model to pick catalog question ids for a preventive review.

    Returns (final_ids, model_picked_ids). The model's picks are shown for
    transparency but the full seeded catalog is always unioned in afterward,
    so the demo's data flow is deterministic regardless of the model's choice.
    """
    catalog_summary = [
        {"id": q["id"], "role": q["role"], "field": q["field"], "purpose": q["purpose"], "text": q["text"]}
        for q in QUESTION_CATALOG
    ]
    output = model_call(
        prompts.COORDINATOR_PLAN_INSTRUCTIONS,
        json.dumps({"patient_id": patient_id, "catalog": catalog_summary}),
    )
    try:
        picked = json.loads(_strip_code_fences(output))
        if not isinstance(picked, list):
            picked = []
    except json.JSONDecodeError:
        picked = []

    seeded_ids = [q["id"] for q in QUESTION_CATALOG]
    final_ids = [qid for qid in seeded_ids if qid in set(picked) | set(seeded_ids)]
    return final_ids, picked


def reason_over_ledger(model_call_streamed: Callable[[str, str], str], ledger: Ledger, concise: bool = False) -> str:
    """Coordinator's risk-review call. Input is ONLY the ledger's wire items."""
    context_items = [item.as_wire_dict() for item in ledger.items]
    refusals = [
        {"question_id": r.question_id, "role": r.role, "reason": r.reason} for r in ledger.refusals
    ]
    input_text = json.dumps({"ledger": context_items, "refusals": refusals})
    instructions = prompts.COORDINATOR_REASON_INSTRUCTIONS + (prompts.CONCISE_SUFFIX if concise else "")
    return model_call_streamed(instructions, input_text)


def section_header(line: str) -> str:
    """'## Risks', '**RISKS:**', 'RISKS' -> 'RISKS'; anything else -> ''."""
    text = re.sub(r"^[#\s]+", "", line).strip().strip("*").strip().rstrip(":").strip("*").strip().upper()
    return text if text in ("RISKS", "RECOMMENDATION", "CONFIDENCE") else ""


def extract_recommendation(risk_summary_md: str) -> str:
    """Pull the RECOMMENDATION section's text out of the coordinator's markdown."""
    out, inside = [], False
    for line in risk_summary_md.splitlines():
        header = section_header(line)
        if header:
            inside = header == "RECOMMENDATION"
        elif inside and line.strip():
            out.append(re.sub(r"^[-*]\s+", "", line.strip()).replace("**", ""))
    return " ".join(out).strip()
