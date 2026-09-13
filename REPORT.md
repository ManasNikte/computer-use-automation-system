# Design write-up

## Architecture

A goal comes in, a model discovers how to satisfy it, and what it learned is
frozen into an artifact that production executes without the model.

```
             ┌────────── discovery (model in the loop) ──────────┐
  goal ─────▶│  AgentLoop ──▶ Planner ──▶ Surface ──▶ Recorder   │──▶ capability.json
             └───────────────────────────┬──────────────────────┘        │
                                         │                         human review
             ┌────────── replay (no model, ever) ──────┐                 │ (draft→approved)
  args ─────▶│  ReplayExecutor ──▶ Surface             │◀────────────────┘
             └──────────────┬──────────────────────────┘
                            │ stuck / irreversible
                            ▼
                    EscalationManager ──▶ operator channel (same live session)
```

Four boundaries carry the design.

**`Surface` is the load-bearing one.** Everything above it — the agent loop,
the recorder, the replay executor, the escalation manager — is written against
eight operations (`observe`, `resolve`, `click`, `fill`, `select`, `navigate`,
`read_labelled_value`, `screenshot`). None of them imports Playwright, knows
what a CSS selector is, or assumes a DOM exists. The vocabulary is deliberately
that of an **accessibility tree** — role plus accessible name — because that is
the one representation available on a modern web app, a 2003 frameset, *and* a
native desktop app. The test suite proves the seam is real rather than
aspirational: `tests/fakes.py` is an in-memory `Surface` with no browser behind
it, and the replay executor runs against it unmodified.

**The planner is the only place a model is called.** It receives the goal, the
visible text, and a numbered list of controls as role + name. It never sees
HTML, a selector, a frame, or a secret — parameters are referenced as
`{passcode}` and substituted by the executor after the decision is made, so a
prompt log cannot leak a credential. Locator synthesis happens in the recorder,
from addressing hints the model never reads. Providers are called over stdlib
HTTP rather than vendor SDKs; switching model is a flag, not an install.

**Replay cannot reach a model.** Not by policy — by construction. There is no
import path from `replay/` to `agent/planner.py`, and a test asserts it.

**Evidence is written through redaction in the sink.** `EvidenceRecorder.log()`
runs every payload through the redactor before serializing. A future caller who
logs raw page text cannot leak a tax ID by forgetting to redact, because there
is no path to the file that skips it.

**Trade-offs I made deliberately.** One process, synchronous, JSON files on
disk — the brief explicitly does not reward building queues and registries, and
artifacts are small, diffable things whose approval step is a human reading a
pull request. A database would make the review story worse and buy nothing at
this size; swapping in a real registry is a change to `artifact/store.py` alone.

The one place I spent complexity is the target application. I wrote a
deliberately hostile stand-in — a frameset, table layout, no test IDs, labels
supplied only by adjacent table cells, plus injectable session expiry,
interstitials, permission denials, validation errors and 500s — because a
system built against a clean demo site would be quietly wrong in ways that only
show up on a real core banking screen. Two concrete things fell out of that
choice that I would otherwise have shipped broken: the frameset's top-level URL
and `<title>` never change, so a naive `page.url` makes every checkpoint
trivially true; and `page.wait_for_load_state()` on the main frame returns
immediately when a *child* frame is navigating, so the agent reads the previous
screen and records a checkpoint for a page it never reached.

---

## Artifact schema

A capability serves three audiences at once, and every field earns its place
against all three: a **calling agent** needs a function signature, the **replay
engine** needs something executable, and a **human reviewer** — plausibly in
compliance — needs to see what it touches and what is irreversible.

```jsonc
{
  "schema_version": "cua.capability/v1",
  "id": "open_member_subaccount", "version": 1, "status": "approved",
  "description": "Open a new {account_type} sub-account for member {member_id}…",

  "target": { "vendor_product": "meridiancore",      // what it's recorded against
              "product_version_range": ">=8.0 <9.0",
              "surface": "web", "entry_path": "/login" },   // NOT a base URL

  "inputs":  [{ "name": "member_id", "type": "string", "required": true },
              { "name": "passcode",  "type": "string", "sensitive": true }],
  "outputs": [{ "name": "new_account_number", "type": "string",
                "redact_in_evidence": true }],

  "business_outcomes": [{ "name": "member_not_found", "terminal": true,
      "detect": { "kind": "text_present", "value": "No member matching that number" }}],

  "recovery_rules": [{ "name": "reauthenticate_on_session_expiry",
      "detect": { "kind": "text_present", "value": "Your session has expired" },
      "action": { "kind": "restart_from_step", "step_id": "@first" }, "max_attempts": 1 }],

  "steps": [{
    "id": "06_click_open", "action": "click", "risk": "reversible",
    "locator": { "strategies": [
      { "kind": "row_role_name", "role": "link", "name": "Open",
        "row": "{member_id}", "frame": "name=mainframe" },      // ← primary
      { "kind": "role_name", "role": "link", "name": "Open", "frame": "name=mainframe" },
      { "kind": "css",  "value": "a[href=\"/members/100237\"]", "frame": "name=mainframe" },
      { "kind": "text", "value": "Open", "frame": "name=mainframe" }]},
    "checkpoint": { "kind": "url_contains", "value": "/members/{member_id}" }
  }],

  "final_checkpoint": { "kind": "text_present", "value": "New Account Number" },
  "applies_to": ["*"],
  "overrides":  { "summit": { "base_path": "/servicing", "labels": { … }}},
  "provenance": { "discovered_by": "groq:openai/gpt-oss-120b", "human_intervened": true, … }
}
```

**Locators are ranked strategy lists, and the ranking is derived from evidence
rather than a fixed template.** A single selector is a single point of failure
that tells you nothing when it breaks. Look at `06_click_open` above: the
row-anchored strategy leads, and that is a *correctness* decision, not a
robustness one. In a results grid every row holds a link named "Open" — role +
name alone would silently open row one no matter which member was searched for.
Conversely, the recorder demotes `role_name` when the platform would not agree
the name is the accessible name: `web_probe.js` records *where a name came
from*, and a name recovered from an adjacent table cell (the dominant legacy
labelling convention) reads well to a human but will never resolve via
`get_by_role`, so those controls lead with the form-field attribute instead.

**Every step carries its own checkpoint and risk level.** Without per-step
checkpoints you discover a click silently failed four steps later with a
useless error. Without per-step risk, the safety layer would have to re-derive
intent at enforcement time. Checkpoints are *inferred* by diffing the surface
before and after each step: the path changed, or a value we supplied appeared
on screen, or the title changed. A step with no observable effect — typing into
a field — correctly gets no checkpoint rather than a fabricated one.

**Business outcomes and recovery rules are declared, not discovered.** A
happy-path run never sees "no such member", so a recorder that claimed to have
found it would be lying. They come from a per-application library
(`config/outcomes.meridiancore.json`) — which is also how it works in practice,
since these are properties of the *application*, shared by every capability
recorded against it, and they belong to a contract that needs human review
regardless.

**Nothing sensitive is in here.** Values for `sensitive` params are never
written, only referenced by name; there is no model transcript, no credential,
no member data. `provenance` is kept separate from the flow so the capability
stays reviewable and so re-discovering it doesn't churn the contract.

**`base_url` is deliberately absent from `target`.** Baking a hostname into a
capability is the single most common thing that forces a re-recording per
tenant.

---

## Determinism & error handling

Replay is deterministic because the step sequence is fixed by the artifact,
locator resolution is ordered and total, and **no wait is a bare sleep tied to
wall-clock timing** — every wait is either "until the surface settles" or a
bounded retry. A test runs the same capability five times and asserts the
action sequence and outputs are byte-identical.

Getting "the surface settled" right was most of the work. Clicking a submit
button in a frameset navigates a child frame; the naive wait returns
immediately and the next observation reads the *previous* screen. The fix
separates two questions that a flat timeout conflates: *is anything happening?*
(watch for a navigation **request**, which fires immediately) and *how long am
I willing to wait for it?* (once a request is in flight, wait patiently for the
commit). That's what makes it both fast on a click that does nothing and
patient with a back end that takes five seconds — a flat interval has to be one
or the other.

The result contract makes the three categories mutually exclusive, because
collapsing them is the mistake the brief singles out:

| result | meaning | caller does |
|---|---|---|
| `SUCCESS` | flow completed, final checkpoint verified | reads `outputs` |
| `BUSINESS_OUTCOME` | a declared, legitimate non-happy-path answer | handles it as **data** |
| `HARD_FAILURE` | the automation could not proceed | debugs from step id / expected / observed |
| `POLICY_BLOCKED` | a guardrail declined | nothing went wrong |
| `ESCALATED` | handed to a human, with resolution | — |

Recoverable conditions are deliberately **not** a status. Dismissing an
interstitial or re-authenticating is something the executor handled; the run
continues and still ends in one of the above. They are recorded in
`recoveries[]` because a capability that silently re-authenticates on every run
is telling you something, and you only find out if you count.

**Ordering is the whole design.** Per step, in this order:

1. **Check declared business outcomes — before acting.** A "no such member"
   banner produced by the *previous* step must be read as an answer, not as
   this step's locator failing. This single ordering decision is what keeps
   `member_not_found` from surfacing as a stack trace.
2. **Check declared recovery conditions**, bounded by each rule's attempt cap.
3. **Enforce policy** — allowlist, action type, risk gate.
4. **Resolve** through the ranked strategies, recording which rank won.
5. **Act**, with bounded retry on timeout.
6. **Verify the checkpoint.** On failure, re-check business outcomes *and*
   recovery rules before declaring breakage — a failed checkpoint is the most
   informative moment to reclassify, and it is exactly how session expiry
   presents.

Two safety properties inside recovery are worth calling out. `restart_from_step`
re-runs a prefix to restore session state, and it **refuses if any step in that
prefix is irreversible** — replaying a commit to recover from a timeout would
post the transaction twice, so the recovery would cause a worse failure than
the one it was fixing. And when an operator reports they finished the work
manually, the executor still verifies the capability's own `final_checkpoint`
before reporting success: the contract is the contract regardless of who
executed the steps.

**On UI drift** (secondary, as the brief notes): a step that resolves via a
fallback strategy still succeeds, but emits a `DriftSignal`. That is the early
warning — the capability is now running on its backup, one app upgrade from
breaking. `drift == 0` on the cross-tenant run in `evidence/` is what proves
the tenant override genuinely relabelled the locators rather than the run
limping along on attribute fallbacks that happen to be identical.

---

## Heterogeneity & multi-tenant

**Surfaces.** The seam is `Surface`: perception and action are surface-specific,
the recorded flow is not. A `WindowsUiaSurface` yielding
`Control(role="button", name="Submit Request", hints={"automation_id": …})`
from a UIA tree drops in without the artifact schema changing shape, because
role + accessible name means the same thing on a UIA tree as on a DOM. Three
things in the current design exist specifically to keep that door open:
`read_labelled_value` is modelled as one portable operation ("the value next to
label X") rather than a table XPath, because on desktop that's a sibling Text
element; `hints` is an open dict so `automation_id` needs no schema change; and
`frame` in a locator strategy generalises to a window/view path.

For a surface with *no* structural access at all — Citrix, a terminal emulator,
a remote desktop — the same interface holds with a screenshot-plus-OCR
implementation producing `Control(role, name, hints={"bbox": …})`. The artifact
would not change; only the confidence you place in it would, which is what the
drift signal and the approval gate are for.

**Tenants.** A capability is recorded once against a *vendor product* and
stored once; a tenant entry supplies only the deltas. Three kinds cover
essentially everything that differs between two institutions running the same
software:

- **`base_path`** — the install is mounted under a different route prefix.
- **`labels`** — the same control is captioned differently. Because a rebrand
  renames the control *everywhere at once*, one substitution map fixes every
  locator, checkpoint and extraction label simultaneously. That is why this is
  a cheap, high-leverage override rather than a per-step patch.
- **`steps`** — per-step patches for genuine structural differences,
  deliberately last-resort.

`evidence/7_replay_cross_tenant_summit` is this working: the artifact recorded
against Meridian Credit Union runs unmodified against Summit Federal Credit
Union — different route prefix, seven different control captions — with
`drift=0`.

**Drift detection across tenants** is the honest weak point, and the design
gives two signals rather than a solution. First, `DriftSignal` tells you *which*
tenant started falling back to a secondary locator and on which step — the
early warning that one institution has upgraded. Second, `product_version_range`
on `target` plus `product_version` per tenant means a tenant outside the
declared range is a known-unknown rather than a surprise. What I have not built
is the thing that closes the loop: a scheduled canary replay per tenant with a
stability score, gating unattended use. That is the first thing I would build
next, and the data model already carries what it needs.

The escape valve is intentional: if a tenant accumulates more than a couple of
step patches, that is the signal it is really a different product version and
should get its own recording rather than an ever-growing patch set.

---

## Escalation & handoff

**Detecting stuck.** Four triggers, converging on one manager:

1. the planner declares `stuck` during discovery;
2. **no progress** — the planner proposes the *same action on the same screen*
   three times running, or three consecutive actions fail to execute, or the
   step budget is exhausted;
3. replay raises a hard failure — no strategy resolved, or a checkpoint failed
   and the state matches no declared outcome;
4. policy raises `ConfirmationRequired` for an irreversible action.

The no-progress signal is worth a note: my first version compared the screen
and its control set across steps, which fires immediately and wrongly, because
typing into a form legitimately changes neither. The thing that actually means
"the agent is looping" is a *repeated decision*.

Only #3 and #4 can occur in production replay. That is deliberate — the
production path escalates on mechanical failure or authorisation, never because
a model was uncertain, because no model is there to be uncertain.

**The control-transfer model** is shaped by one hard constraint: the surface
has exactly one owning thread. Playwright's sync API is not thread-safe, and
neither is any OS-level automation API. So "hand control to a human" cannot
mean letting an HTTP handler call `page.click()`. It means the owning thread
keeps driving the surface but starts taking its instructions from a person:

```
automation ──escalate──▶ operator ──hand back──▶ automation
     │                       │                        │
planner/artifact        command queue          artifact resumes
drives the surface     drives the surface      at the failed step
```

A `SessionLease` records who holds it, why, and since when, and emits an event
on every transition — so the evidence log shows exactly which actions in a run
were taken by a machine and which by a person, which is the audit question a
bank will actually ask. `assert_automation()` on the agent's action path turns
any future race over the live session into a loud failure rather than a silent
one.

**Routing.** An `InterventionRequest` carries the capability, step id, live
location, expected-vs-observed, a screenshot and the flattened control tree —
captured *before* control transfers, so the evidence shows the state the
automation actually got stuck in rather than whatever the operator has since
changed it to.

**Taking control.** Four channels, all interchangeable because all of them call
the same executor on the owning thread: a terminal REPL, a **remote web console**
(live view of the session plus a command form — the `_Bridge` marshals commands
from the HTTP thread onto the owning thread, which is the concrete expression
of the constraint above), a **takeover** mode that hands the actual browser
window to the person sitting at it, and a scripted channel so the escalation
path is testable and the evidence reproducible.

**Handing back.** `resume` retries the failed step on the same session; `done`
means the operator finished the work (still checked against the final
checkpoint); `abort` fails the run. One human retry per step — someone who
hands back a still-broken session should not be asked about it forever. Every
operator action is logged individually, and a human-assisted *discovery* run
still becomes an artifact, flagged `human_intervened: true` so a reviewer is
never shown it as machine-discovered.

`evidence/6_replay_handoff_and_resume` is the full cycle. The operator does
four steps rather than twelve, because control transferred on a live session
rather than a fresh one — which is the entire point.

---

## Safety

Three orthogonal gates, checked at different moments.

**Allowlist.** Permitted origins, allowed path globs, denied path globs (deny
wins), and permitted action types. Enforced before every navigation *and*
re-checked after every action, because a click can navigate somewhere a `goto`
check never saw — and across **every frame**, since a hidden frame pointed at
an unapproved host is exactly what a guardrail is for. The fault-injection
control plane lives on an allowed host and is on the denied list, so no
capability can reach it.

**Risk.** Classified by *reversibility*, and read from the **surface**, not
from the model's prose — letting a model's self-description decide whether
something is irreversible means a model that says "just a small click" bypasses
the guardrail. The signals used are the ones the application puts on screen: a
commit-shaped control alongside an explicit irreversibility warning.

The gate differs by mode, and the reasoning is the point:

- **Discovery**: an irreversible step raises `ConfirmationRequired` → a human
  intervention. A model may *propose* opening an account; it does not get to do
  it unsupervised.
- **Replay**: an irreversible step runs only if the capability is `approved` —
  a human review gate on the artifact itself.

So **every irreversible action has a human behind it**, either live at
discovery time or earlier via approval. I chose confirm-and-gate over "block"
(which would make the system unable to record the flows that matter most) and
over "flag and proceed" (which is not a guardrail).

**Data.** Two complementary redaction mechanisms, because either alone is
insufficient. Known-value redaction masks anything bound to a `sensitive`
param — the only thing that protects a passcode, since a passcode looks like
nothing in particular. Pattern redaction catches SSN/TIN, card and account
numbers, emails and phones — which is what protects data we never passed in but
*scraped off the screen*, the realistic leak path. Both run in the logging sink,
not at call sites. The `structure_snapshot` on failure is the control tree, not
raw HTML, precisely because raw HTML from a servicing screen is full of member
names and balances. Outputs marked `redact_in_evidence` are returned to the
caller but masked on disk. Tests assert that no passcode and no tax ID appears
anywhere in `evidence/` or `artifacts/`.

**Limits, stated plainly.** Pattern redaction cannot recognise a member *name* —
"Dana Whitfield" is in the evidence logs, and catching that needs either NER or
a per-app field-level allowlist, of which the second is the right answer.
**Screenshots are unredacted bitmaps** and are the largest real exposure here;
production would need field-level masking before capture or OCR-and-blur after.
The allowlist is per-run configuration, not per-capability, so a capability
cannot yet declare the narrower policy it actually needs. And an operator
holding the lease is still bound by the allowlist, which is arguably wrong in a
genuine emergency — the escape hatch today is `--operator takeover`, which the
system does not mediate at all.

---

## Cuts

**Cut deliberately, with the seam left real:**

- **Desktop and OCR surfaces.** `Surface` has one implementation. The interface
  and its vocabulary were designed against the desktop case, and the in-memory
  fake in the tests demonstrates a second implementation works, but I did not
  write a UIA backend.
- **A real co-browsing console.** The web operator console is a screenshot
  refreshed on a timer plus a command form. The two properties that matter are
  real — the operator acts on the actual live session, and control genuinely
  transfers and returns. Production would change the *transport* (CDP or WebRTC
  screen streaming with input forwarding), not the control-transfer model.
- **Business outcomes proposed by the model.** They come from a hand-written
  per-app library. Having the model propose candidates post-hoc for human
  review would cut the per-app setup cost, and is a natural extension.
- **Multi-tenant infrastructure.** Two tenants in a JSON file. The brief is
  explicit that building the plumbing is not rewarded; making the abstractions
  survive it is.
- **Concurrency, queueing, a capability registry service.** One process, files
  on disk.

**What I would build next, in order:**

1. **Per-tenant canary replays with a stability score**, gating unattended use.
   This is the missing half of the drift story: `DriftSignal` tells you a
   fallback was used, but nothing currently *watches* for that across tenants.
   The data model already carries what it needs.
2. **Screenshot redaction.** The largest real data-exposure gap.
3. **Per-capability policy.** Let a read-only capability declare that it needs
   only `navigate`/`click`/`extract` and a narrow path glob, so a mis-recorded
   `fill` is refused rather than executed. The `Policy` object is already data;
   it just needs to be attachable to an artifact.
4. **Bounded LLM recovery for a single step** on replay failure — policy-checked,
   never open-ended, recorded as evidence, and gated behind the same approval
   state. The escalation path is the right place to hang it: it would become a
   channel that tries once before routing to a human.
5. **Codegen from an artifact** — emitting a Playwright test or page object —
   which is nearly free given the schema and would give integration teams
   something to review in a language they already read.
