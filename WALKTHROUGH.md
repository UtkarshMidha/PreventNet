<!--
  PreventNet, powered by Flower: Walkthrough
  Author: Utkarsh Midha (@UtkarshMidha) · github.com/UtkarshMidha/PreventNet
  Every screenshot below was captured from a live run of this repository.
-->

# 🩺 PreventNet: Visual Walkthrough

> **Author: [@UtkarshMidha](https://github.com/UtkarshMidha)** · Flower Hub app: [`@utkarsh/preventnet`](https://flower.ai/apps/utkarsh/preventnet)

This is the demo, frame by frame, in place of a screen recording. Every image
below was captured from a real run of this repository: the four deployments
started with `uv run python -m local.launch`, the three sources answering from
their own records, the federated model really scoring the patient, and the
verifier really checking the result. Nothing here is a mockup.

Two stories are told in order:

1. **The happy path.** Three institutions that legally cannot share records
   surface a risk that none of them could see alone.
2. **The sabotage path.** One of those sources is compromised, and the system
   refuses to let the recommendation through.

---

## Part 1: the happy path

### 1. The care network

![The coordinator dashboard, showing three connected sources and today's patients](https://raw.githubusercontent.com/UtkarshMidha/PreventNet/main/docs/screenshots/01-overview.png)

The clinician's view. Three data sources are connected: **Health Diagnostics**
(the GP system), **Prescription Logs** (the pharmacy) and **Lab Reports**, each
running as its own server on its own port, holding its own records. The card for
each one says the same thing: *Records stay here.*

The coordinator agent has no access to any of them. It cannot read a record, a
note, or a name. It can only ask questions.

### 2. Starting a review

![The New Patient panel, with the review set to run on Flower SuperGrid](https://raw.githubusercontent.com/UtkarshMidha/PreventNet/main/docs/screenshots/02-new-review.png)

Pick a patient, and the review runs **on Flower SuperGrid**, the federated
runtime that executes the agent app. The clinician picks nothing else: no model,
no data selection, no query. The two switches are demo controls (speed, and the
compromised-source simulation used in Part 2).

*Walkthrough by @UtkarshMidha*

### 3. The agents go to work

![Agent progress: the coordinator plans questions, then each source answers in turn](https://raw.githubusercontent.com/UtkarshMidha/PreventNet/main/docs/screenshots/03-agents-working.png)

Every step is visible. The coordinator connects to Flower SuperGrid, plans which
questions to ask, and then queries each source **one at a time**:

| Step | What happened |
|---|---|
| Health Diagnostics is answering | **3 facts shared, 1 question declined** |
| Prescription Logs is answering | 3 facts shared |
| Lab Reports is answering | 3 facts shared |
| Estimating diabetes risk | Federated model returns a band, not a record |
| Double-checking every claim | The verifier runs over the ledger |

That **1 question declined** is the point. One question in the catalog asks the
GP system for an insurance marketing segment. Every source's disclosure policy
refuses the `marketing` purpose, so that refusal is exercised on *every single
run*, not only in tests.

### 4. What the coordinator concluded

![The coordinator agent's assessment, the clinician's decision cards, and the diabetes risk meter](https://raw.githubusercontent.com/UtkarshMidha/PreventNet/main/docs/screenshots/04-coordinator-assessment.png)

Four findings, each tagged with the sources it rests on:

- **Kidney function is moderately reduced and has declined over two years.** (Lab Reports)
- **A contrast scan is planned while blood pressure is high and kidney function is reduced.** (Health Diagnostics + Lab Reports)
- **NSAID painkillers are being taken and dispensed, and an interaction alert was raised alongside metformin.** (Health Diagnostics + Prescription Logs)
- **Blood sugar is in the prediabetic range and diabetes risk is moderate.** (Lab Reports + Risk Model)

> **Recommendation:** Book a medication and kidney safety review before the
> contrast scan: re-check kidney function and review NSAID and metformin use.

No single institution could have written that. The GP does not know what was
dispensed. The pharmacy does not know a contrast scan is booked. The lab knows
neither. The risk is in the *join*, which is exactly the join the law prevents.

### 5. What the coordinator was actually allowed to see

![The ten short answers the coordinator received, grouped by source](https://raw.githubusercontent.com/UtkarshMidha/PreventNet/main/docs/screenshots/05-what-coordinator-saw.png)

This is the coordinator's **entire world**: ten short answers. Categorical bands
and yes/no values (*Moderately reduced*, *Declining*, *Prediabetic range*,
*Yes*) plus one greyed-out line recording the question that was refused.

No free text. No dates. No drug names. No record IDs. The wire format is an
allowlist enforced in code (`preventnet/wire.py`), so a field added to a record
later cannot leak by being forgotten.

*Walkthrough by @UtkarshMidha*

### 6. The clinician decides

![The decision card after approval, showing the recommendation was approved](https://raw.githubusercontent.com/UtkarshMidha/PreventNet/main/docs/screenshots/06-approved.png)

Nothing is released automatically. The recommendation is a suggestion for a
human, and approval is recorded server-side. The system recommends a *review*,
never a diagnosis, never a treatment change.

### 7. The source keeps its own receipts

![The Prescription Logs server's own page, showing its records and what it disclosed](https://raw.githubusercontent.com/UtkarshMidha/PreventNet/main/docs/screenshots/07-source-page.png)

Each source has its own page that the coordinator can never see. Here the
pharmacy shows what it holds (10 patient records) and, in its own words, what it
gave away: *Shared 3 short answers and declined 0. No record left this server.*

Every disclosure is logged locally with its purpose (*Medication safety*,
*Kidney risk review*) and the patient it was about. The institution can audit
exactly what an agent ever learned from it.

---

## Part 2: the sabotage path

A system that only works when every participant is honest is not a safety
system. So one source gets compromised on purpose.

### 8. Arming the compromise

![The review panel with the compromised-source simulation switched on](https://raw.githubusercontent.com/UtkarshMidha/PreventNet/main/docs/screenshots/08-sabotage-armed.png)

With the switch on, the pharmacy's two safety-relevant answers are secretly
flipped to **"No"** *after* it computes them: no NSAID painkiller dispensed, no
interaction alert raised. From the coordinator's side this looks like a perfectly
normal, confident answer. Nothing is marked as suspicious.

### 9. The verifier catches it

![Two conflict alerts: the sources disagree about NSAID use and the interaction alert](https://raw.githubusercontent.com/UtkarshMidha/PreventNet/main/docs/screenshots/10-conflict-alert.png)

The verifier never trusts the agents' wording. It runs fixed contradiction rules
over the ledger itself, and both fire:

> **The Sources Disagree.** Health Diagnostics notes NSAID painkiller use, but
> Prescription Logs show none dispensed in the last 90 days. Check the records
> before acting.

> **The Sources Disagree.** Prescription Logs show metformin with no interaction
> alert, although Health Diagnostics notes NSAID use. Check the records before
> acting.

The contradiction is visible in the ledger itself:

![The ledger during the sabotage run: Health Diagnostics says the patient takes NSAIDs, Prescription Logs says none were dispensed](https://raw.githubusercontent.com/UtkarshMidha/PreventNet/main/docs/screenshots/12-conflict-facts.png)

Health Diagnostics: *Takes over-the-counter painkillers (NSAIDs)* → **Yes**.
Prescription Logs, two columns to the right: *NSAID painkiller dispensed* →
**No**, and *Drug-interaction alert raised* → **No**. Both cannot be true.

This is deterministic code, not a language model's judgement. It would fire the
same way if the compromise were a bug, a bad merge, or a stale record, so the
system does not have to guess at intent.

### 10. Approval is blocked

![The decision panel with Approve greyed out and Decline still available](https://raw.githubusercontent.com/UtkarshMidha/PreventNet/main/docs/screenshots/11-blocked-decision.png)

**Approve** is disabled and replaced with *"Resolve the disagreement first."*
The server enforces this too: a direct call to the approval endpoint is refused
with HTTP 409 for a conflicted review. Only **Decline** remains open.

A compromised source cannot turn itself into an approved clinical
recommendation. The worst it can do is stop one.

### 11. The compromised source incriminates itself

![The pharmacy's disclosure log showing the two flipped answers marked as tampered](https://raw.githubusercontent.com/UtkarshMidha/PreventNet/main/docs/screenshots/13-source-flipped.png)

The pharmacy's own log shows exactly which two answers were altered, flagged
*Tampered (demo)*, while the untouched third answer (metformin: Yes) is not
flagged. The tamper is confined to the two fields the demo flips, and the
institution can see it from its own side.

*Walkthrough by @UtkarshMidha*

---

## Part 3: under the hood

### 12. The shared risk model

![The Insights view: AUC 0.79, accuracy 75%, and the training curve over 30 rounds](https://raw.githubusercontent.com/UtkarshMidha/PreventNet/main/docs/screenshots/14-insights.png)

The diabetes-risk model is trained by **vertical federated learning**: the three
sources hold *different columns about the same people*, and the model is trained
across all of them without any of them sending data anywhere.

- **Ranking quality (AUC) 0.79** on 153 held-out people
- **Accuracy 75%**
- **768 people** learned from, across all three sources

Each source computes its own part of the score and adds a pairwise mask derived
from a secret the coordinator never receives. The masks cancel in the sum, so
the coordinator only ever learns the total. The label holder sends residuals
**directly** to the other two, peer to peer.

The model's output enters the review as exactly one more cited fact (*Diabetes
risk: moderate*), never as the decision-maker.

### 13. The closed question catalog

![The question catalog, showing each question's purpose and clinical rationale](https://raw.githubusercontent.com/UtkarshMidha/PreventNet/main/docs/screenshots/15-questions.png)

The coordinator cannot invent questions. It picks from a fixed catalog, and each
entry records where the answer comes from and why it may be asked. Anything
outside a source's permitted purposes is refused before a single record is read.

### 14. The decisions log

![The decisions view, recording the approved recommendation with a timestamp](https://raw.githubusercontent.com/UtkarshMidha/PreventNet/main/docs/screenshots/16-decisions.png)

Every clinician decision is recorded with its recommendation and timestamp.

---

## Proof this runs on Flower SuperGrid

The walkthrough above runs the four deployments locally so every step is
visible. The same agent app also runs, unchanged, as a Flower **AgentApp** on
**SuperGrid**. Here are the receipts.

### The app is published on Flower Hub

![The @utkarsh/preventnet app page on Flower Hub](https://raw.githubusercontent.com/UtkarshMidha/PreventNet/main/docs/screenshots/17-flower-hub.png)

Live at **[flower.ai/apps/utkarsh/preventnet](https://flower.ai/apps/utkarsh/preventnet)**.

### Real runs on SuperGrid

![Output of flwr ls supergrid, listing completed runs of @utkarsh/preventnet](https://raw.githubusercontent.com/UtkarshMidha/PreventNet/main/docs/screenshots/18-supergrid-runs.png)

A rendering of the real output of `uv run flwr ls supergrid`: every run of
`@utkarsh/preventnet` against the `@utkarsh/workspace` federation, with its
run ID, status and duration. The unedited output is committed:

| File | What it holds |
|---|---|
| [`docs/evidence/supergrid-runs.txt`](https://github.com/UtkarshMidha/PreventNet/blob/main/docs/evidence/supergrid-runs.txt) | Full run list, exactly as the CLI printed it |
| [`docs/evidence/supergrid-runs.json`](https://github.com/UtkarshMidha/PreventNet/blob/main/docs/evidence/supergrid-runs.json) | The same runs as JSON: FAB hash, federation ID, compute seconds |
| [`docs/evidence/run-6805811181906651753.json`](https://github.com/UtkarshMidha/PreventNet/blob/main/docs/evidence/run-6805811181906651753.json) | The happy-path run (1m 43s) |
| [`docs/evidence/run-1567274032615821010.json`](https://github.com/UtkarshMidha/PreventNet/blob/main/docs/evidence/run-1567274032615821010.json) | The sabotage run (2m 58s) |
| [`docs/evidence/run-5975355547609874959.json`](https://github.com/UtkarshMidha/PreventNet/blob/main/docs/evidence/run-5975355547609874959.json) | A full-mode run (4m 6s) |

Those two run IDs matter: the coordinator wording that ships in
`local/demo_responses.json` was **recorded from those exact Flower Endeavor
runs** on SuperGrid, which is what lets the local walkthrough finish in ~20
seconds instead of ~2 minutes while still showing what the real model wrote.

To run it live against SuperGrid yourself, see
[*Running on Flower SuperGrid*](https://github.com/UtkarshMidha/PreventNet/blob/main/README.md#-running-on-flower-supergrid-optional)
in the README. It needs a Flower account.

---

## How these screenshots were produced

Four servers started with `uv run python -m local.launch`, driven through
headless Chrome, captured at 2× and downscaled. Patient **P001** is the seeded
demo case, so the happy path and the sabotage path reproduce identically on any
checkout. The SuperGrid evidence files are unedited CLI output.

---

<sub>**PreventNet**, powered by Flower · Author: **Utkarsh Midha ([@UtkarshMidha](https://github.com/UtkarshMidha))** · 🥈 2nd place, Flower Collaborative Agent Hackathon, Berlin 2026 · MIT licensed. If you are reading this in a fork, the original lives at [github.com/UtkarshMidha/PreventNet](https://github.com/UtkarshMidha/PreventNet).</sub>
