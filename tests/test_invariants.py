"""Offline invariant tests: no model calls."""

import json
from pathlib import Path

import pytest

from preventnet import policy, verifier
from preventnet.wire import Ledger, WireViolation, strip_for_wire

DATA_DIR = Path(__file__).parent.parent / "preventnet" / "data"


def _wire(field, value, purpose, source):
    return strip_for_wire(
        {"field": field, "value": value, "purpose": purpose, "source": source, "ts": "2026-06-01T00:00:00Z"}
    )


# 1. strip_for_wire raises on forbidden/unknown keys and oversized strings.
@pytest.mark.parametrize("bad_key", ["record_id", "text", "note", "name", "dob"])
def test_strip_for_wire_rejects_blocked_keys(bad_key):
    raw = {"field": "x", "value": "y", "purpose": "p", "source": "gp", "ts": "t", bad_key: "z"}
    with pytest.raises(WireViolation):
        strip_for_wire(raw)


def test_strip_for_wire_rejects_unknown_key():
    raw = {"field": "x", "value": "y", "purpose": "p", "source": "gp", "ts": "t", "extra": "z"}
    with pytest.raises(WireViolation):
        strip_for_wire(raw)


def test_strip_for_wire_rejects_oversized_string():
    raw = {
        "field": "x",
        "value": "y" * 41,
        "purpose": "p",
        "source": "gp",
        "ts": "t",
    }
    with pytest.raises(WireViolation):
        strip_for_wire(raw)


def test_strip_for_wire_accepts_clean_item():
    raw = {"field": "egfr_band", "value": "moderately_reduced", "purpose": "renal-risk-review", "source": "lab", "ts": "2026-06-01"}
    item = strip_for_wire(raw)
    assert item.field == "egfr_band"
    assert item.value == "moderately_reduced"


# 2. No raw record field name ever appears in a serialized ledger built from
# a stubbed facet answer set.
def test_ledger_never_carries_raw_record_fields():
    events = []
    ledger = Ledger(emit=events.append)

    # Simulate facets.py building wire items from model answers, never from
    # the raw jsonl records directly.
    stubbed_answers = [
        {"field": "egfr_band", "value": "moderately_reduced", "purpose": "renal-risk-review", "source": "lab", "ts": "2026-06-01"},
        {"field": "metformin_active", "value": True, "purpose": "renal-risk-review", "source": "pharmacy", "ts": "2026-06-01"},
    ]
    for raw in stubbed_answers:
        ledger.append(strip_for_wire(raw))

    serialized = ledger.to_json()
    for forbidden in ("record_id", "note", "dob", "name", "condition", "drug", "referral_type"):
        assert forbidden not in serialized


# 3. Policy refuses the marketing question for all roles.
def test_marketing_question_refused_for_every_role():
    marketing_qs = [q for q in policy.QUESTION_CATALOG if q["purpose"] == "marketing"]
    assert marketing_qs, "expected a marketing question in the catalog"
    for q in marketing_qs:
        reason = policy.check(q["role"], q["field"], q["purpose"])
        assert reason == policy.OUT_OF_PURPOSE


def test_non_marketing_catalog_questions_are_permitted():
    for q in policy.QUESTION_CATALOG:
        if q["purpose"] == "marketing":
            continue
        assert policy.check(q["role"], q["field"], q["purpose"]) is None


# 6. Fixtures sanity.
def _load(role):
    path = DATA_DIR / role / "records.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_p001_exists_in_all_three_facets():
    for role in ("gp", "pharmacy", "lab"):
        records = _load(role)
        assert any(r["patient_id"] == "P001" for r in records), f"P001 missing from {role}.jsonl"


def test_no_single_facet_contains_all_three_legs_of_case_a():
    gp = _load("gp")
    pharmacy = _load("pharmacy")
    lab = _load("lab")

    assert not any(r["patient_id"] == "P001" and r["kind"] == "dispense" for r in gp)
    assert not any(r["patient_id"] == "P001" and "egfr_band" in r for r in gp)
    assert not any(r["patient_id"] == "P001" and "egfr_band" in r for r in pharmacy)
    assert not any(r["patient_id"] == "P001" and r["kind"] == "dispense" for r in lab)


# 4. Contradiction rule fires on the sabotage value pattern; does not fire on
# the honest pattern.
def test_contradiction_rule_fires_on_sabotage_pattern():
    ledger = Ledger(emit=lambda e: None)
    ledger.append(_wire("patient_reported_otc_nsaid", True, "medication-safety-review", "gp"))
    ledger.append(_wire("nsaid_dispensed_90d", False, "medication-safety-review", "pharmacy"))  # sabotaged

    conflicts = verifier.check_contradictions(ledger)
    assert any(c["rule"] == "otc_nsaid_vs_pharmacy_dispense" for c in conflicts)


def test_contradiction_rule_silent_on_honest_pattern():
    ledger = Ledger(emit=lambda e: None)
    ledger.append(_wire("patient_reported_otc_nsaid", True, "medication-safety-review", "gp"))
    ledger.append(_wire("nsaid_dispensed_90d", True, "medication-safety-review", "pharmacy"))

    assert verifier.check_contradictions(ledger) == []


def test_second_contradiction_rule_fires_on_inconsistent_dispense_story():
    ledger = Ledger(emit=lambda e: None)
    ledger.append(_wire("patient_reported_otc_nsaid", True, "medication-safety-review", "gp"))
    ledger.append(_wire("metformin_active", True, "renal-risk-review", "pharmacy"))
    ledger.append(_wire("interacting_dispense_flag", False, "medication-safety-review", "pharmacy"))

    conflicts = verifier.check_contradictions(ledger)
    assert any(c["rule"] == "metformin_nsaid_interaction_not_flagged" for c in conflicts)


# 5. Verifier strikes an uncited claim; verdict transitions (0 conflicts ->
# APPROVED, >=1 -> ESCALATE).
def test_verifier_strikes_uncited_claim_and_approves_when_no_conflicts():
    ledger = Ledger(emit=lambda e: None)
    cited = ledger.append(_wire("egfr_band", "moderately_reduced", "renal-risk-review", "lab"))

    md = (
        f"RISKS\n"
        f"- Declining renal function [{cited.id}]\n"
        f"- Unsupported claim with no citation\n"
        f"RECOMMENDATION\nInvite for medication review.\nCONFIDENCE\nmedium"
    )
    result = verifier.verify(md, ledger, model_call=lambda i, t: "explanation")

    assert result["struck_claims"] == 1
    assert "~~- Unsupported claim with no citation~~" in result["annotated_summary_md"]
    assert result["verdict"] == "APPROVED_FOR_REVIEW"
    assert result["conflicts"] == []


def test_verifier_handles_markdown_headers_like_real_model_output():
    ledger = Ledger(emit=lambda e: None)
    cited = ledger.append(_wire("egfr_band", "moderately_reduced", "renal-risk-review", "lab"))
    md = (
        f"## RISKS\n\n- **Renal:** reduced eGFR. [{cited.id}]\n- **Invented:** no source. [L99]\n\n"
        f"## RECOMMENDATION\n\n- **Review medication** before imaging. [{cited.id}]\n\n"
        f"## CONFIDENCE\n\n**High**"
    )
    result = verifier.verify(md, ledger, model_call=lambda i, t: "explanation")
    assert result["struck_claims"] == 1
    assert result["verdict"] == "APPROVED_FOR_REVIEW"
    assert result["recommendation"] == f"Review medication before imaging. [{cited.id}]"


def test_verifier_blocks_summary_without_risks_section():
    ledger = Ledger(emit=lambda e: None)
    ledger.append(_wire("egfr_band", "moderately_reduced", "renal-risk-review", "lab"))
    result = verifier.verify("Everything looks fine, start new drug X.", ledger, model_call=lambda i, t: "x")
    assert result["verdict"] == "BLOCKED_UNVERIFIED"


def test_verifier_escalates_on_conflict_regardless_of_citations():
    ledger = Ledger(emit=lambda e: None)
    otc = ledger.append(_wire("patient_reported_otc_nsaid", True, "medication-safety-review", "gp"))
    dispensed = ledger.append(_wire("nsaid_dispensed_90d", False, "medication-safety-review", "pharmacy"))

    md = f"RISKS\n- Some risk [{otc.id}] [{dispensed.id}]\nRECOMMENDATION\noriginal text\nCONFIDENCE\nhigh"
    result = verifier.verify(md, ledger, model_call=lambda i, t: "explanation")

    assert result["verdict"] == "ESCALATE_CONFLICT"
    assert len(result["conflicts"]) == 1
    assert result["recommendation"] == verifier.ESCALATE_RECOMMENDATION
