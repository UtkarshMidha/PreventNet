"""Deterministic verification of the Coordinator's risk summary.

The verdict is always decided by code (citation check + contradiction rules +
allowlist audit). The explanation is written by a model call (prose only) or,
in fast mode, by plain_note(). Neither can change the verdict.
"""

from __future__ import annotations

import json
import re
from typing import Callable

from . import coordinator, prompts
from .wire import Ledger, WireItem, strip_for_wire

CITATION_RE = re.compile(r"\[([^\[\]]+)\]")

ESCALATE_RECOMMENDATION = (
    "The sources disagree. Check the prescription log with the patient before acting on this review."
)


def _find_section_bounds(lines: list[str], start_header: str, end_header: str) -> tuple[int, int]:
    start, end = None, len(lines)
    for i, line in enumerate(lines):
        header = coordinator.section_header(line)
        if start is None and header == start_header:
            start = i + 1
        elif start is not None and header == end_header:
            end = i
            break
    return (start, end) if start is not None else (0, 0)


def check_citations(risk_summary_md: str, ledger: Ledger) -> tuple[str, int, bool]:
    """Strike any RISKS-section claim line with no citation to a real ledger id.

    Returns (annotated_md, struck_count, any_risk_line_survives).
    """
    valid_ids = {item.id for item in ledger.items}
    lines = risk_summary_md.splitlines()
    start, end = _find_section_bounds(lines, "RISKS", "RECOMMENDATION")
    if (start, end) == (0, 0):
        return risk_summary_md, 1, False  # no RISKS section at all: fail closed, nothing is verifiable

    struck = 0
    survives = False
    for i in range(start, end):
        stripped = lines[i].strip()
        if not stripped or not stripped.startswith(("-", "*")):
            continue
        cited = CITATION_RE.findall(lines[i])
        if any(cid in valid_ids for cid in cited):
            survives = True
        else:
            lines[i] = f"~~{lines[i]}~~"
            struck += 1

    return "\n".join(lines), struck, survives


def _index_by_role_field(ledger: Ledger) -> dict[tuple[str, str], WireItem]:
    return {(item.source, item.field): item for item in ledger.items}


def check_contradictions(ledger: Ledger) -> list[dict]:
    """Hardcoded contradiction rules: this is what catches a sabotaged agent."""
    idx = _index_by_role_field(ledger)
    conflicts: list[dict] = []

    otc = idx.get(("gp", "patient_reported_otc_nsaid"))
    dispensed = idx.get(("pharmacy", "nsaid_dispensed_90d"))
    if otc is not None and dispensed is not None and otc.value is True and dispensed.value is False:
        conflicts.append(
            {
                "rule": "otc_nsaid_vs_pharmacy_dispense",
                "description": (
                    "Health Diagnostics notes NSAID painkiller use, but Prescription Logs "
                    "show none dispensed in the last 90 days."
                ),
                "ledger_ids": [otc.id, dispensed.id],
            }
        )

    metformin = idx.get(("pharmacy", "metformin_active"))
    interacting = idx.get(("pharmacy", "interacting_dispense_flag"))
    if (
        metformin is not None
        and interacting is not None
        and otc is not None
        and metformin.value is True
        and interacting.value is False
        and otc.value is True
    ):
        conflicts.append(
            {
                "rule": "metformin_nsaid_interaction_not_flagged",
                "description": (
                    "Prescription Logs show metformin with no interaction alert, "
                    "although Health Diagnostics notes NSAID use."
                ),
                "ledger_ids": [metformin.id, interacting.id, otc.id],
            }
        )

    return conflicts


def audit_allowlist(ledger: Ledger) -> int:
    """Re-validate every ledger item against the wire allowlist. Raises on violation."""
    for item in ledger.items:
        raw = item.as_wire_dict()
        raw.pop("id", None)
        strip_for_wire(raw)
    return len(ledger.items)


def plain_note(_instructions: str, input_text: str) -> str:
    """Code-written explanation of a verdict (used instead of a model call when speed matters)."""
    facts = json.loads(input_text)
    if facts["verdict"] == "ESCALATE_CONFLICT":
        return (f"The sources disagree on {len(facts['conflicts'])} point(s), so no recommendation is released. "
                "The records need to be reconciled first.")
    if facts["verdict"] == "BLOCKED_UNVERIFIED":
        return "None of the risk claims could be traced to disclosed facts, so the summary is blocked."
    removed = facts["struck_claims"]
    return (f"The remaining risk claims trace back to the {facts['context_item_count']} disclosed facts"
            f"{f', and {removed} unsupported claim(s) were removed' if removed else ''}. No cross-institution "
            "contradictions were found, so the recommendation awaits clinician approval.")


def verify(risk_summary_md: str, ledger: Ledger, model_call: Callable[[str, str], str]) -> dict:
    """Run all deterministic checks, decide the verdict, then explain it in prose."""
    context_item_count = audit_allowlist(ledger)
    annotated_md, struck_claims, any_risk_survives = check_citations(risk_summary_md, ledger)
    conflicts = check_contradictions(ledger)

    if conflicts:
        verdict = "ESCALATE_CONFLICT"
    elif struck_claims > 0 and not any_risk_survives:
        verdict = "BLOCKED_UNVERIFIED"
    else:
        verdict = "APPROVED_FOR_REVIEW"

    if verdict == "ESCALATE_CONFLICT":
        recommendation = ESCALATE_RECOMMENDATION
    else:
        recommendation = coordinator.extract_recommendation(annotated_md)

    explanation = model_call(
        prompts.VERIFIER_EXPLAIN_INSTRUCTIONS,
        json.dumps(
            {
                "verdict": verdict,
                "conflicts": conflicts,
                "struck_claims": struck_claims,
                "context_item_count": context_item_count,
            }
        ),
    )

    return {
        "annotated_summary_md": annotated_md,
        "verdict": verdict,
        "conflicts": conflicts,
        "struck_claims": struck_claims,
        "recommendation": recommendation,
        "verifier_note": explanation,
        "context_item_count": context_item_count,
    }
