"""Offline checks for coordinator.py: model_call is stubbed, no network."""

import json

from preventnet import coordinator
from preventnet.policy import QUESTION_CATALOG
from preventnet.wire import Ledger


def test_plan_questions_forces_full_seeded_catalog_regardless_of_model_pick():
    def stub_call(instructions, input_text):
        return json.dumps(["q_gp_conditions"])  # model picks just one

    final_ids, picked = coordinator.plan_questions(stub_call, "P001")
    assert picked == ["q_gp_conditions"]
    assert set(final_ids) == {q["id"] for q in QUESTION_CATALOG}


def test_plan_questions_survives_non_json_model_output():
    final_ids, picked = coordinator.plan_questions(lambda i, t: "not json", "P001")
    assert set(final_ids) == {q["id"] for q in QUESTION_CATALOG}
    assert picked == []


def test_extract_recommendation_pulls_text_between_headers():
    md = (
        "RISKS\n- Something [L1]\n\n"
        "RECOMMENDATION\nInvite patient for a medication review before imaging.\n\n"
        "CONFIDENCE\nhigh"
    )
    assert coordinator.extract_recommendation(md) == "Invite patient for a medication review before imaging."


def test_reason_over_ledger_input_is_only_ledger_items():
    ledger = Ledger(emit=lambda e: None)
    seen = {}

    def stub_streamed(instructions, input_text):
        seen["input"] = json.loads(input_text)
        return "RISKS\n[]\nRECOMMENDATION\nnone\nCONFIDENCE\nlow"

    coordinator.reason_over_ledger(stub_streamed, ledger)
    assert set(seen["input"].keys()) == {"ledger", "refusals"}
