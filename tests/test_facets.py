"""Offline check for facets.py: model_call is stubbed, no network."""

import json

from preventnet.facets import facet_answer
from preventnet.policy import QUESTION_CATALOG
from preventnet.wire import Ledger

PHARMACY_ANSWERS = {
    "q_ph_nsaid_90d": True,
    "q_ph_metformin_active": True,
    "q_ph_interacting_pairs": True,
}


def _stub_model_call(answers):
    def call(instructions, input_text):
        return json.dumps({"answers": answers})

    return call


def test_sabotage_forces_pharmacy_answers_false_in_ledger():
    events = []
    ledger = Ledger(emit=events.append)
    facet_answer(
        role="pharmacy",
        patient_id="P001",
        questions=QUESTION_CATALOG,
        model_call=_stub_model_call(PHARMACY_ANSWERS),
        ledger=ledger,
        emit=events.append,
        sabotage=True,
    )
    by_field = {item.field: item.value for item in ledger.items}
    assert by_field["nsaid_dispensed_90d"] is False
    assert by_field["interacting_dispense_flag"] is False
    assert any(e.get("preventnet") == "sabotage_injected" for e in events)


def test_marketing_question_refused_before_model_even_if_model_would_answer_null():
    events = []
    ledger = Ledger(emit=events.append)
    seen_payloads = []

    def call(instructions, input_text):
        seen_payloads.append(input_text)
        # A model with no marketing data answers null: the refusal must still be recorded.
        return json.dumps({"answers": {"q_gp_conditions": "hypertension", "q_gp_marketing_segment": None}})

    facet_answer(
        role="gp",
        patient_id="P001",
        questions=QUESTION_CATALOG,
        model_call=call,
        ledger=ledger,
        emit=events.append,
    )
    assert "q_gp_marketing_segment" not in seen_payloads[0]
    assert not any(item.field == "insurance_marketing_segment" for item in ledger.items)
    assert [r.question_id for r in ledger.refusals] == ["q_gp_marketing_segment"]
