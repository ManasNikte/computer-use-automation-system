"""End-to-end: a real browser, a real application, the whole vertical slice.

Everything else in this suite runs against fakes so it stays fast. This file
exists to prove the seams actually meet: that the accessibility-tree
extraction works on a frameset, that a recorded artifact replays without a
model, that the error taxonomy classifies real application states, and that
credentials do not reach disk.

Slow by nature (it launches Chromium), so it is marked and can be deselected:

    pytest -m "not e2e"
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

import pytest

from cua.agent.loop import AgentLoop
from cua.agent.planner import SandboxPlanner
from cua.artifact.recorder import Recorder, load_outcome_library
from cua.artifact.schema import RiskLevel, resolve_for_tenant
from cua.artifact.store import CapabilityCatalog, CapabilityStore
from cua.replay.executor import ReplayExecutor
from cua.replay.result import BUSINESS_OUTCOME, SUCCESS
from cua.session import control, run_session
from conftest import REPO_ROOT

pytestmark = pytest.mark.e2e

GOAL = ("Sign in, look up the member, open a new sub-account with the requested "
        "deposit, and report the new account number")
PARAMS = {
    "operator_id": "svc_admin",
    "passcode": "sandbox-only-pw",
    "member_id": "100237",
    "account_type": "Savings",
    "initial_deposit": "50",
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def target_app():
    """Start a private instance of the target app so the test never collides
    with a demo server the developer happens to be running."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "app.py", "--port", str(port), "--tenant", "meridian"],
        cwd=os.path.join(REPO_ROOT, "target_app"),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base_url = "http://127.0.0.1:{}".format(port)
    for _ in range(100):
        try:
            control(base_url, "reset")
            break
        except RuntimeError:
            time.sleep(0.1)
    else:
        proc.terminate()
        pytest.fail("target app did not start")
    try:
        yield base_url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture(scope="module")
def discovered(target_app, tmp_path_factory):
    """Run discovery once; every replay test reuses the artifact it produced."""
    from cua.escalation.channels import ScriptedOperatorChannel

    artifacts = tmp_path_factory.mktemp("artifacts")
    evidence = tmp_path_factory.mktemp("evidence")
    control(target_app, "reset")

    with run_session(run_prefix="e2e_discover", base_url=target_app,
                     evidence_root=str(evidence), secrets={"passcode": PARAMS["passcode"]},
                     echo=False, operator="none") as ctx:
        # Approve the irreversible step the way a human on the console would.
        ctx.escalation.channel = ScriptedOperatorChannel({"commands": [], "resolution": "resumed"})
        ctx.escalation.enabled = True

        loop = AgentLoop(ctx.surface, SandboxPlanner(), ctx.policy, ctx.recorder,
                         escalation=ctx.escalation, lease=ctx.lease, max_steps=25)
        result = loop.run(GOAL, PARAMS, target_app + "/login")
        assert result.status == "completed", result.reason

        capability = Recorder(
            params=PARAMS, tenant_id="meridian", vendor_product="meridiancore",
            product_version="8.2.1", base_url=target_app,
        ).record(
            result, capability_id="e2e_open_subaccount", name="Open Sub-Account",
            description="Open a sub-account for {member_id}", goal=GOAL,
            entry_path="/login", run_id=ctx.recorder.run_id, model_id="sandbox-rules:v1",
            outcome_library=load_outcome_library(
                os.path.join(REPO_ROOT, "config", "outcomes.meridiancore.json")),
        )
        evidence_dir = ctx.recorder.dir

    store = CapabilityStore(str(artifacts))
    store.save(capability)
    store.set_status(capability.id, capability.version, "approved")
    return {"store": store, "capability_id": capability.id, "base_url": target_app,
            "evidence_dir": evidence_dir, "evidence_root": str(evidence)}


def replay(discovered, params, inject=None, tenant=None):
    base_url = discovered["base_url"]
    control(base_url, "reset")
    if inject:
        control(base_url, "inject", {inject: True})
    capability = discovered["store"].load(discovered["capability_id"])
    if tenant:
        capability = resolve_for_tenant(capability, tenant)
    with run_session(run_prefix="e2e_replay", base_url=base_url,
                     evidence_root=discovered["evidence_root"],
                     secrets={"passcode": params.get("passcode", "")},
                     echo=False, operator="none") as ctx:
        executor = ReplayExecutor(ctx.surface, ctx.policy, ctx.recorder,
                                  escalation=ctx.escalation, lease=ctx.lease)
        return executor.replay(capability, params, base_url)


# -- discovery --------------------------------------------------------------
def test_discovery_produces_a_replayable_capability(discovered):
    capability = discovered["store"].load(discovered["capability_id"])
    assert [s.action.value for s in capability.steps][-1] == "extract"
    assert capability.has_irreversible_step()
    assert capability.outputs[0].name == "new_account_number"
    assert capability.final_checkpoint.kind == "text_present"


def test_the_row_anchored_locator_is_parameterized_not_pinned_to_one_record(discovered):
    """The recorded run searched for 100237. If that number ended up hardcoded
    in the locator, the capability works for exactly one member."""
    capability = discovered["store"].load(discovered["capability_id"])
    # The invariant is specifically that the parameterized row anchor is the
    # *primary* strategy for the results-grid link -- role+name alone would
    # match every row and silently open the wrong record.
    grid_steps = [s for s in capability.steps
                  if s.locator and s.locator.strategies[0]["kind"] == "row_role_name"]
    assert len(grid_steps) == 1, [s.id for s in grid_steps]
    primary = grid_steps[0].locator.strategies[0]
    assert primary["row"] == "{member_id}"
    assert primary["name"] == "Open"
    # ...and that role+name survives as the fallback beneath it.
    assert grid_steps[0].locator.strategies[1]["kind"] == "role_name"


def test_the_commit_step_is_classified_irreversible(discovered):
    capability = discovered["store"].load(discovered["capability_id"])
    irreversible = [s.id for s in capability.steps if s.risk == RiskLevel.IRREVERSIBLE]
    assert len(irreversible) == 1
    assert "submit" in irreversible[0].lower()


def test_no_credential_reaches_the_artifact_or_the_evidence(discovered):
    artifact = json.dumps(discovered["store"].load(discovered["capability_id"]).to_dict())
    assert PARAMS["passcode"] not in artifact

    events = open(os.path.join(discovered["evidence_dir"], "events.jsonl"),
                  encoding="utf-8").read()
    assert PARAMS["passcode"] not in events
    # The member's tax ID is on the record screen and never asked for; it must
    # not survive into the log either.
    assert "521-44-9087" not in events


# -- replay -----------------------------------------------------------------
def test_replays_deterministically_without_a_model(discovered):
    result = replay(discovered, PARAMS)
    assert result.status == SUCCESS
    assert result.outputs["new_account_number"].startswith("SV-100237-")
    assert result.drift == []


def test_repeated_replays_are_identical(discovered):
    outcomes = []
    for _ in range(3):
        result = replay(discovered, PARAMS)
        outcomes.append((result.status, result.outputs["new_account_number"],
                         result.steps_completed, len(result.drift)))
    assert len(set(outcomes)) == 1


def test_a_different_member_uses_the_same_capability(discovered):
    params = dict(PARAMS, member_id="100412", account_type="Checking", initial_deposit="125")
    result = replay(discovered, params)
    assert result.status == SUCCESS
    assert result.outputs["new_account_number"].startswith("CK-100412-")


@pytest.mark.parametrize("overrides,expected_outcome", [
    ({"member_id": "999999"}, "member_not_found"),
    ({"member_id": "100999"}, "member_restricted"),
    ({"operator_id": "teller01"}, "not_authorized"),
    ({"initial_deposit": "50000"}, "deposit_limit_exceeded"),
    ({"passcode": "wrong-passcode"}, "sign_in_rejected"),
])
def test_runtime_conditions_are_reported_as_business_outcomes(
        discovered, overrides, expected_outcome):
    """None of these are failures. Each is an answer the caller needs."""
    result = replay(discovered, dict(PARAMS, **overrides))
    assert result.status == BUSINESS_OUTCOME, result.summary()
    assert result.business_outcome.name == expected_outcome
    assert result.failure is None


def test_an_unexpected_interstitial_is_dismissed_and_the_run_completes(discovered):
    result = replay(discovered, PARAMS, inject="interstitial")
    assert result.status == SUCCESS
    assert [r.rule for r in result.recoveries] == ["dismiss_maintenance_notice"]


def test_a_session_timeout_is_recovered_by_re_authenticating(discovered):
    result = replay(discovered, PARAMS, inject="session_timeout")
    assert result.status == SUCCESS
    assert [r.rule for r in result.recoveries] == ["reauthenticate_on_session_expiry"]


def test_a_slow_backend_is_waited_out_rather_than_failed(discovered):
    result = replay(discovered, PARAMS, inject="slow")
    assert result.status == SUCCESS


def test_an_application_error_is_a_hard_failure_with_debuggable_detail(discovered):
    result = replay(discovered, PARAMS, inject="app_error")
    assert result.status == "hard_failure"
    assert result.failure.kind == "locator_unresolved"
    assert result.failure.step_id
    assert result.failure.observed
    # A screenshot and a control tree must exist for someone to debug from.
    files = os.listdir(result.evidence_dir)
    assert any(f.endswith(".png") for f in files)
    assert any(f.endswith(".controls.json") for f in files)


def test_a_draft_capability_will_not_commit_unattended(discovered):
    """The approval gate is a safety control, not bookkeeping."""
    store = discovered["store"]
    store.set_status(discovered["capability_id"], 1, "draft")
    try:
        result = replay(discovered, PARAMS)
        assert result.status in ("escalated", "hard_failure")
        assert result.failure.kind == "approval_required"
    finally:
        store.set_status(discovered["capability_id"], 1, "approved")


def test_the_catalog_hides_drafts_from_the_production_path(discovered):
    store = discovered["store"]
    store.set_status(discovered["capability_id"], 1, "draft")
    try:
        assert CapabilityCatalog(store, approved_only=True).tools() == []
        assert len(CapabilityCatalog(store, approved_only=False).tools()) == 1
    finally:
        store.set_status(discovered["capability_id"], 1, "approved")
