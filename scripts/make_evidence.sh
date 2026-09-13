#!/usr/bin/env bash
#
# Regenerate everything in /evidence from scratch.
#
# Every run below uses --run-id so the evidence directories have meaningful,
# stable names, and --reset-target so each starts from identical application
# state. That is what makes the checked-in evidence reproducible rather than
# a snapshot of one lucky afternoon.
#
# Usage:
#   scripts/make_evidence.sh              # offline stand-in planner
#   PLANNER=groq scripts/make_evidence.sh # real LLM-driven discovery
#
set -euo pipefail

cd "$(dirname "$0")/.."

PY=${PY:-.venv/bin/python}
PLANNER=${PLANNER:-auto}
MERIDIAN=${MERIDIAN:-http://127.0.0.1:5075}
SUMMIT=${SUMMIT:-http://127.0.0.1:5076}
CUA="env PYTHONPATH=src $PY -m cua.cli"

PARAMS='{"operator_id":"svc_admin","passcode":"sandbox-only-pw","member_id":"100237","account_type":"Savings","initial_deposit":"50"}'

banner() { printf '\n\033[1m=== %s\033[0m\n' "$1"; }

for url in "$MERIDIAN" "$SUMMIT"; do
  if ! curl -sf "$url/_control/health" >/dev/null; then
    echo "The target app is not running at $url."
    echo "Start both tenants first:"
    echo "  $PY target_app/app.py --port 5075 --tenant meridian &"
    echo "  $PY target_app/app.py --port 5076 --tenant summit &"
    exit 1
  fi
done

# Preserve the hand-written index while clearing the runs it describes.
mkdir -p evidence artifacts
find evidence -mindepth 1 -maxdepth 1 ! -name README.md -exec rm -rf {} +
rm -rf artifacts
mkdir -p artifacts

# ---------------------------------------------------------------------------
banner "1/8  discovery -- an LLM drives the UI and we record what it learned"
# The irreversible commit pauses for a human; a scripted operator reviews and
# authorises it so this is reproducible. Interactively, use --operator console.
$CUA discover \
  --run-id 1_discovery \
  --goal "Sign in, look up member 100237, open a new Savings sub-account with an initial deposit of 50, and report the new account number" \
  --params "$PARAMS" \
  --capability-id open_member_subaccount \
  --description "Open a new {account_type} sub-account for member {member_id} with an opening deposit of {initial_deposit}, and return the new account number." \
  --planner "$PLANNER" \
  --operator script --operator-script config/operator_scripts/confirm_irreversible.json \
  --reset-target

banner "2/8  human review gate -- draft becomes approved"
$CUA approve --id open_member_subaccount --version 1 --yes

# ---------------------------------------------------------------------------
banner "3/8  replay -- deterministic, no model in the loop"
$CUA replay --run-id 2_replay_success --id open_member_subaccount \
  --params "$PARAMS" --operator none --reset-target

banner "4/8  business outcome -- 'no such member' is an answer, not a crash"
$CUA replay --run-id 3_replay_outcome_member_not_found --id open_member_subaccount \
  --params "${PARAMS/100237/999999}" --operator none --reset-target || true

banner "5/8  business outcome -- the operator lacks the entitlement"
$CUA replay --run-id 4_replay_outcome_not_authorized --id open_member_subaccount \
  --params "${PARAMS/svc_admin/teller01}" --operator none --reset-target || true

banner "6/8  recoverable -- the session expires mid-flow and we re-authenticate"
$CUA replay --run-id 5_replay_recovered_session_timeout --id open_member_subaccount \
  --params "$PARAMS" --inject session_timeout --operator none --reset-target

# ---------------------------------------------------------------------------
banner "7/8  hard failure -> human takes the live session -> automation resumes"
$CUA replay --run-id 6_replay_handoff_and_resume --id open_member_subaccount \
  --params "$PARAMS" --inject app_error --reset-target \
  --operator script --operator-script config/operator_scripts/recover_from_app_error.json

# ---------------------------------------------------------------------------
banner "8/8  cross-tenant -- the same artifact on a rebranded install"
$CUA replay --run-id 7_replay_cross_tenant_summit --id open_member_subaccount \
  --tenant summit \
  --params '{"operator_id":"svc_admin","passcode":"sandbox-only-pw","member_id":"100412","account_type":"Checking","initial_deposit":"125"}' \
  --operator none --reset-target

banner "done"
echo "Artifact: artifacts/open_member_subaccount.v1.json"
echo "Evidence: evidence/"
ls -1 evidence
