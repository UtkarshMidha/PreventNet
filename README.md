# 🩺 PreventNet

> Powered by Flower. Agents share judgements, never records.

**Author: [Utkarsh Midha (@UtkarshMidha)](https://github.com/UtkarshMidha)** · 🥈 **2nd place**, Flower Collaborative Agent Hackathon, Berlin 2026 · Published on Flower Hub as [`@utkarsh/preventnet`](https://flower.ai/apps/utkarsh/preventnet)

A preventable harm sometimes exists only in the *join* of several institutions'
records, and that join is exactly what the law forbids. PreventNet is a
multi-agent preventive-medicine review, built as a Flower **AgentApp**, in which
a GP system, a pharmacy and a lab answer a coordinator's purpose-tagged
questions with short categorical facts, refuse anything outside their purpose,
and never let a record leave their boundary.

![A full review running end to end: the agents query each source in turn, the coordinator writes its assessment, and the clinician decides](https://raw.githubusercontent.com/UtkarshMidha/PreventNet/main/docs/screenshots/demo.gif)

*One review, start to finish, in about twenty seconds. Full frame-by-frame walkthrough with both the happy path and the sabotage path: **[WALKTHROUGH.md](https://github.com/UtkarshMidha/PreventNet/blob/main/WALKTHROUGH.md)**.*

## 🧩 Problem

Your pharmacy is legally forbidden from reading your GP's notes, and your lab is
forbidden from reading either. That is correct: GDPR purpose limitation, and
Germany's ePA keeps institutional boundaries for good reason. But it means a
preventable harm that only appears when three institutions' data are read
together can silently fall through the cracks.

A patient on active metformin, self-reporting over-the-counter NSAID use, with
kidney function declining over two years, about to receive a contrast CT scan,
is a real medication-and-kidney safety problem. No single institution in that
chain sees enough of it to flag it. The GP does not know what was dispensed. The
pharmacy does not know a scan is booked. The lab knows neither.

The naive fix, pooling the records, is illegal, and building it would be the
wrong thing to build.

## 🏁 Objective

PreventNet sets out to surface that cross-institutional risk **without any
institution disclosing a record**, and to produce a recommendation a clinician
can act on and audit: every claim tied to a specific disclosed fact, every fact
tied to the source that released it and the purpose it was released for.

The standard it holds itself to: a claim the verifier cannot tie to a real
ledger entry does not ship, and a review whose sources contradict each other
cannot be approved at all, however confident the model sounds.

## 💡 Solution

A coordinator agent asks each institution's agent questions drawn from a
**closed catalog**, each tagged with a purpose. Each institution answers from
its own records only, and may **refuse**. One catalog question exists purely to
be refused, so the refusal path runs on every review rather than only in tests.
Answers cross the wire through an **allowlist**: categorical bands and booleans,
never free text, dates, drug names or record IDs.

Everything disclosed lands in a **disclosure ledger**. The coordinator's
reasoning phase is given the ledger and nothing else. A deterministic
**verifier** then checks that every claim cites a real ledger entry and that the
ledger contains no cross-source contradictions, and sets the verdict in code:
`APPROVED_FOR_REVIEW`, `ESCALATE_CONFLICT` or `BLOCKED_UNVERIFIED`. A human
clinician makes the final call, and an escalated review cannot be approved at
all.

Alongside this, a diabetes-risk model is trained across the same three
institutions by **vertical federated learning**, since they hold different
columns about the same people. Its output enters the review as one more cited
fact, not as the decision-maker.

## ✨ Key features

- **Judgements, not records.** The coordinator's entire input is ~10 short categorical answers. Raw records never cross a boundary.
- **Code-enforced wire format.** `strip_for_wire()` is an allowlist that raises on unknown, oversized or blocked keys, so a field added to a record later cannot leak by being forgotten.
- **Purpose-tagged questions and real refusals.** Every question carries a purpose; every source can decline one, and one always does.
- **Disclosure ledger.** Every field an agent ever disclosed, with its source, purpose and timestamp, auditable from the institution's own side too.
- **Deterministic verifier.** Citation checking and fixed contradiction rules decide the verdict, not the language model.
- **Survives a compromised agent.** Flip a source's answers and the verifier escalates; approval is refused server-side with HTTP 409.
- **Vertical federated learning.** Three column sets, one logistic regression, pairwise additive masks with single-use nonces, peer-to-peer residuals. ~0.79 test AUC.
- **Human approval gate.** Nothing is released automatically, and the recommendation is always a *review*, never a diagnosis or a treatment change.
- **No credentials in the repo.** Model access comes only from `FLWR_RUNTIME_BASE_URL` / `FLWR_RUNTIME_API_KEY`, injected by the Flower Runtime.
- **Runs fully offline.** The four local deployments, the federated training and the whole test suite need no account, no key and no network.

## ⚙️ How it works?

The coordinator is never trusted with data, and the model is never trusted with
the verdict.

Each institutional agent reads exactly one folder. `facets.py` is the only code
that opens `data/<role>/records.jsonl`, and it is called with one role per
phase; `vfl.SiteModel` is the only code that opens `data/<role>/cohort.jsonl`,
one object per institution. Policy runs *before* a record is read, so a refused
question never reaches a model at all. The coordinator's reasoning phase
receives `[item.as_wire_dict() for item in ledger.items]`, built from the ledger
object, never from any variable that touched a raw record.

The verdict is then computed by code. `verifier.py` strikes any claim that does
not cite a real ledger entry, runs hardcoded contradiction rules across sources,
and re-audits the allowlist. That is what catches a sabotaged agent: the
compromised source's answer is internally consistent and confidently phrased,
but it contradicts what another source said, and the rule fires regardless of
how the finding was worded.

## 🏗️ Architecture

Who holds what, and what is allowed to cross the boundary between them.

![PreventNet architecture: three departments holding their own records above a boundary line, disclosing only yes/no answers and bands to a coordinator, which reasons over them and hands a recommendation to a clinician](https://raw.githubusercontent.com/UtkarshMidha/PreventNet/main/docs/screenshots/architecture.png)

Everything above the red line stays inside its institution. The only things that
cross it are short categorical answers, refusals, and masked partial scores that
the coordinator can sum but not unmask. Training residuals move department to
department, from the label holder to the other two, and never through the
coordinator. *(Source: [`docs/architecture.html`](https://github.com/UtkarshMidha/PreventNet/blob/main/docs/architecture.html).)*

As a run, that is one AgentApp with sequential phases:

```text
run_config: patient.id, demo.sabotage, fl.rounds, fl.lr, review.ledger
        |
        v
+-- Coordinator (plan) ---------------- picks purpose-tagged questions
|                                       from the closed catalog
+-- GP agent -------------------------- data/gp/records.jsonl ONLY
+-- Pharmacy agent -------------------- data/pharmacy/records.jsonl ONLY
+-- Lab agent ------------------------- data/lab/records.jsonl ONLY
|     each: policy check -> answer -> strip_for_wire() -> ledger.append()
|
+-- Federated phase ------------------- no model calls
|     each institution loads ONLY data/<role>/cohort.jsonl
|     vertical logistic regression over masked sums
|     -> ledger fact: fl_diabetes_risk_band (low / moderate / high)
|
+-- Coordinator (reason) -------------- input = ONLY the ledger's wire items
|                                       -> findings + recommendation
+-- Verifier -------------------------- deterministic:
|     (a) every claim cites a real ledger entry
|     (b) contradiction rules across sources
|     (c) allowlist re-audit of everything the coordinator saw
|     verdict: APPROVED_FOR_REVIEW | ESCALATE_CONFLICT | BLOCKED_UNVERIFIED
|
+-- Output ---------------------------- PREVENTNET_RESULT {json} on stdout
                                        + context.state; human approves in the UI
```

`local/` runs that same policy, federated code and verifier as **four
independent processes** on ports 8100 to 8103, so the boundaries above are
visible rather than asserted: each department is a separate server that the
coordinator can only reach through `/ask`.

The mask secret (`PREVENTNET_FL_SECRET`) is given to the three sources by the
launcher and withheld from the coordinator, so the coordinator cannot unmask a
partial score even in principle.

<sub>Architecture and implementation by [@UtkarshMidha](https://github.com/UtkarshMidha).</sub>

## 🔒 The core safety property

| Guarantee | Enforced by |
|---|---|
| One agent reads one institution's records | `facets.py` is the only reader of `data/<role>/records.jsonl` |
| The coordinator sees only judgements | reasoning input is built from the `Ledger` object alone |
| Nothing unexpected crosses the wire | `wire.strip_for_wire()` allowlist raises on unknown/blocked/oversized keys |
| Out-of-purpose questions are never seen by a model | policy runs before any record is read |
| The verdict is not the model's opinion | `verifier.py`: citation check + fixed contradiction rules |
| A conflicted review cannot be approved | approval endpoint returns HTTP 409 |
| No key in the repo | model access only via injected `FLWR_RUNTIME_*` env vars |
| Failures are loud | any unhandled exception emits a failure event and re-raises |

## 🧱 Technology stack

**Application**
- Python 3.11 to 3.13
- [Flower](https://flower.ai) `flwr >= 1.37`: AgentApp, SuperGrid, FAB packaging
- Flower Endeavor (`flower-endeavor-v1.0`) via the OpenAI Responses API, with a configurable fallback model and a per-call time budget
- NumPy, for the federated logistic regression

**Local deployments**
- Python standard library only: `http.server.ThreadingHTTPServer`, no web framework
- Hand-written CSS and vanilla JS, no npm, no build step, no component or chart library
- `viewer/viewer.html` is a single dependency-free file

**Testing**
- pytest: 42 tests, offline, no API key, ~4 seconds

## 📁 Repository structure

```text
.
├── preventnet/              the Flower AgentApp
│   ├── agent_app.py         entry point, phase orchestration, events, output
│   ├── wire.py              WireItem, strip_for_wire (allowlist), Ledger
│   ├── policy.py            question catalog + per-role/purpose disclosure policy
│   ├── facets.py            the only reader of a role's records
│   ├── rules.py             deterministic institutional answers
│   ├── coordinator.py       question planning and ledger reasoning
│   ├── verifier.py          citation check, contradiction rules, verdict logic
│   ├── prompts.py           every model prompt string
│   ├── vfl.py               vertical federated logistic regression
│   └── data/<role>/         records.jsonl (review records) + cohort.jsonl (Pima columns)
├── local/                   the four local deployments
│   ├── launch.py            starts all four
│   ├── site.py              one institution (GP / Pharmacy / Lab)
│   ├── dashboard.py         coordinator: reviews, federated training, approvals
│   ├── demo_responses.json  coordinator wording recorded from real Endeavor runs
│   └── *.html, ui.css       the interface
├── viewer/viewer.html       the review result, one dependency-free file
├── scripts/
│   ├── prepare_data.py      splits Pima vertically into the three cohorts
│   └── extract_result.py    pulls PREVENTNET_RESULT out of a run log
├── tests/                   42 offline tests
└── docs/
    ├── screenshots/         the walkthrough images
    ├── evidence/            unedited `flwr ls supergrid` output
    ├── PRD.md               the original build spec
    └── PreventNet_Overview.pdf
```

## ✅ Prerequisites

- Python 3.11 to 3.13
- [uv](https://docs.astral.sh/uv/) (or pip and a virtualenv)
- Nothing else. No Docker, no npm, no database, no API key.

A Flower account is needed **only** for the optional SuperGrid section below.

## 🚀 Quick start

```bash
uv sync
uv run pytest -q                       # 42 passed
uv run python -m local.launch          # Ctrl+C stops all four
```

Then open **<http://127.0.0.1:8100>**. The four deployments are:

| Deployment | URL | Holds |
|---|---|---|
| Coordinator dashboard | <http://127.0.0.1:8100> | nothing raw: only disclosed facts, masked scores, approvals |
| Health Diagnostics (GP) | <http://127.0.0.1:8101> | visit notes, diagnoses, referrals + BMI, blood pressure, age, family history **and the outcome label** |
| Prescription Logs (pharmacy) | <http://127.0.0.1:8102> | dispensing records + a synthetic medication column |
| Lab Reports | <http://127.0.0.1:8103> | eGFR/HbA1c results + glucose, insulin |

Without uv:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e . && pip install pytest
python -m local.launch
```

## 🧭 Using the application

1. **Look at the sources.** Open any of the three source pages. Each shows what it holds and, separately, every answer it has ever disclosed and every question it refused. The coordinator cannot see those pages.
2. **Run a review.** *Reviews* → patient **P001** → **Start Review**. Watch each source answer in turn, with one question declined.
3. **Read the assessment.** Findings tagged with the sources they rest on, a recommendation, and *What The Coordinator Saw*: the ten short answers that were its entire input.
4. **Decide.** **Approve** or **Decline**. The decision is recorded server-side.
5. **Break it.** Switch on *Simulate Compromised Prescription Logs* and run P001 again. Two answers are secretly flipped; the verifier catches the contradiction, and **Approve** is disabled.
6. **Look under the hood.** *Insights* shows the federated model's training curve, what each source contributes, and the full question catalog with each question's purpose and rationale.

Patient P001 is the seeded demo case, so both paths reproduce identically on any
checkout. `PREVENTNET_AS_OF` (default `2026-06-20`) sets the "today" used for
90-day lookbacks over the synthetic records.

**Fast Mode**, on by default, plays a full review in ~20 s: the three sources are
really queried, the risk model really scores the patient, and the verifier
really checks the result. Only the coordinator's *wording* is pre-recorded, per
patient, in `local/demo_responses.json`. Switch it off to run the live AgentApp
on SuperGrid, which needs the setup below and takes 1 to 4 minutes, almost all
of it model time.

## 🌸 Running on Flower SuperGrid (optional)

The same app, unchanged, runs as a Flower AgentApp on SuperGrid. This is the
only part of the project that needs an account.

```bash
uv run flwr build
uv run flwr login supergrid
uv run flwr run . supergrid --stream | tee run.log
uv run python scripts/extract_result.py run.log   # -> viewer/latest.json

# the sabotage act
uv run flwr run . supergrid --run-config 'demo.sabotage=true' --stream | tee run_sabotage.log
```

Model access is injected by the Flower Runtime as `FLWR_RUNTIME_BASE_URL` and
`FLWR_RUNTIME_API_KEY`; there are no credentials in this repository. If Endeavor
does not answer within `model.budget_s`, the run switches once to
`model.fallback` and says so. `model_used` in the result always names the model
that actually ran. Model calls are capped at `MAX_MODEL_CALLS = 8`; a full run
uses 6, a fast run 1.

Inside one SuperGrid task the three institutions are separate objects, each
loading only its own folder and holding a mask secret the coordinator code never
receives. The isolation is enforced by code structure, not by separate machines,
since all three folders ship in the same FAB. Physically separate institutions
would need one Flower node each (the Deployment Runtime), which this project
does not use. `local/` is what shows the same code running as genuinely separate
processes.

Real runs of this app on SuperGrid, with their IDs, durations and FAB hashes,
are committed in [`docs/evidence/`](https://github.com/UtkarshMidha/PreventNet/blob/main/docs/evidence/) and shown in
[WALKTHROUGH.md](https://github.com/UtkarshMidha/PreventNet/blob/main/WALKTHROUGH.md#proof-this-runs-on-flower-supergrid).

## 📊 The federated risk model

The public [Pima Indians Diabetes dataset](https://archive.ics.uci.edu/) (US
National Institute of Diabetes and Digestive and Kidney Diseases) covers 768
women aged 21 and over, with no identifiers. 268 of them (34.9%) developed
diabetes within five years. It is split **vertically**: each institution gets
only the columns its function would plausibly hold, and the GP keeps the label.

| Measure | Value |
|---|---|
| People learned from | 768 (615 train / 153 test, seeded split) |
| Test AUC | 0.79 |
| Test accuracy | 75% |
| Default rounds | 30 (converges by ~15) |

Logistic regression is used because its score is a plain sum (*GP part + Lab
part + Pharmacy part*), so each institution can compute its own part locally and
only the parts are ever combined. Each one adds a pairwise additive mask from a
shared secret with single-use nonces; the masks cancel, so the coordinator
learns only the total. The label holder converts that total into residuals and
sends them **directly** to the other two, and the coordinator gets only
aggregate loss, accuracy and AUC. Entity alignment is checked blind, via a
digest of each institution's entity IDs.

None of the ten review questions (`egfr_band`, `metformin_active`, …) exist in
Pima. They are answered from each institution's own synthetic review records.
Pima's only role is to train this model, whose output enters the ledger as a
single fact.

## 🚧 Constraints

Worth stating plainly.

- **The records are synthetic.** The review records are invented; the Pima
  cohort is real but is a research dataset, not a clinical population. The
  pharmacy's medication column is simulated, because Pima has no medication
  data.
- **The masking is plain VFL, not cryptography.** Residuals leak label signal to
  the other two institutions, and the coordinator sees per-sample summed scores.
  Production VFL would add homomorphic encryption or DP noise there.
- **The risk-band cut points (0.15 / 0.40) are uncalibrated demo values**, and
  the Insights view is labelled *Dev* for that reason.
- **The contradiction rules are a fixed, hand-written set.** They catch the
  cross-source disagreements this catalog can express, not arbitrary tampering.
- **One SuperGrid task, not one node per institution.** See the SuperGrid
  section above.
- **Not a medical device.** The output is a suggestion for a human clinician to
  approve, and only ever a *review*, never a diagnosis or a treatment change.

## 🧪 Testing

```bash
uv run pytest -q     # 42 passed in ~4s
```

The whole suite is offline: no account, no API key, no network. It covers the
wire allowlist and ledger invariants, the policy and refusal path, the
verifier's citation and contradiction rules, the coordinator phases with a
stubbed model client, the AgentApp's ledger-only mode and model-budget fallback,
and an in-process run of all four local deployments on ephemeral ports (review,
federated training, sabotage escalation, approval refusal).

`uv run flwr build` packages the FAB and needs no account either.

## 📜 Provenance and license

Built by **[Utkarsh Midha (@UtkarshMidha)](https://github.com/UtkarshMidha)** for the
Flower Collaborative Agent Hackathon, Berlin, September 2026, where it placed
**2nd**. The original build spec is kept at [`docs/PRD.md`](https://github.com/UtkarshMidha/PreventNet/blob/main/docs/PRD.md), and a
longer written overview at
[`docs/PreventNet_Overview.pdf`](https://github.com/UtkarshMidha/PreventNet/blob/main/docs/PreventNet_Overview.pdf).

The Pima Indians Diabetes dataset is public; the source file is committed at
`scripts/source_data/pima-indians-diabetes.csv` and is not shipped in the FAB.
All review records are invented.

MIT. See [`LICENSE`](https://github.com/UtkarshMidha/PreventNet/blob/main/LICENSE).

---

<sub>**PreventNet**, powered by Flower · © 2026 **Utkarsh Midha ([@UtkarshMidha](https://github.com/UtkarshMidha))** · original repository: [github.com/UtkarshMidha/PreventNet](https://github.com/UtkarshMidha/PreventNet) · Flower Hub: [@utkarsh/preventnet](https://flower.ai/apps/utkarsh/preventnet)</sub>
