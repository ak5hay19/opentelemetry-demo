# Evaluation note (Day 1)

Skeleton. The tables are filled from the real sweep results after the P7 harness has run. Nothing below is a result yet.

## How it was run

- Each incident: inject with `dev/faultctl`, wait for it to settle, run the agent as a separate process that is given only the generic symptom, save the run folder, reset, wait for a clean baseline.
- The injected incident's name never enters the agent's context. It is read afterwards from `tests/ground_truth.md` to mark right or wrong.
- Agent model, step budget, token budget and settle time: _fill in from the run manifest_.
- Date and commit of the run: _fill in_.

## Results

| Incident | Type | Ground truth component | Agent's component | Stated confidence | Verdict | Steps | Tokens |
|---|---|---|---|---|---|---|---|
| _fill in_ | container / flag / infrastructure | | | | right / wrong / unknown | | |

Verdicts: **right** (component matches), **wrong** (component differs), **unknown** (the agent said it could not tell). A wrong answer with high stated confidence is counted separately as **confidently wrong**.

## Confidence calibration

| Stated confidence | Diagnoses | Right | Wrong |
|---|---|---|---|
| 0.0 to 0.4 | | | |
| 0.4 to 0.7 | | | |
| 0.7 to 1.0 | | | |

## What went right and why

_One short paragraph per correct diagnosis: which signals the agent followed and which ledger IDs carried the case._

## What went wrong and why

_One short paragraph per wrong or unknown diagnosis: what the agent saw, what it missed or misread, and whether the cause was the evidence, the prompt or the budget._

## Leak check

- `tests/test_leak.py` on every saved run folder: _pass / fail_.

## Known gaps

- Candidates are validated only once P6 is merged (exact-quote checks, confidence caps, escalation). Say here whether these results are before or after it.
- _Anything else the sweep showed, for example incidents not run._
