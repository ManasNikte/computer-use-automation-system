# Evidence

Seven runs covering the whole thread: a goal → an LLM-driven discovery run →
a saved capability → deterministic replays → a human taking over the live
session and handing it back.

Everything here was produced by `scripts/make_evidence.sh`, which resets the
target application before each run, so it is reproducible rather than a
snapshot of one lucky afternoon.

Each directory contains:

| file | what it is |
|---|---|
| `events.jsonl` | append-only structured log; one JSON object per event |
| `*.png` | screenshots, captured at every failure and every control handoff |
| `*.controls.json` | the flattened control tree at that moment — what a locator *could* have matched |
| `replay_result.json` / `recorded_capability.json` | the machine-readable result / the artifact produced |

All of it is routed through redaction on the way to disk. You will not find
the operator passcode or a member's tax ID anywhere in this directory, and
`tests/test_end_to_end.py` asserts exactly that.

---

### 1_discovery — the model discovers the flow

`status=completed`, 12 steps, output `new_account_number`.

Worth reading in order:

* `planner_decision` events — what the agent decided at each screen and why.
  The planner only ever sees role + accessible name, never a selector.
* At step 10 the agent proposes clicking **Submit Request**. The policy
  classifies it irreversible and refuses to let it through unattended:
  `confirmation_required` → `intervention_raised` → `control_transfer`
  (automation → operator) → `confirmation_granted`. A human authorised the
  commit; the automation performed it.
* `intervention_itv_*.png` is the screen at the moment it paused.
* `recorded_capability.json` is what came out.

The resulting artifact is flagged `human_intervened: true` in its provenance,
so a reviewer is never shown a human-assisted recording as if the model had
found the whole flow unaided.

### 2_replay_success — the production path

`SUCCESS (12/12 steps, ~0.9s)`, no model involved, `drift=0`.

Compare the step count and duration against the discovery run: same work,
none of the reasoning. `new_account_number` is returned to the caller but
appears as `[REDACTED]` in the log, because the artifact declares that output
as regulated data.

### 3_replay_outcome_member_not_found — an answer, not a crash

`BUSINESS OUTCOME (5/12 steps)`.

The search returns no rows. The capability *declared* `member_not_found` as a
possible result, so this is reported as structured data with `failure: null`.
The run stops at step 5 because there is nothing left to do — not because
anything broke.

This is the case the brief singles out as the most commonly botched, and the
ordering in `replay/executor.py` exists specifically to get it right: business
outcomes are checked **before** the next step tries to find a control that was
never going to be there.

### 4_replay_outcome_not_authorized — a permission denial

`BUSINESS OUTCOME (7/12 steps)`.

Same artifact, signed in as `teller01`, who lacks the `subaccount.open`
entitlement. The application renders the button for everyone and refuses
server-side — as these systems generally do — so the refusal arrives as a
screen mid-flow. The caller is told to route to a supervisor.

### 5_replay_recovered_session_timeout — recovered without a human

`SUCCESS (12/12 steps)`, `recoveries=1`.

The session expires immediately after sign-in. The checkpoint at step 3 fails,
and before declaring breakage the executor asks whether this matches a
declared recoverable condition. It does:
`recovery_triggered` → `recovery_restart` (re-runs the two-step sign-in
prefix) → `recovered_after_failure` → the run carries on and completes.

Note what does *not* happen: nobody is paged. Note also
`recovery_refused` in the test suite — the same rule is refused outright if the
prefix it would re-run contains an irreversible step, because replaying a
commit to recover from a timeout would post the transaction twice.

### 6_replay_handoff_and_resume — the full human-in-the-loop cycle

`SUCCESS (12/12 steps)`, `human: resumed (4 actions)`. **The most interesting
run here.**

1. The back end returns `MRDN-5001` on the member record. There is nothing on
   the error page to click, so no locator strategy resolves.
2. `hard_failure` → `hard_failure_*.png` + `.controls.json` captured *before*
   control moves, so the evidence shows the state the automation actually got
   stuck in.
3. `intervention_raised` carries the capability, the step id, the live
   location, expected-vs-observed, and a screenshot — enough to act on cold.
4. `control_transfer` automation → operator. The human then works the **same
   live session**: navigates back into the console, re-runs the lookup,
   reopens the record. Each `operator_action` is logged individually.
5. `control_transfer` operator → automation, `resuming_after_handoff`, and
   replay retries the step it failed on — sign-in and all other state intact.
   It finishes the remaining five steps and returns the output.

The operator did four steps, not twelve, because control transferred on a live
session rather than a fresh one.

### 7_replay_cross_tenant_summit — one recording, two institutions

`SUCCESS (12/12 steps)`, `drift=0`.

The *same artifact*, recorded against Meridian Credit Union, executed against
Summit Federal Credit Union: a different install of the same vendor product,
rebranded and mounted under `/servicing`. Seven control captions differ
("Member Number" → "Account Holder ID", "Submit Request" → "Submit", …), and
the routes are prefixed.

`drift=0` is the part that matters. Every step resolved on its *primary*
locator strategy, which means the tenant override genuinely relabelled the
locators rather than the run quietly limping along on `name=` attribute
fallbacks that happen to be identical across tenants.

---

## Regenerating

```bash
python target_app/app.py --port 5075 --tenant meridian &
python target_app/app.py --port 5076 --tenant summit &
scripts/make_evidence.sh                 # offline stand-in planner
PLANNER=groq scripts/make_evidence.sh    # real LLM-driven discovery
```

The discovery run checked in here used the offline `sandbox-rules:v1` planner
so that this evidence regenerates without an API key. That stand-in is a rule
set reading the live observation, **not** a model pretending to be one — see
the note in `README.md` under *Running discovery with a real LLM*. Replay is
unaffected either way: no model is reachable from that path by construction.
