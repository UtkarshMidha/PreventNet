"""Per-role facet phase: one isolated model call over one role's own records.

facet_answer is the only place that loads data/{role}/records.jsonl. It calls the
model with exactly that role's records, checks each answer against policy,
and writes permitted answers (and refusals) straight into the ledger via the
wire allowlist. Nothing else ever sees the raw records.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import policy, prompts, rules
from .wire import Ledger, Refusal, WireItem, strip_for_wire

DATA_DIR = Path(__file__).parent / "data"

# Sabotage targets: overridden deterministically so the demo can never flake.
SABOTAGE_ROLE = "pharmacy"
SABOTAGE_QUESTION_IDS = {"q_ph_nsaid_90d", "q_ph_interacting_pairs"}


def load_records(role: str, patient_id: str) -> list[dict]:
    path = DATA_DIR / role / "records.jsonl"
    with path.open() as f:
        records = [json.loads(line) for line in f if line.strip()]
    return [r for r in records if r["patient_id"] == patient_id]


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def _parse_answers(text: str) -> dict:
    data = json.loads(_strip_code_fences(text))
    return data["answers"]


def _call_model_for_answers(model_call: Callable[[str, str], str], instructions: str, input_text: str) -> dict:
    output = model_call(instructions, input_text)
    try:
        return _parse_answers(output)
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    retry_output = model_call(instructions + prompts.FACET_JSON_RETRY_SUFFIX, input_text)
    try:
        return _parse_answers(retry_output)
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise ValueError(f"facet model did not return valid JSON after one retry: {e}") from e


def split_by_policy(role: str, questions: list[dict]) -> tuple[list[dict], list[Refusal]]:
    """Policy runs BEFORE anyone looks at records: refused questions are never answered."""
    permitted, refusals = [], []
    for q in questions:
        if q["role"] != role:
            continue
        reason = policy.check(role, q["field"], q["purpose"])
        if reason is None:
            permitted.append(q)
        else:
            refusals.append(Refusal(question_id=q["id"], role=role, reason=reason))
    return permitted, refusals


def disclose(role: str, permitted: list[dict], answers: dict, sabotage: bool = False) -> tuple[list[WireItem], bool]:
    """Turn answers to permitted questions into wire items. Returns (items, sabotaged)."""
    answers = dict(answers)
    sabotaged = sabotage and role == SABOTAGE_ROLE
    if sabotaged:
        for qid in SABOTAGE_QUESTION_IDS:
            answers[qid] = False

    items = []
    for q in permitted:
        value = answers.get(q["id"])
        if value is None:
            continue  # records didn't determine an answer
        items.append(
            strip_for_wire(
                {"field": q["field"], "value": value, "purpose": q["purpose"], "source": role, "ts": _now_ts()}
            )
        )
    return items, sabotaged


def facet_answer(
    role: str,
    patient_id: str,
    questions: list[dict],
    model_call: Callable[[str, str], str],
    ledger: Ledger,
    emit: Callable[[dict], None],
    sabotage: bool = False,
) -> int:
    """Run one facet phase; write permitted answers/refusals into `ledger`.

    Returns the number of this patient's records scanned in data/{role}/records.jsonl.
    """
    records = load_records(role, patient_id)
    permitted, refusals = split_by_policy(role, questions)
    for refusal in refusals:
        ledger.refuse(refusal)
    if not permitted:
        return len(records)

    instructions = prompts.FACET_PERSONA_TEMPLATE.format(role=role)
    payload = {
        "questions": [
            {
                "id": q["id"],
                "field": q["field"],
                "text": q["text"],
                "value_type": q["value_type"],
                **({"enum": q["enum"]} if "enum" in q else {}),
            }
            for q in permitted
        ],
        "records": records,
    }
    answers = _call_model_for_answers(model_call, instructions, json.dumps(payload))
    _record(role, permitted, answers, ledger, emit, sabotage)
    return len(records)


def facet_rules(
    role: str,
    patient_id: str,
    questions: list[dict],
    ledger: Ledger,
    emit: Callable[[dict], None],
    sabotage: bool = False,
) -> int:
    """Fast path: the department answers with auditable rules instead of a model call."""
    records = load_records(role, patient_id)
    permitted, refusals = split_by_policy(role, questions)
    for refusal in refusals:
        ledger.refuse(refusal)
    _record(role, permitted, rules.answer(role, permitted, records), ledger, emit, sabotage)
    return len(records)


def _record(role: str, permitted: list[dict], answers: dict, ledger: Ledger, emit, sabotage: bool) -> None:
    items, sabotaged = disclose(role, permitted, answers, sabotage)
    if sabotaged:
        emit({"type": "preventnet.sabotage_injected", "preventnet": "sabotage_injected", "role": role})
    for item in items:
        ledger.append(item)
