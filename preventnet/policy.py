"""Closed question catalog and per-role, per-purpose disclosure policy."""

from __future__ import annotations

from typing import Optional

# None of these fields exist in the Pima dataset. They are answered from each
# department's synthetic review records (data/<role>/records.jsonl). The Pima
# columns only feed the federated diabetes model (vfl.py), whose output enters
# the ledger as one extra fact: fl_diabetes_risk_band.
FL_FACT = {
    "field": "fl_diabetes_risk_band", "label": "Diabetes risk", "role": "federated-model", "purpose": "metabolic-screening",
    "derived_from": "Vertical federated logistic regression over the Pima columns held by GP, Lab and Pharmacy",
    "why": "Adds a population-trained diabetes-risk estimate that no single department could compute alone.",
}

# Closed catalog: only these questions are ever asked. Each role only ever
# sees its own role's questions (enforced by facets.py, not by this file).
QUESTION_CATALOG = [
    {
        "id": "q_gp_conditions",
        "label": 'Relevant long-term condition',
        "role": "gp",
        "field": "chronic_conditions",
        "purpose": "renal-risk-review",
        "text": "What relevant chronic condition, if any, does the patient have?",
        "derived_from": "GP diagnosis records (kind=diagnosis, field 'condition')",
        "why": 'Hypertension and diabetes are the main causes of chronic kidney disease; they raise the chance that contrast dye or NSAIDs injure the kidneys.',
        "value_type": "enum",
        "enum": ["hypertension", "type2_diabetes", "none_relevant"],
    },
    {
        "id": "q_gp_planned_imaging",
        "label": 'Contrast scan planned (next 90 days)',
        "role": "gp",
        "field": "contrast_imaging_planned_90d",
        "purpose": "renal-risk-review",
        "text": "Is contrast imaging planned for this patient in the next 90 days?",
        "derived_from": "GP referral records (kind=referral, referral_type contains 'contrast')",
        "why": 'Iodinated contrast dye can cause acute kidney injury in people with weak kidneys, and metformin is often paused around it.',
        "value_type": "bool",
    },
    {
        "id": "q_gp_reported_otc",
        "label": 'Takes over-the-counter painkillers (NSAIDs)',
        "role": "gp",
        "field": "patient_reported_otc_nsaid",
        "purpose": "medication-safety-review",
        "text": "Has the patient self-reported over-the-counter NSAID use?",
        "derived_from": 'Free-text GP visit notes mentioning ibuprofen/naproxen/NSAID (the note itself never leaves)',
        "why": 'Over-the-counter painkillers are invisible to pharmacy dispensing systems, yet NSAIDs reduce kidney blood flow.',
        "value_type": "bool",
    },
    {
        "id": "q_ph_nsaid_90d",
        "label": 'NSAID painkiller dispensed (last 90 days)',
        "role": "pharmacy",
        "field": "nsaid_dispensed_90d",
        "purpose": "medication-safety-review",
        "text": "Has an NSAID been dispensed to this patient in the last 90 days?",
        "derived_from": 'Pharmacy dispensing records: an NSAID dispensed within 90 days of the review date',
        "why": 'Confirms NSAID exposure from a second, independent source; disagreement with the GP is a data-integrity signal.',
        "value_type": "bool",
    },
    {
        "id": "q_ph_metformin_active",
        "label": 'Currently taking metformin',
        "role": "pharmacy",
        "field": "metformin_active",
        "purpose": "renal-risk-review",
        "text": "Is the patient on active metformin therapy?",
        "derived_from": 'Pharmacy dispensing records: metformin dispensed within 90 days',
        "why": 'Metformin plus reduced kidney function plus contrast raises the risk of lactic acidosis; guidelines call for a pre-imaging review.',
        "value_type": "bool",
    },
    {
        "id": "q_ph_interacting_pairs",
        "label": 'Drug-interaction alert raised',
        "role": "pharmacy",
        "field": "interacting_dispense_flag",
        "purpose": "medication-safety-review",
        "text": "Did the dispensing system flag an interacting drug pair for this patient?",
        "derived_from": 'Pharmacy interaction-check records (kind=interaction_flag, triggered=true)',
        "why": "Shows the pharmacy's own safety system already saw a risky drug combination.",
        "value_type": "bool",
    },
    {
        "id": "q_lab_egfr_band",
        "label": 'Kidney function (eGFR)',
        "role": "lab",
        "field": "egfr_band",
        "purpose": "renal-risk-review",
        "text": "What is the patient's most recent eGFR band?",
        "derived_from": 'Most recent lab eGFR result, stored as a clinical band (normal to severely_reduced)',
        "why": 'eGFR measures kidney filtering; a reduced band is the core vulnerability behind this preventable harm.',
        "value_type": "enum",
        "enum": ["normal", "mildly_reduced", "moderately_reduced", "severely_reduced"],
    },
    {
        "id": "q_lab_egfr_trend",
        "label": 'Kidney function trend (2 years)',
        "role": "lab",
        "field": "egfr_trend_24m",
        "purpose": "renal-risk-review",
        "text": "What is the patient's eGFR trend over the last 24 months?",
        "derived_from": 'First vs. latest eGFR band over 24 months (worse = declining)',
        "why": 'A falling trend means the kidneys are getting weaker, which makes a new insult more dangerous.',
        "value_type": "enum",
        "enum": ["stable", "declining", "improving"],
    },
    {
        "id": "q_lab_hba1c_band",
        "label": 'Blood sugar (HbA1c)',
        "role": "lab",
        "field": "hba1c_band",
        "purpose": "metabolic-screening",
        "text": "What is the patient's most recent HbA1c band?",
        "derived_from": 'Most recent lab HbA1c result, stored as a band (normal / prediabetic / diabetic)',
        "why": 'HbA1c shows long-term blood sugar; prediabetes is the window where screening and lifestyle change prevent diabetes.',
        "value_type": "enum",
        "enum": ["normal", "prediabetic", "diabetic"],
    },
    {
        # Exists ONLY to be refused: every role's disclosure policy denies the
        # "marketing" purpose, so the refusal path is real in every demo run.
        "id": "q_gp_marketing_segment",
        "label": 'Insurance marketing segment',
        "role": "gp",
        "field": "insurance_marketing_segment",
        "purpose": "marketing",
        "text": "What insurance marketing segment is this patient in?",
        "derived_from": 'Nothing: no department holds this, and policy refuses it before any record is read',
        "why": 'Exists only to prove that out-of-purpose questions (marketing) are refused on every run.',
        "value_type": "enum",
        "enum": ["premium", "standard", "basic"],
    },
]

# Human-readable names for everything that appears on screen. The payload carries
# these so every UI (including the standalone viewer) shows the same words.
VALUE_LABELS = {
    "hypertension": "High blood pressure", "type2_diabetes": "Type 2 diabetes", "none_relevant": "None relevant",
    "normal": "Normal", "mildly_reduced": "Mildly reduced", "moderately_reduced": "Moderately reduced",
    "severely_reduced": "Severely reduced", "stable": "Stable", "declining": "Declining", "improving": "Improving",
    "prediabetic": "Prediabetic range", "diabetic": "Diabetic range",
    "low": "Low", "moderate": "Moderate", "high": "High",
}
PURPOSE_LABELS = {
    "renal-risk-review": "Kidney risk review", "medication-safety-review": "Medication safety",
    "metabolic-screening": "Diabetes screening", "marketing": "Marketing",
}
# One application, three data sources: the GP system holds health diagnostics, the
# pharmacy system holds prescription logs, the lab system holds lab reports.
ROLE_LABELS = {"gp": "Health Diagnostics", "pharmacy": "Prescription Logs", "lab": "Lab Reports",
               "federated-model": "Risk Model"}

# Refusal reasons (closed set).
OUT_OF_PURPOSE = "out_of_purpose"
NOT_HELD = "not_held"
BELOW_DISCLOSURE_THRESHOLD = "below_disclosure_threshold"


def _build_allowed() -> dict[str, dict[str, set[str]]]:
    allowed: dict[str, dict[str, set[str]]] = {}
    for q in QUESTION_CATALOG:
        if q["purpose"] == "marketing":
            continue  # never permitted for any role
        allowed.setdefault(q["role"], {}).setdefault(q["purpose"], set()).add(q["field"])
    return allowed


ALLOWED = _build_allowed()


def check(role: str, field: str, purpose: str) -> Optional[str]:
    """Return a refusal reason if disclosure isn't permitted, else None (OK)."""
    if field in ALLOWED.get(role, {}).get(purpose, set()):
        return None
    return OUT_OF_PURPOSE


REASON_LABELS = {
    OUT_OF_PURPOSE: "Outside the purpose of this review",
    NOT_HELD: "Not held by this department",
    BELOW_DISCLOSURE_THRESHOLD: "Too sensitive to share",
}


def display_labels() -> dict:
    fields = {q["field"]: q["label"] for q in QUESTION_CATALOG} | {FL_FACT["field"]: FL_FACT["label"]}
    return {
        "fields": fields,
        "questions": {q["id"]: q["label"] for q in QUESTION_CATALOG},
        "values": VALUE_LABELS,
        "purposes": PURPOSE_LABELS,
        "roles": ROLE_LABELS,
        "reasons": REASON_LABELS,
    }
