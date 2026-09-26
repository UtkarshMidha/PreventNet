"""Offline check of the AgentApp's ledger-only mode with a stubbed model client (no SuperGrid)."""

import json
from types import SimpleNamespace

from flwr.app import Context, RecordDict

from preventnet import agent_app

SUMMARY = "## RISKS\n- Reduced eGFR before contrast imaging [L1] [L2]\n\n## RECOMMENDATION\nPre-imaging review. [L1]\n\n## CONFIDENCE\nhigh"


class FakeResponses:
    def __init__(self):
        self.calls = []

    def create(self, model, input, instructions, stream=False):
        self.calls.append({"model": model, "input": input, "stream": stream})
        if not stream:
            return SimpleNamespace(output_text="Plain-language verdict note.")
        return iter([SimpleNamespace(type="response.output_text.delta", delta=SUMMARY,
                                     to_dict=lambda: {"type": "response.output_text.delta"})])


def test_ledger_mode_reads_no_records_and_uses_endeavor(monkeypatch, capsys):
    fake = FakeResponses()
    monkeypatch.setattr(agent_app, "OpenAI", lambda **kw: SimpleNamespace(responses=fake))
    monkeypatch.setattr(agent_app, "facet_answer", lambda **kw: (_ for _ in ()).throw(AssertionError("read records")))
    monkeypatch.setenv("FLWR_RUNTIME_BASE_URL", "http://stub")
    monkeypatch.setenv("FLWR_RUNTIME_API_KEY", "stub")

    review = {
        "patient_id": "P002", "sabotage": False, "facet_status": {"gp": "ok"},
        "local_views": {"gp": {"records_scanned": 2}},
        "ledger": [
            {"id": "L1", "field": "egfr_band", "value": "moderately_reduced", "purpose": "renal-risk-review",
             "source": "lab", "ts": "2026-06-20T00:00:00Z"},
            {"id": "L2", "field": "contrast_imaging_planned_90d", "value": True, "purpose": "renal-risk-review",
             "source": "gp", "ts": "2026-06-20T00:00:00Z"},
        ],
        "refusals": [{"question_id": "q_gp_marketing_segment", "role": "gp", "reason": "out_of_purpose"}],
    }
    events = []
    agent = SimpleNamespace(events=SimpleNamespace(emit=events.append))
    context = Context(run_id=1, node_id=1, node_config={}, state=RecordDict(), run_config={
        "agent.input": "review", "patient.id": "P001", "demo.sabotage": False,
        "model.ref": "flower-endeavor-v1.0", "model.fallback": "openai/gpt-5.6-sol",
        "review.ledger": json.dumps(review),
    })

    agent_app.app(agent, context)

    line = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("PREVENTNET_RESULT "))
    payload = json.loads(line[len("PREVENTNET_RESULT "):])
    assert payload["patient_id"] == "P002"
    assert payload["model_used"] == "flower-endeavor-v1.0"
    assert payload["verdict"] == "APPROVED_FOR_REVIEW"
    assert payload["context_item_count"] == 2 and len(payload["refusals"]) == 1
    assert payload["recommendation"] == "Pre-imaging review. [L1]"
    assert [c["model"] for c in fake.calls] == ["flower-endeavor-v1.0"] * 2  # reason (streamed) + verifier note
    assert all(isinstance(e.get("type"), str) and e["type"] for e in events)


def test_full_mode_trains_federated_model_and_cites_it(monkeypatch, capsys):
    answers = {
        "gp": {"q_gp_conditions": "hypertension", "q_gp_planned_imaging": True, "q_gp_reported_otc": True},
        "pharmacy": {"q_ph_nsaid_90d": True, "q_ph_metformin_active": True, "q_ph_interacting_pairs": True},
        "lab": {"q_lab_egfr_band": "moderately_reduced", "q_lab_egfr_trend": "declining", "q_lab_hba1c_band": "prediabetic"},
    }

    class Responses:
        def create(self, model, input, instructions, stream=False):
            if stream:
                return iter([SimpleNamespace(type="response.output_text.delta", delta=SUMMARY,
                                             to_dict=lambda: {"type": "response.output_text.delta"})])
            for role, a in answers.items():
                if f"You are the {role} data-steward" in instructions:
                    return SimpleNamespace(output_text=json.dumps({"answers": a}))
            return SimpleNamespace(output_text="[]" if "planning a review" in instructions else "note")

    monkeypatch.setattr(agent_app, "OpenAI", lambda **kw: SimpleNamespace(responses=Responses()))
    monkeypatch.setenv("FLWR_RUNTIME_BASE_URL", "http://stub")
    monkeypatch.setenv("FLWR_RUNTIME_API_KEY", "stub")
    events = []
    agent = SimpleNamespace(events=SimpleNamespace(emit=events.append))
    context = Context(run_id=1, node_id=1, node_config={}, state=RecordDict(), run_config={
        "agent.input": "review", "patient.id": "P001", "demo.sabotage": False, "fl.rounds": 20, "fl.lr": 0.5,
        "model.ref": "flower-endeavor-v1.0", "model.fallback": "openai/gpt-5.6-sol", "review.ledger": ""})

    agent_app.app(agent, context)

    out = capsys.readouterr().out
    payload = json.loads(next(l for l in out.splitlines() if l.startswith("PREVENTNET_RESULT "))[18:])
    assert "PREVENTNET_FL round 20/20" in out
    assert payload["fl"]["source"] == "supergrid" and len(payload["fl"]["history"]) == 20
    assert payload["fl"]["test_auc"] > 0.7
    assert payload["fl_risk"]["band"] == "moderate"
    fl_items = [i for i in payload["ledger"] if i["field"] == "fl_diabetes_risk_band"]
    assert fl_items == [dict(fl_items[0], value="moderate", source="federated-model")]
    assert payload["refusals"] == [{"question_id": "q_gp_marketing_segment", "role": "gp", "reason": "out_of_purpose"}]
    assert payload["coordinator_raw_md"] == SUMMARY
    assert any(e["type"] == "preventnet.fl_round" for e in events)
    assert "residual" not in out


def _fast_context(sabotage=False, budget=12):
    return Context(run_id=1, node_id=1, node_config={}, state=RecordDict(), run_config={
        "agent.input": "review", "patient.id": "P001", "demo.sabotage": sabotage, "demo.fast": True,
        "fl.rounds": 30, "fl.lr": 0.5, "model.ref": "flower-endeavor-v1.0", "model.fallback": "openai/gpt-5.6-sol",
        "model.budget_s": budget, "review.ledger": ""})


def _stream(text):
    return iter([SimpleNamespace(type="response.created", to_dict=lambda: {"type": "response.created"}),
                 SimpleNamespace(type="response.output_text.delta", delta=text,
                                 to_dict=lambda: {"type": "response.output_text.delta"}),
                 SimpleNamespace(type="response.completed", to_dict=lambda: {"type": "response.completed"})])


def _run(monkeypatch, capsys, responses, context):
    monkeypatch.setattr(agent_app, "OpenAI", lambda **kw: SimpleNamespace(responses=responses))
    monkeypatch.setenv("FLWR_RUNTIME_BASE_URL", "http://stub")
    monkeypatch.setenv("FLWR_RUNTIME_API_KEY", "stub")
    events = []
    agent_app.app(SimpleNamespace(events=SimpleNamespace(emit=events.append)), context)
    out = capsys.readouterr().out
    payload = json.loads(next(l for l in out.splitlines() if l.startswith("PREVENTNET_RESULT "))[18:])
    return payload, events, out


def test_fast_mode_makes_one_model_call_and_no_model_reads_records(monkeypatch, capsys):
    calls = []

    class Responses:
        def create(self, model, input, instructions, stream=False, **kw):
            calls.append((model, stream, kw))
            assert stream and "Be brief" in instructions, "the only call is the coordinator's short assessment"
            assert "records" not in json.loads(input), "no raw records may reach the model"
            return _stream(SUMMARY)

    payload, events, out = _run(monkeypatch, capsys, Responses(), _fast_context(sabotage=True))
    assert len(calls) == 1 and payload["model_calls"] == 1 and payload["model_used"] == "flower-endeavor-v1.0"
    assert "timeout" in calls[0][2]  # the Endeavor call carries the time budget
    assert [(i["id"], i["source"]) for i in payload["ledger"][:9]] == [
        (f"L{n}", role) for n, role in enumerate(["gp"] * 3 + ["pharmacy"] * 3 + ["lab"] * 3, start=1)]
    assert payload["refusals"] == [{"question_id": "q_gp_marketing_segment", "role": "gp", "reason": "out_of_purpose"}]
    assert payload["verdict"] == "ESCALATE_CONFLICT" and "disagree" in payload["verifier_note"]
    assert payload["fl"]["rounds"] == 30 and payload["model_fallback"] is None
    assert "PREVENTNET_PHASE facets t=" in out and "coordinator_plan" not in out
    types = [e["type"] for e in events]
    assert "response.output_text.delta" not in types  # token events are not forwarded in fast mode
    assert "preventnet.coordinator_output" in types
    assert types.count("preventnet.sabotage_injected") == 1


def test_fast_mode_switches_model_when_endeavor_exceeds_budget(monkeypatch, capsys):
    import httpx
    import openai

    calls = []

    class Responses:
        def create(self, model, input, instructions, stream=False, **kw):
            calls.append(model)
            if model == "flower-endeavor-v1.0":
                raise openai.APITimeoutError(request=httpx.Request("POST", "http://stub"))
            return _stream(SUMMARY)

    payload, events, _ = _run(monkeypatch, capsys, Responses(), _fast_context(budget=3))
    assert calls == ["flower-endeavor-v1.0", "openai/gpt-5.6-sol"]
    assert payload["model_used"] == "openai/gpt-5.6-sol" and payload["model_fallback"] == "timeout"
    assert payload["model_calls"] == 2 and payload["verdict"] == "APPROVED_FOR_REVIEW"
    assert any(e["type"] == "preventnet.model_fallback" for e in events)
