"""Offline integration test of the four local deployments (in-process, ephemeral ports)."""

import json
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from local import dashboard, site
from preventnet import rules
from local.net import post_json
from preventnet.vfl import SiteModel
from preventnet.agent_app import parse_review_ledger
from preventnet.policy import QUESTION_CATALOG

SITES_DIR = Path(__file__).parent.parent / "preventnet" / "data"
SECRET = "test-secret-not-for-coordinator"
RAW_ONLY_STRINGS = ["ibuprofen", "knee pain", "metformin\"", "contrast_ct", "cetirizine", "2019-03-11"]


def _serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture(scope="module")
def stack():
    lab = site.make_server("lab", 0, SITES_DIR / "lab", SECRET, [])
    pharmacy = site.make_server("pharmacy", 0, SITES_DIR / "pharmacy", SECRET, [])
    urls = {"lab": _serve(lab), "pharmacy": _serve(pharmacy)}
    gp = site.make_server("gp", 0, SITES_DIR / "gp", SECRET, list(urls.values()))
    urls["gp"] = _serve(gp)
    coord = dashboard.make_server(urls, 0)
    base = _serve(coord)
    yield {"base": base, "urls": urls}
    for s in (lab, pharmacy, gp, coord):
        s.shutdown()


def test_rules_reproduce_case_a_from_each_sites_own_records():
    def answers(role):
        records = [json.loads(line) for line in (SITES_DIR / role / "records.jsonl").read_text().splitlines()]
        records = [r for r in records if r["patient_id"] == "P001"]
        return rules.answer(role, [q for q in QUESTION_CATALOG if q["role"] == role], records)

    assert answers("gp") == {"q_gp_conditions": "hypertension", "q_gp_planned_imaging": True,
                             "q_gp_reported_otc": True, "q_gp_marketing_segment": None}
    assert answers("pharmacy") == {"q_ph_nsaid_90d": True, "q_ph_metformin_active": True,
                                   "q_ph_interacting_pairs": True}
    assert answers("lab") == {"q_lab_egfr_band": "moderately_reduced", "q_lab_egfr_trend": "declining",
                              "q_lab_hba1c_band": "prediabetic"}


def test_masks_cancel_and_nonce_reuse_is_refused():
    models = [SiteModel(r, SITES_DIR / r / "cohort.jsonl", SECRET) for r in ("gp", "lab", "pharmacy")]
    for m in models:
        m.w = np.random.default_rng(0).normal(size=len(m.w))
    masked = np.sum([m.forward("train", "n" * 16) for m in models], axis=0)
    plain = np.sum([m.X[m.split == "train"] @ m.w for m in models], axis=0)
    assert np.allclose(masked, plain)
    with pytest.raises(ValueError):
        models[0].forward("train", "n" * 16)


def test_offline_review_approves_case_a_and_leaks_no_raw_data(stack):
    payload = post_json(f"{stack['base']}/api/review", {"patient_id": "P001", "engine": "local"})
    assert payload["verdict"] == "APPROVED_FOR_REVIEW"
    assert payload["context_item_count"] == 9
    assert payload["refusals"] == [{"question_id": "q_gp_marketing_segment", "role": "gp", "reason": "out_of_purpose"}]
    assert payload["struck_claims"] == 0
    assert payload["labels"]["fields"]["egfr_band"] == "Kidney function (eGFR)"
    text = json.dumps({k: v for k, v in payload.items() if k != "labels"})  # labels are fixed UI text
    for raw in RAW_ONLY_STRINGS:
        assert raw not in text


def test_federated_training_then_review_adds_fl_fact(stack):
    fl = post_json(f"{stack['base']}/api/fl/train", {"rounds": 30, "lr": 0.5})
    assert fl["test_auc"] > 0.7 and len(fl["history"]) == 30
    assert "residual" not in json.dumps(fl)  # residuals go peer-to-peer only
    gp_summary = next(s for s in fl["dataset_summary"] if s["role"] == "gp")
    assert gp_summary["label"]["positives"] == 268 and gp_summary["rows"] == 768

    payload = post_json(f"{stack['base']}/api/review", {"patient_id": "P002", "engine": "local"})
    fl_items = [i for i in payload["ledger"] if i["source"] == "federated-model"]
    assert len(fl_items) == 1 and fl_items[0]["value"] in ("low", "moderate", "high")
    assert payload["fl_risk"]["band"] == fl_items[0]["value"]


def test_sabotaged_pharmacy_escalates_and_blocks_approval(stack):
    post_json(f"{stack['urls']['pharmacy']}/admin/sabotage", {"on": True})
    try:
        payload = post_json(f"{stack['base']}/api/review", {"patient_id": "P001", "engine": "local"})
        assert payload["verdict"] == "ESCALATE_CONFLICT"
        assert payload["sabotaged_roles"] == ["pharmacy"]
        with pytest.raises(RuntimeError, match="409"):
            post_json(f"{stack['base']}/api/approve", {"patient_id": "P001"})
    finally:
        post_json(f"{stack['urls']['pharmacy']}/admin/sabotage", {"on": False})


def test_clinician_approval_is_recorded(stack):
    post_json(f"{stack['base']}/api/review", {"patient_id": "P001", "engine": "local"})
    approval = post_json(f"{stack['base']}/api/approve", {"patient_id": "P001", "clinician": "Dr. Test"})
    assert approval["clinician"] == "Dr. Test"


def test_ledger_mode_round_trips_through_flwr_toml_override(tmp_path):
    from flwr.common.config import parse_config_args

    review = {"patient_id": "P001", "sabotage": False, "ledger": [
        {"id": "L1", "field": "egfr_band", "value": "moderately_reduced", "purpose": "renal-risk-review",
         "source": "lab", "ts": "2026-06-20T00:00:00Z"}],
        "refusals": [{"question_id": "q_gp_marketing_segment", "role": "gp", "reason": "out_of_purpose"}]}
    path = tmp_path / "review.toml"
    path.write_text("[review]\nledger = " + json.dumps(json.dumps(review)) + "\n")
    raw = parse_config_args([str(path)])["review.ledger"]

    ledger, meta = parse_review_ledger(raw, emit=lambda e: None)
    assert meta["patient_id"] == "P001"
    assert [i.id for i in ledger.items] == ["L1"] and len(ledger.refusals) == 1

    bad = dict(review, ledger=[dict(review["ledger"][0], note="patient mentions ibuprofen")])
    with pytest.raises(Exception):
        parse_review_ledger(json.dumps(bad), emit=lambda e: None)


def test_supergrid_job_streams_log_and_publishes_result(stack, tmp_path):
    coord = dashboard.Coordinator(stack["urls"], "supergrid")
    result = {"patient_id": "P001", "verdict": "APPROVED_FOR_REVIEW", "recommendation": "r", "sabotage": False}
    script = ("print('Successfully started run 42 in federation x'); "
              f"print('\\x1b[92mINFO\\x1b[0m: phase'); print('PREVENTNET_PHASE facet_gp t=1.5'); "
              "print('PREVENTNET_FL round 5/60 train_loss=0.5'); "
              f"print('PREVENTNET_RESULT ' + {json.dumps(json.dumps(result))})")
    config = tmp_path / "c.toml"
    config.write_text("")
    job = dashboard.Job("supergrid", "P001", [sys.executable, "-c", script], str(config), coord._finish_full_run)
    for _ in range(100):
        if job.status != "running":
            break
        time.sleep(0.05)
    view = job.view()
    assert view["status"] == "done", view
    assert view["run_id"] == "42"
    assert view["steps"] == ["submitted", "started", "facet_gp", "done"]
    assert len(view["step_times"]) == 4 and view["step_times"] == sorted(view["step_times"])
    assert view["fl_progress"] == "round 5/60 train_loss=0.5"
    assert view["log"] == []  # raw output is only exposed on failure
    assert "INFO: phase" in job.log
    assert coord.latest["verdict"] == "APPROVED_FOR_REVIEW" and coord.latest["run_id"] == "42"
    assert not config.exists()


def test_demo_review_runs_every_step_updates_sources_and_supports_decisions(stack):
    coord = dashboard.Coordinator(stack["urls"], "supergrid")
    fast_timeline = tuple((name, i * 0.02) for i, (name, _) in enumerate(dashboard.DEMO_TIMELINE))
    job = dashboard.DemoJob(coord, "P001", sabotage=False, timeline=fast_timeline)
    coord.job = job
    for _ in range(200):
        if job.status != "running":
            break
        time.sleep(0.05)
    assert job.status == "done", job.error
    assert job.steps == [name for name, _ in dashboard.DEMO_TIMELINE]
    assert job.details["facet_gp"] == "3 facts shared, 1 question declined"

    p = coord.latest
    assert p["demo"] is True and p["verdict"] == "APPROVED_FOR_REVIEW" and p["struck_claims"] == 0
    assert [i["source"] for i in p["ledger"]] == ["gp"] * 3 + ["pharmacy"] * 3 + ["lab"] * 3 + ["federated-model"]
    # every source logged the request on its own page
    for role in ("gp", "pharmacy", "lab"):
        audit = site_overview(stack, role)["audit"]
        assert any(a.get("patient_id") == "P001" for a in audit), role

    assert coord.today(None)["counts"]["awaiting"] == 1
    decision = coord.decline({"patient_id": "P001"})
    assert decision["action"] == "declined"
    with pytest.raises(dashboard.ApiError):  # a decision can only target the review on screen
        coord.approve({"patient_id": "P002"})
    counts = coord.today(None)["counts"]
    assert counts["declined"] == 1 and counts["awaiting"] == 0 and counts["reviewed"] == 1


def test_demo_responses_cover_every_patient_and_pass_the_verifier(stack):
    coord = dashboard.Coordinator(stack["urls"], "supergrid")
    coord._train(10, 0.5)
    for pid in dashboard.PATIENTS:
        for variant, response in dashboard.DEMO_RESPONSES[pid].items():
            post_json(f"{stack['urls']['pharmacy']}/admin/sabotage", {"on": variant == "sabotage"})
            try:
                ledger, collected = coord._collect_ledger(pid)
            finally:
                post_json(f"{stack['urls']['pharmacy']}/admin/sabotage", {"on": False})
            payload = coord._assessed(response["summary"], ledger, collected["review"], response["model"])
            assert payload["struck_claims"] == 0, (pid, variant)
            expected = "ESCALATE_CONFLICT" if variant == "sabotage" else "APPROVED_FOR_REVIEW"
            assert payload["verdict"] == expected, (pid, variant)


def site_overview(stack, role):
    from local.net import get_json
    return get_json(f"{stack['urls'][role]}/api/overview")


def test_training_is_locked_while_a_review_runs(stack, tmp_path):
    coord = dashboard.Coordinator(stack["urls"], "supergrid")
    config = tmp_path / "c.toml"
    config.write_text("")
    coord.job = dashboard.Job("supergrid", "P001", [sys.executable, "-c", "import time; time.sleep(3)"],
                              str(config), lambda job, result: None, {"rounds": 60})
    try:
        with pytest.raises(dashboard.ApiError, match="review is running"):
            coord.train({"rounds": 5})
    finally:
        coord.job._proc.kill()


def test_supergrid_run_config_toml_is_parsed_by_flwr():
    from flwr.common.config import parse_config_args

    cmd, path = dashboard.supergrid_command("supergrid", {
        "patient": {"id": "P002"}, "demo": {"sabotage": True}, "fl": {"rounds": 40, "lr": 0.5},
        "review": {"ledger": json.dumps({"note": "quotes \" and ünïcode"})}})
    try:
        parsed = parse_config_args([path])
    finally:
        Path(path).unlink()
    assert cmd[1:4] == ["run", str(dashboard.REPO), "supergrid"] and cmd[-1] == "--stream"
    assert parsed == {"patient.id": "P002", "demo.sabotage": True, "fl.rounds": 40, "fl.lr": 0.5,
                      "review.ledger": json.dumps({"note": "quotes \" and ünïcode"})}
