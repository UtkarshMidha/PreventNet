# PreventNet - Product Requirements Document (Build Spec)

**Event:** Flower Collaborative Agent Hackathon, Berlin, 16 Sep 2026
**Time budget:** 3–4 hours of implementation. Bias every decision toward "works in the demo."
**Demo slot:** 3 minutes + judge questions.
**Hard platform constraints:** runs as a Flower **AgentApp** on **SuperGrid** (flwr 1.37.0); each task has a **5-minute timeout**; there is a credit limit, so the total model calls per run must stay under ~10.

---

## 0. One-paragraph summary

PreventNet is a multi-agent preventive-medicine review built on Flower Agent. Three institutional agents (**GP**, **Pharmacy**, **Lab**) each hold their own patient records that never leave their boundary. A **Coordinator** agent runs a preventive-risk review by asking the institutional agents *structured, purpose-tagged questions*; they answer only through a strict allowlisted wire format (categorical bands, booleans, never raw records, free text, or exact values) and may **refuse** out-of-purpose questions. Every disclosed field is written to a **disclosure ledger**. A **Verifier** checks that (a) every claim in the Coordinator's risk summary is backed by a ledger entry and (b) the ledger contains no cross-agent contradictions. The output is a *recommendation for a human clinician to approve*, never a diagnosis, never a treatment. The demo shows: one preventable risk that no single institution could see, the Coordinator's entire context on screen (a dozen categorical facts, fully accounted for by the ledger), a sabotaged agent being caught, and a human approval gate.

**Why this wins on the rubric:** the collaboration IS the result (the risk only exists in the join of three agents' data), the safety mechanism IS the architecture (allowlist + ledger + verifier + human gate), and the Flower usage IS structural (AgentApp, Context state, run events, SuperGrid, Hub-published).

---

## 1. Goals and non-goals

### Goals
1. A working AgentApp that runs end-to-end on SuperGrid in **one `flwr run` invocation** (single run = whole review; fits the 5-min timeout with margin).
2. Multi-agent collaboration where **each agent phase is a separate, isolated model conversation** with access to only its own data file, enforced in code, not by prompt.
3. A machine-checkable privacy boundary (`strip_for_wire` allowlist) + a disclosure ledger + a deterministic conflict/verification layer.
4. A **sabotage mode** (run-config flag) that demonstrably triggers conflict detection and human escalation instead of a recommendation.
5. A minimal single-file HTML viewer for the demo.
6. Published to Flower Hub + public GitHub repo (submission requirements).
7. Uses the **Endeavor** model (bonus point) with a zero-code fallback to the default model.

### Non-goals: explicitly DO NOT build
- No model training, no FL rounds, no embeddings, no RAG, no vector DB.
- No real datasets (MIMIC etc.). Synthetic JSONL only.
- No web framework, no React, no npm, no build step. One static HTML file.
- No Flower connectors (`web_search` etc.): this app needs none; local data reads are the "tools."
- No auth, no FHIR, no database, no more than 3 institutional roles, no more than 2 patient cases.
- No retries/backoff sophistication; fail loudly with a clear message.

---

## 2. Verified platform facts (do NOT deviate from these; do NOT invent other Flower APIs)

These are confirmed against the official Flower Agent docs (Flower 1.37.0) and the Berlin brief:

- Project template: `uvx --from flwr==1.37.0 flwr new @flwrlabs/agent` → structure `agent/agent_app.py` + `pyproject.toml`.
- Imports: `from flwr.agentapp import AgentApp, AgentSession`, `from flwr.app import ConfigRecord, Context`, `from openai import OpenAI`.
- Entry point:
  ```python
  app = AgentApp()

  @app.main()
  def main(agent: AgentSession, context: Context) -> None: ...
  ```
- Model access: Flower injects `FLWR_RUNTIME_BASE_URL` and `FLWR_RUNTIME_API_KEY` into the process. Create the client as:
  ```python
  client = OpenAI(base_url=os.environ["FLWR_RUNTIME_BASE_URL"],
                  api_key=os.environ["FLWR_RUNTIME_API_KEY"], max_retries=0)
  ```
  Use `client.responses.create(model=MODEL, input=..., instructions=...)` (OpenAI Responses API). No provider API key anywhere in the repo.
- Run config: values from `[tool.flwr.app.config.*]` in pyproject arrive as `context.run_config["agent.input"]` etc., overridable via `flwr run . supergrid --run-config 'key="value"' --stream`.
- Persistent/shared state: `context.state.config_records` holding `ConfigRecord` objects; mutate under `with context.locked():`. Store JSON strings inside ConfigRecords (this is the documented pattern).
- Frontend-visible events: `agent.events.emit({...})`: emitted dicts appear in SuperGrid run activity. Use it for every phase transition and every disclosure decision.
- Streaming the final answer: iterate `client.responses.create(..., stream=True)` events, `agent.events.emit(event.to_dict())` each, collect `response.output_text.delta`; raise on `error` / `response.failed` / `response.incomplete`.
- Validate every run-config value before any model call (official guidance). Cap all loops with constants.
- `uv sync` → `uv run flwr build` → `uv run flwr login supergrid` → `uv run flwr run . supergrid --stream`. Logs: `uv run flwr log <run-id> supergrid --show`.
- Publishing: `flwr` app publish flow uploads source and Hub builds the FAB server-side; the `publisher` field in pyproject **must match the Flower account username**.
- Python `>=3.11,<3.14`. `dependencies = ["flwr>=1.37.0,<2.0", "openai>=2.16.0,<3.0.0"]`.

### Model selection (Endeavor bonus)
```toml
[tool.flwr.app.config.model]
ref = "ENDEAVOR_MODEL_REF_TBD"      # confirmed in #hackathon_berlin_2026 Slack on the day
fallback = "openai/gpt-5.6-sol"     # template default, known to work on the runtime
```
Code reads `model.ref`; if it equals `"ENDEAVOR_MODEL_REF_TBD"` or a model call fails with a model-not-found style error on the FIRST call, log a clear warning event and use `model.fallback` for the whole run. Never hardcode the model in code.

> **HUMAN ACTION REQUIRED (leave as TODO comment):** replace `ENDEAVOR_MODEL_REF_TBD` with the real ref from Slack; replace `publisher = "PUBLISHER_TBD"` with the Flower account username.

---

## 3. Architecture

One AgentApp. One run = one complete review, executed as **five sequential phases inside `main()`**. Each phase is an isolated model conversation (fresh `input` list; phases NEVER share raw conversation history; only wire-format objects cross phases). This is the "agent chain in which multiple agents contribute to a result" from the Berlin brief, with the chain's shared state carried exactly the way the official tutorial carries conversation state.

```
run_config: patient.id, demo.sabotage, agent.mode
        │
        ▼
┌─ Phase 0: Coordinator (plan) ── model call #1
│    reads: patient.id + question catalog → picks purpose-tagged questions per role
│
├─ Phase 1: GP agent ──────────── model call #2      data/gp.jsonl ONLY
├─ Phase 2: Pharmacy agent ────── model call #3      data/pharmacy.jsonl ONLY
├─ Phase 3: Lab agent ─────────── model call #4      data/lab.jsonl ONLY
│    each: policy check → answer → strip_for_wire() → ledger.append()
│    (sabotage flag: pharmacy answer overridden post-model, deterministically)
│
├─ Phase 4: Coordinator (reason) ─ model call #5 (streamed)
│    input = ONLY the ledger's wire items → risk summary + recommendation draft
│
├─ Phase 5: Verifier ──────────── deterministic checks + model call #6 (explanation only)
│    (a) every summary claim ↦ ledger entry   (b) contradiction rules over ledger
│    (c) allowlist audit of everything the Coordinator saw
│    verdict: APPROVED_FOR_REVIEW │ ESCALATE_CONFLICT │ BLOCKED_UNVERIFIED
│
└─ Output: RESULT_JSON printed to stdout (single line, prefixed `PREVENTNET_RESULT `)
     + full payload stored in context.state ConfigRecord "preventnet"
     + human approval happens in the viewer (recommendation is never auto-released)
```

Total model calls per run: **6** (well under timeout and credit limits). Set `MAX_MODEL_CALLS = 8` and hard-fail past it.

### Data isolation rule (the core safety property, enforced in code)
- `facet_answer(role, ...)` is the ONLY function that opens `data/{role}.jsonl`, and it is called with exactly one role per phase.
- The Coordinator phases receive **only** `[w.as_wire_dict() for w in ledger]`; construct their model input from the ledger object, never from any variable that touched raw records.
- `strip_for_wire()` is an **allowlist**: it copies only known fields (`field`, `value`, `band`, `purpose`, `source`, `ts`, `confidence`) and raises if asked to pass anything else. A field added to records later cannot leak by being forgotten.

---

## 4. Repository layout

```
preventnet/
├── pyproject.toml
├── README.md                  # problem, architecture diagram (ascii ok), run commands, safety section
├── LICENSE                    # MIT
├── preventnet/
│   ├── __init__.py
│   ├── agent_app.py           # AgentApp entry point, phase orchestration, events, output
│   ├── wire.py                # WireItem dataclass, strip_for_wire (allowlist), Ledger
│   ├── policy.py              # per-role disclosure policy (purpose → allowed fields), refusal reasons
│   ├── facets.py              # facet_answer(role, questions, records) → list[WireItem] | Refusal
│   ├── coordinator.py         # plan_questions(), reason_over_ledger() prompts + parsing
│   ├── verifier.py            # deterministic checks + rules; verdict logic
│   ├── prompts.py             # ALL prompt strings in one file (easy to tune at the venue)
│   └── data/
│       ├── gp.jsonl
│       ├── pharmacy.jsonl
│       └── lab.jsonl
├── viewer/
│   └── viewer.html            # single-file demo UI (no dependencies)
├── scripts/
│   └── extract_result.py      # pull the PREVENTNET_RESULT line from a saved log → viewer/latest.json
└── tests/
    └── test_invariants.py     # offline, no model calls
```

pyproject essentials:
```toml
[project]
name = "preventnet"
version = "0.1.0"
description = "Federated preventive-risk review: agents share judgements, never records."
requires-python = ">=3.11,<3.14"
dependencies = ["flwr>=1.37.0,<2.0", "openai>=2.16.0,<3.0.0"]

[tool.flwr.app]
publisher = "PUBLISHER_TBD"
flwr-version-target = "1.37.0"
fab-include = ["preventnet/**/*.py", "preventnet/data/*.jsonl", "LICENSE"]

[tool.flwr.app.config.agent]
input = "Run a preventive review for patient P001."

[tool.flwr.app.config.patient]
id = "P001"

[tool.flwr.app.config.demo]
sabotage = false

[tool.flwr.app.config.model]
ref = "ENDEAVOR_MODEL_REF_TBD"
fallback = "openai/gpt-5.6-sol"

[tool.flwr.app.components]
agentapp = "preventnet.agent_app:app"
```
(Keep whatever build-system/hatch sections the template generates. Data files MUST be in `fab-include`, because the app runs remotely on SuperGrid and reads them relative to the package via `importlib.resources` or `Path(__file__).parent / "data"`.)

---

## 5. Component specs

### 5.1 `wire.py`
```python
ALLOWED_WIRE_FIELDS = {"field", "value", "band", "purpose", "source", "ts", "confidence"}
ALLOWED_VALUE_TYPES = (bool, str)     # str values must come from closed enums/bands, never free text > 40 chars
```
- `WireItem` dataclass with exactly those fields. `strip_for_wire(raw: dict) -> WireItem` copies only allowlisted keys; raises `WireViolation` on unknown keys, on values > 40 chars, and on keys named `record_id`, `text`, `note`, `name`, `dob` (defense in depth).
- `Ledger`: append-only list of WireItems + `Refusal` entries (`{question_id, role, reason}`); `to_json()`; every append also calls the provided `emit` callback so each disclosure appears live in SuperGrid run activity as `{"preventnet": "disclosure", "role": ..., "field": ..., "band": ..., "purpose": ...}`.

### 5.2 Question catalog + `policy.py`
Hardcode a **closed catalog of ~10 questions**, each `{id, role, field, purpose, text}`. Examples:
- `q_gp_conditions` (gp, `chronic_conditions`, purpose `renal-risk-review`): returns enum list from {hypertension, type2_diabetes, none_relevant}
- `q_gp_planned_imaging` (gp, `contrast_imaging_planned_90d`, bool)
- `q_gp_reported_otc` (gp, `patient_reported_otc_nsaid`, bool)
- `q_ph_nsaid_90d` (pharmacy, `nsaid_dispensed_90d`, bool)
- `q_ph_metformin_active` (pharmacy, `metformin_active`, bool)
- `q_ph_interacting_pairs` (pharmacy, `interacting_dispense_flag`, bool)
- `q_lab_egfr_band` (lab, `egfr_band`, enum {normal, mildly_reduced, moderately_reduced, severely_reduced})
- `q_lab_egfr_trend` (lab, `egfr_trend_24m`, enum {stable, declining, improving})
- `q_lab_hba1c_band` (lab, `hba1c_band`, enum {normal, prediabetic, diabetic})  ← purpose `metabolic-screening`
- `q_gp_marketing_segment` (gp, `insurance_marketing_segment`, purpose `marketing`) ← **exists ONLY to be refused** (out-of-purpose), so the refusal path is real in every demo run.

`policy.py`: per-role dict `ALLOWED = {role: {purpose: {field, ...}}}`. `check(role, field, purpose)` → OK or `Refusal(reason ∈ {out_of_purpose, not_held, below_disclosure_threshold})`. The marketing question must be refused by policy for every role.

### 5.3 `facets.py`
`facet_answer(role, questions, sabotage=False)`:
1. Load ONLY `data/{role}.jsonl`, filter to `patient.id`.
2. One model call: instructions = role persona ("You are the {role} data-steward agent. Answer ONLY the questions asked, ONLY from the records provided, ONLY as the closed enums/bools specified. If records don't determine an answer, answer null.") + the role's policy-permitted questions + that role's records for the patient. Request strict JSON (`{"answers": {question_id: value}}`); parse with `json.loads` after stripping code fences; on parse failure, ONE retry with "return only JSON"; on second failure raise.
3. Policy check each answer's question; refused questions → `Refusal` to ledger, answer discarded.
4. `strip_for_wire` each permitted answer → ledger.
5. **Sabotage** (only if `demo.sabotage=true` AND role == pharmacy): after the model answers, override `nsaid_dispensed_90d → false` and `interacting_dispense_flag → false` before writing to the ledger, and emit `{"preventnet": "sabotage_injected", "role": "pharmacy"}` (transparency: the demo narrates this as "we compromise the pharmacy agent live"). Deterministic, so the sabotage demo can never fail.

### 5.4 `coordinator.py`
- `plan_questions(model_call, patient_id)`: model picks question ids from the catalog for a preventive review (prompt lists the catalog; output = JSON list of ids). Then code **forces inclusion** of the full seeded set for P001 (union) so the demo is deterministic; the model's selection is displayed but cannot break the run.
- `reason_over_ledger(model_call, ledger)`: input is ONLY ledger wire items + refusals. Instructions (put verbatim in `prompts.py`):
  - "You are a preventive-care coordinator. You advise on PREVENTION only. You never diagnose and never recommend starting/stopping specific treatments; you recommend *reviews, screenings, and checks* for a clinician to consider."
  - "Every claim you make MUST cite ledger item ids in [brackets]."
  - "Structure: RISKS (each with cited evidence), RECOMMENDATION (one preventive action for the GP to approve), CONFIDENCE (high/medium/low)."
- Stream this call; emit deltas; collect final text.

### 5.5 `verifier.py`: deterministic first, model for prose only
1. **Citation check:** every `[ledger-id]` in the summary must exist in the ledger; every RISK line must contain ≥1 citation. Fail → strike the line (wrap in `~~`), count it.
2. **Contradiction rules** (hardcoded, this is the sabotage catch):
   - `gp.patient_reported_otc_nsaid == true` AND `pharmacy.nsaid_dispensed_90d == false` → CONFLICT
   - `pharmacy.metformin_active == true` AND `pharmacy.interacting_dispense_flag == false` AND `gp.patient_reported_otc_nsaid == true` → CONFLICT (dispensing story inconsistent)
3. **Allowlist audit:** re-validate every item the coordinator saw against `ALLOWED_WIRE_FIELDS` + value constraints; record `context_item_count` (the money-shot number).
4. Verdict: conflicts > 0 → `ESCALATE_CONFLICT` (recommendation replaced by "Data conflict between institutions; human review required before any action; do not trust the pharmacy-derived items until reconciled"); struck lines > 0 and no risks survive → `BLOCKED_UNVERIFIED`; else `APPROVED_FOR_REVIEW`.
5. One model call ONLY to write a 2-sentence plain-language explanation of the verdict (never to decide it).

### 5.6 `agent_app.py`
- Validate run config (`agent.input` non-empty str, `patient.id` matches `^P\d{3}$`, `demo.sabotage` bool) before any model call.
- Orchestrate phases; emit `{"preventnet": "phase", "name": ...}` at each boundary.
- `model_call()` helper: counts calls against `MAX_MODEL_CALLS=8`, handles the Endeavor→fallback switch once, `max_retries=0`.
- Persist final payload: `with context.locked(): context.state.config_records["preventnet"] = ConfigRecord({"result": json.dumps(payload)})`.
- Print exactly one line: `PREVENTNET_RESULT {json}` (compact). Also print the human-readable summary after it.
- On any unhandled exception: emit `{"preventnet": "failed", "reason": str(e)}` and re-raise (fail loudly; a facet that fails is reported, never treated as "no findings"; include `facet_status` per role in the payload).

**RESULT_JSON schema** (the viewer's contract):
```json
{
  "patient_id": "P001",
  "model_used": "…",
  "sabotage": false,
  "facet_status": {"gp": "ok", "pharmacy": "ok", "lab": "ok"},
  "local_views": {"gp": {"records_scanned": 6}, "pharmacy": {"records_scanned": 9}, "lab": {"records_scanned": 7}},
  "ledger": [ {"id": "L1", "role": "lab", "field": "egfr_band", "value": "moderately_reduced", "purpose": "renal-risk-review", "ts": "…"} ],
  "refusals": [ {"question_id": "q_gp_marketing_segment", "role": "gp", "reason": "out_of_purpose"} ],
  "coordinator_context_items": ["L1", "L2", "…"],
  "context_item_count": 12,
  "risk_summary_md": "…",
  "verdict": "APPROVED_FOR_REVIEW",
  "conflicts": [],
  "struck_claims": 0,
  "verifier_note": "…",
  "recommendation": "Invite P001 for a medication review before the scheduled contrast CT; flag OTC NSAID use."
}
```

### 5.7 Data fixtures (write these exactly; ~1 hour total including noise)
Record shape (per line): `{"patient_id": "P001", "kind": "...", "date": "2026-06-14", ...fields...}` plus a `"note"` free-text field on some records **specifically so the tests can prove notes never reach the wire**.

**Patient P001 (Case A, the join risk; each facet is individually boring):**
- `gp.jsonl`: hypertension dx (2019); type-2 relative history; visit note "patient mentions taking ibuprofen from the pharmacy for knee pain" (`patient_reported_otc_nsaid` derivable); referral: contrast CT scheduled in 3 weeks; 2 noise visits.
- `pharmacy.jsonl`: metformin repeat dispensings ×4; ibuprofen 400mg dispensed 5 weeks ago; noise (vitamin D, cetirizine).
- `lab.jsonl`: creatinine/eGFR results across 24 months showing decline into the moderately_reduced band (encode band directly per result to avoid unit math: `"egfr_band": "moderately_reduced"`); HbA1c 5.9%→`prediabetic` band ×2; noise CBC.
- The seeded story: metformin + NSAID + declining eGFR + upcoming contrast imaging = classic preventable AKI/contrast-risk setup → recommendation: pre-imaging medication review. No single file contains more than one leg of it.

**Patient P002 (Case B, optional, only if time remains):** prediabetes screening story (lab prediabetic trend + gp BMI/family history + pharmacy no meds).

Add 2 other patients' noise records to each file so filtering visibly matters.

### 5.8 `viewer/viewer.html`: minimal, single file, zero dependencies
- Plain HTML + vanilla JS + a `<style>` block. Dark, clean, big fonts (projector). No frameworks, no CDN (venue wifi risk).
- Loads `latest.json` via `fetch("latest.json")`; if that fails (file:// context), show a `<textarea>` "paste RESULT_JSON" fallback with a Load button.
- Layout, top to bottom:
  1. Header: patient id, model badge (says "Endeavor" when the ref matches), verdict pill (green APPROVED_FOR_REVIEW / amber ESCALATE_CONFLICT / red BLOCKED_UNVERIFIED).
  2. **Three institution cards** side-by-side (GP / Pharmacy / Lab): records_scanned count and the caption "raw records: LOCAL ONLY". A skull-and-crossbones "COMPROMISED (demo)" tag on Pharmacy when `sabotage=true`.
  3. **Disclosure ledger table** (id, role, field, value, purpose) + refusals rendered in a distinct "refused" style. Above it, huge: "Everything the coordinator ever saw: **{context_item_count} items**, all listed below."
  4. Risk summary (render the markdown minimally: bold, lists, and `~~struck~~` as strikethrough; conflicts highlighted).
  5. **Human gate:** the recommendation is blurred/greyed with an "🔒 Awaiting clinician approval" overlay and an **Approve & send to GP** button; clicking unblurs and stamps "Approved by clinician at {time}" (client-side only; that's honest, since the system never releases without this click). If verdict is ESCALATE_CONFLICT, the button is replaced by "Resolve data conflict first" (disabled); escalation means the human must act *upstream*.
- Total target: ≤ 250 lines. Do not exceed meaningfully.

### 5.9 `scripts/extract_result.py`
Read a log file (or stdin), find the last line starting `PREVENTNET_RESULT `, write the JSON to `viewer/latest.json`, pretty-print the verdict. ~20 lines.

### 5.10 `tests/test_invariants.py` (offline, no model calls, <10 tests)
1. `strip_for_wire` raises on `record_id`, `text`, `note`, unknown keys, >40-char strings.
2. No raw record field name from the fixtures ever appears in a serialized ledger built from a stubbed facet answer set.
3. Policy refuses the marketing question for all roles.
4. Contradiction rule fires on the sabotage value pattern; does not fire on the honest pattern.
5. Verifier strikes an uncited claim; verdict transitions (0 conflicts→APPROVED, ≥1→ESCALATE).
6. Fixtures sanity: P001 exists in all three files; no single file contains all three legs of Case A (assert e.g. gp.jsonl has no dispensing records, pharmacy.jsonl has no eGFR).

Run with `uv run pytest -q` (add `pytest` to a dev dependency group, NOT to `[project] dependencies`, to keep the FAB lean).

---

## 6. Demo runbook (put verbatim in README)

```bash
# one-time
uv sync
uv run pytest -q
uv run flwr build
uv run flwr login supergrid

# happy path (Case A)
uv run flwr run . supergrid --stream | tee run_happy.log
python scripts/extract_result.py run_happy.log
# open viewer: cd viewer && python -m http.server 8000  → http://localhost:8000/viewer.html

# sabotage act
uv run flwr run . supergrid --run-config 'demo.sabotage=true' --stream | tee run_sabotage.log
python scripts/extract_result.py run_sabotage.log   # refresh viewer

# publish (do this at 16:00, not 17:20)
# flwr app publish flow per Flower Hub how-to; publisher in pyproject must equal your Flower username
```
At ~16:00 also record a screen capture of one successful happy-path + sabotage run as the fallback if SuperGrid is congested at demo time.

**3-minute script:** (0:00) "Your pharmacy is legally forbidden from reading your GP's notes. Here's what that costs, and how agents fix it without breaking the law." (0:20) happy path result in viewer: three boring institutions → one caught risk. (1:10) scroll to ledger: "the coordinator's entire world is these 12 categorical facts, and one refusal, because we asked an out-of-purpose question on purpose." (1:50) "Now we compromise the pharmacy agent live" → sabotage run result → amber ESCALATE, conflict highlighted, approval disabled. (2:30) happy-path tab → clinician clicks Approve. (2:50) Flower Hub link on screen. One speaker; second teammate drives.

---

## 7. Evaluation-criteria verification matrix (acceptance criteria)

The build is DONE only when every row's "verifiable by" holds:

| Rubric dimension | How PreventNet satisfies it | Verifiable by |
|---|---|---|
| **Impact** | Preventable-harm detection across legally separated institutions (GDPR purpose limitation / ePA context); recommendation is a concrete clinical action | README problem section; demo Case A; Q&A: GenoMed4All shows Flower already runs in EU hospital federations |
| **Innovation** | Vertical partitioning (facets of one patient) vs. existing horizontal projects; negotiated purpose-tagged disclosure + refusals; ledger-audited agent context; deterministic verifier over agent outputs | README "related work" para naming what exists (federated diagnosis consult; FL risk models) and what's new here |
| **Use of Flower** | AgentApp on SuperGrid; run-config-driven behavior; `Context`/`ConfigRecord` state under `context.locked()`; `agent.events.emit` for every phase + disclosure (SuperGrid activity view = live audit trail); FAB-packaged data; Hub-published; **Endeavor model (bonus)** | run visible on SuperGrid with emitted events; Hub link; `model_used` shows Endeavor |
| **Technical execution** | Single-run pipeline under timeout (≤8 model calls); strict JSON contracts; loud failure handling; facet-down reported not ignored; offline test suite green | `uv run pytest -q` passes; two clean SuperGrid runs (happy + sabotage) |
| **Demo & delivery** | 3-min script above; viewer with verdict pill, ledger money-shot, sabotage act, approval gate; recorded fallback | timed rehearsal ≤3:00; recording exists |
| **Safety & oversight** | Allowlist wire (code-enforced), disclosure ledger, refusal path exercised every run, verifier citation+contradiction checks, conflict→escalation, prevention-only prompt policy, human approval before release, no credentials in prompts, hard call cap | tests 1–5; sabotage demo; grep repo for absence of any API key |

---

## 8. Build order (implement in this sequence; each step leaves the repo runnable)

1. **Skeleton (20 min):** template project renamed, pyproject as §4, `agent_app.py` doing one model call and printing `PREVENTNET_RESULT {"ok": true}`. Verify `flwr build` passes.
2. **Wire + ledger + policy + fixtures (45 min):** §5.1, §5.2, §5.7 + tests 1–3, 6.
3. **Facet phases (40 min):** §5.3 with real model calls; ledger fills; emit disclosures.
4. **Coordinator + output payload + extract script (40 min):** §5.4, §5.6, §5.9: end-to-end happy path on SuperGrid. ← *the spine; everything after is enrichment*
5. **Verifier + sabotage (30 min):** §5.5, sabotage override, tests 4–5.
6. **Viewer (45 min):** §5.8.
7. **README + polish (20 min):** runbook, safety section, related-work paragraph, ascii architecture diagram.

If time runs out, cut from the bottom: Case B first (already optional), then viewer polish (the JSON + terminal is presentable), never the spine or the tests.

---

## 9. Known environment gotchas (encode as README "Troubleshooting")

- Cambridge's venue blocked ports 9092/9093 (deployment federation unreachable); everything here targets SuperGrid over 443, which worked there. Don't add SuperNode/deployment paths.
- Endeavor ref unknown until Slack; fallback logic in §2 exists for exactly this; the run still succeeds and `model_used` is honest about which model ran.
- SuperGrid runs execute remotely: never rely on writing local files from the app; stdout line + Context state are the only outputs.
- `publisher` mismatch with the Flower username breaks publishing; fix it BEFORE 16:00.
- Strict-JSON model outputs: always strip ``` fences before `json.loads`; one bounded retry.
```
