# Computer-Use Automation System

An LLM figures out how to operate a legacy back-office application once. That
run is recorded as a typed, versioned **capability artifact**. Production
replays the artifact **deterministically, with no model in the decision loop**
— and when it can't safely proceed, a human takes over the *same live session*
and hands it back.

Built for the environment described in the brief: US bank and credit-union
back-office systems with no API, stable UIs, real runtime error states, and
hundreds of tenants running the same vendor software configured differently.

**The design write-up is in [REPORT.md](REPORT.md).** Worked examples of every
path — success, business outcomes, recovery, human handoff, cross-tenant — are
in [evidence/README.md](evidence/README.md).

---

## What's here

```
target_app/          "MeridianCore Servicing" — a deliberately legacy stand-in
                     for a core banking console: frameset, table layout, no
                     test IDs, injectable runtime faults, two tenant brands
src/cua/
  surfaces/          THE SEAM. Surface protocol + Playwright web implementation.
                     Nothing above this line knows what a DOM is.
  agent/             observe → decide → act loop; multi-provider LLM planner
  artifact/          capability schema, recorder (trace → artifact), store, catalog
  replay/            deterministic executor, error taxonomy, result contract
  safety/            allowlist + risk gating, redaction
  escalation/        session lease, intervention routing, four operator channels
  evidence/          structured logging with redaction in the sink
  cli.py             discover / approve / replay / catalog / invoke / inspect
config/              per-app outcome library, tenant registry, operator scripts
tests/               97 tests; 18 of them drive a real browser end to end
evidence/            checked-in runs covering every path
```

## Setup

Python 3.9+ and about a minute.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Runtime dependencies are Flask and Playwright, nothing else — the LLM
providers are called over stdlib HTTP rather than through vendor SDKs, so
switching model is a flag rather than an install.

## Demo path

**1. Start the target application** (two tenants — same vendor product,
different branding):

```bash
python target_app/app.py --port 5075 --tenant meridian &
python target_app/app.py --port 5076 --tenant summit &
```

Open <http://127.0.0.1:5075/login> to see what the agent is up against.
Sign in as `svc_admin` / `sandbox-only-pw`. Everything in it is synthetic.

**2. Discover** — an LLM drives the real UI and we record what it learned:

```bash
export PYTHONPATH=src

python -m cua.cli discover \
  --goal "Sign in, look up member 100237, open a new Savings sub-account with an initial deposit of 50, and report the new account number" \
  --params '{"operator_id":"svc_admin","passcode":"sandbox-only-pw","member_id":"100237","account_type":"Savings","initial_deposit":"50"}' \
  --capability-id open_member_subaccount \
  --description "Open a new {account_type} sub-account for member {member_id} with an opening deposit of {initial_deposit}, and return the new account number." \
  --operator script --operator-script config/operator_scripts/confirm_irreversible.json \
  --reset-target
```

Add `--headed` to watch it. It will pause partway through: submitting the
account opening is irreversible, and policy does not let a model do that
unsupervised. The scripted operator above reviews and authorises it; swap in
`--operator console` to do it yourself.

Out comes `artifacts/open_member_subaccount.v1.json`. Read it — it's the
centre of the design.

**3. Approve** — the human review gate. Until this, the capability cannot
execute its irreversible step unattended:

```bash
python -m cua.cli approve --id open_member_subaccount --version 1
```

**4. Replay** — deterministic, no LLM:

```bash
python -m cua.cli replay --id open_member_subaccount \
  --params '{"operator_id":"svc_admin","passcode":"sandbox-only-pw","member_id":"100237","account_type":"Savings","initial_deposit":"50"}' \
  --reset-target
```

```
SUCCESS  (12/12 steps, 0.9s)
  output   new_account_number = SV-100237-02
```

### Exercising the interesting parts

Every command below is the same artifact against a different runtime reality.

```bash
# A legitimate business answer, NOT a failure. Exit code 3.
python -m cua.cli replay --id open_member_subaccount --reset-target \
  --params '{"operator_id":"svc_admin","passcode":"sandbox-only-pw","member_id":"999999","account_type":"Savings","initial_deposit":"50"}'
#   BUSINESS OUTCOME  member_not_found

# The operator lacks the entitlement — again an answer, not a crash.
python -m cua.cli replay --id open_member_subaccount --reset-target \
  --params '{"operator_id":"teller01","passcode":"sandbox-only-pw","member_id":"100237","account_type":"Savings","initial_deposit":"50"}'
#   BUSINESS OUTCOME  not_authorized

# Validation error caught by the contract before the browser even opens.
python -m cua.cli replay --id open_member_subaccount --reset-target \
  --params '{"operator_id":"svc_admin","passcode":"sandbox-only-pw","member_id":"100237","account_type":"Savings","initial_deposit":"abc"}'
#   HARD FAILURE  contract_violation

# The session expires mid-flow; replay re-authenticates and carries on.
python -m cua.cli replay --id open_member_subaccount --reset-target \
  --inject session_timeout \
  --params '{"operator_id":"svc_admin","passcode":"sandbox-only-pw","member_id":"100237","account_type":"Savings","initial_deposit":"50"}'
#   SUCCESS  recovery reauthenticate_on_session_expiry
```

**The human-in-the-loop cycle** — the back end errors, replay gets genuinely
stuck, a person takes the live session, fixes it, hands back, and the
automation finishes the job:

```bash
python -m cua.cli replay --id open_member_subaccount --reset-target \
  --inject app_error \
  --params '{"operator_id":"svc_admin","passcode":"sandbox-only-pw","member_id":"100237","account_type":"Savings","initial_deposit":"50"}' \
  --operator script --operator-script config/operator_scripts/recover_from_app_error.json
#   SUCCESS  (12/12 steps)   human  resumed (4 action(s))
```

To do the taking-over yourself, pick an operator channel:

| flag | what happens |
|---|---|
| `--operator console` | a terminal REPL driving the live session (`ls`, `click X`, `fill X = Y`, `resume`, `done`, `abort`) |
| `--operator web` | a remote operator console at <http://127.0.0.1:5099> with a live view of the session and a command form |
| `--operator takeover` | with `--headed`, the actual browser window is handed to you; press Enter to hand back |
| `--operator script` | replays a recorded operator session — used for reproducible evidence |

Try the web console — it's the one that shows the control-transfer model
properly:

```bash
python -m cua.cli replay --id open_member_subaccount --reset-target \
  --inject app_error --operator web --open-console --headed \
  --params '{"operator_id":"svc_admin","passcode":"sandbox-only-pw","member_id":"100237","account_type":"Savings","initial_deposit":"50"}'
```

**Cross-tenant reuse** — the same recording against a rebranded install of the
same vendor product, with no re-recording:

```bash
python -m cua.cli inspect --id open_member_subaccount --tenant summit   # see the deltas applied
python -m cua.cli replay  --id open_member_subaccount --tenant summit --reset-target \
  --params '{"operator_id":"svc_admin","passcode":"sandbox-only-pw","member_id":"100412","account_type":"Checking","initial_deposit":"125"}'
#   SUCCESS  output new_account_number = CK-100412-02   drift=0
```

**The agent-facing catalog** — saved artifacts as typed, callable tools:

```bash
python -m cua.cli catalog            # human view
python -m cua.cli catalog --json     # JSON-Schema tool definitions

python -m cua.cli invoke open_member_subaccount --reset-target \
  --args '{"operator_id":"svc_admin","passcode":"sandbox-only-pw","member_id":"100412","account_type":"Certificate","initial_deposit":"900"}'
```

`invoke` is JSON in, JSON out, and only sees **approved** capabilities — the
view a production agent would have.

Exit codes are part of that contract: `0` success, `3` business outcome
(a valid answer), `1` failure.

## Running discovery with a real LLM

Discovery calls a model; replay never does. Set any one of these and the
`--planner auto` default picks it up:

| provider | environment variable | get a key |
|---|---|---|
| **Groq** (recommended, free, no card) | `GROQ_API_KEY` | <https://console.groq.com/keys> |
| Google Gemini (free tier) | `GEMINI_API_KEY` | <https://aistudio.google.com/apikey> |
| Anthropic | `ANTHROPIC_API_KEY` | <https://console.anthropic.com> |
| OpenAI / Together / OpenRouter | `OPENAI_API_KEY` etc. | — |

Either export it, or put it in a `.env` at the repo root (gitignored, and read
automatically — an explicit `export` overrides it):

```bash
cp .env.example .env && $EDITOR .env       # set GROQ_API_KEY=gsk_...
#   …or…
export GROQ_API_KEY=gsk_...

python -m cua.cli discover ...             # --planner auto picks it up
```

Check it landed, and see what your key can actually reach:

```bash
python -m cua.cli models
```

Provider catalogues churn — a retired default model returns an HTTP 404 that
reads exactly like an auth failure, so this answers both questions at once.
Override with `--model`. Keys are read from the environment and never written
to the repo, to an artifact, or to a log.

**With no key set**, discovery falls back to `--planner sandbox`: a small
deterministic rule set that reads the same live observation a model gets
(matching on control role and accessible name) and picks the next control.
It is **not a model and does not imitate one** — it is bound to this sandbox
app's vocabulary and will not generalise to another application. It exists so
that a reviewer without an API key can still run the full pipeline and get a
real artifact out. The checked-in evidence was generated with it, and says so.

There is also `--planner recorded --from evidence/1_discovery/events.jsonl`,
which replays the decisions a real model made in a previous run — a
deterministic, offline, *genuine* LLM session rather than an imitation of one.

## Tests

```bash
pytest                  # 97 tests, ~33s
pytest -m "not e2e"     # 79 unit tests, ~1s (no browser)
```

The end-to-end tests launch a real Chromium against a private instance of the
target app and assert the whole vertical slice: that discovery produces a
replayable artifact, that the grid locator is parameterized rather than pinned
to one record, that five distinct runtime conditions are classified as
business outcomes, that session expiry recovers, that an app error produces a
debuggable hard failure with a screenshot, that a draft capability refuses to
commit unattended, and that no credential or tax ID reaches disk.

## Fault injection

The target app exposes a control plane so each error path is reproducible:

```bash
python -m cua.cli replay ... --inject slow,interstitial,session_timeout,app_error
```

`/_control/*` is on the policy's **denied** path list, so no capability can
reach it even though it lives on an allowed host.

## Ground rules

No real credentials, no real PII, no third-party terms of service at risk:
everything runs against a local, disposable Flask app with a synthetic member
table. No secrets in the repo — provider keys come from the environment.
# -computer-use-automation-system-using-groq-llm
# -computer-use-automation-system-using-groq-llm
