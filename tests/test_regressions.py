"""Regressions.

Each of these was a real defect found by review after the system was already
passing its other tests. They are grouped here, with the failure each one
caused, because the interesting thing about them is *why they were invisible*:
every one of them produced a plausible-looking success or a plausible-looking
error rather than an obvious crash.
"""
from __future__ import annotations

import os
import tempfile

import pytest
from fakes import FakeSurface, NullRecorder, Screen

from cua.agent.loop import DiscoveryResult, TraceStep
from cua.artifact.recorder import Recorder
from cua.artifact.schema import (
    ActionType,
    Capability,
    Condition,
    ExtractSpec,
    Locator,
    OutputField,
    ParamType,
    RiskLevel,
    Step,
    TargetBinding,
    resolve_for_tenant,
)
from cua.escalation.manager import InterventionOutcome, OperatorAction, OperatorExecutor
from cua.evidence.recorder import EvidenceRecorder
from cua.replay.executor import ReplayExecutor
from cua.replay.result import SUCCESS
from cua.safety.policy import Policy, PolicyViolation, default_policy
from cua.safety.redaction import Redactor


# ---------------------------------------------------------------------------
# An operator claiming "I finished it" was believed without checking
# ---------------------------------------------------------------------------
class _Escalation:
    def __init__(self, resolution, repair=None):
        self.resolution = resolution
        self.repair = repair

    def escalate(self, surface, **kw):
        if self.repair:
            self.repair(surface)
        return InterventionOutcome(resolution=self.resolution,
                                   actions=[OperatorAction(verb="click", args={})])


def _draft_commit_capability() -> Capability:
    """Irreversible step in a *draft* capability, followed by an extraction."""
    return Capability(
        id="cap", name="Cap", version=1, description="d",
        target=TargetBinding(vendor_product="t", entry_path="/review"),
        inputs=[],
        outputs=[OutputField("new_account_number", ParamType.STRING)],
        steps=[
            Step("s1", ActionType.CLICK,
                 Locator([{"kind": "role_name", "role": "button", "name": "Submit"}],
                         description="button 'Submit'"),
                 risk=RiskLevel.IRREVERSIBLE),
            Step("s2", ActionType.EXTRACT,
                 extract=ExtractSpec("new_account_number", "labelled_value",
                                     label="New Account Number")),
        ],
        final_checkpoint=Condition("text_present", "Sub-Account Opened"),
        status="draft",
    )


def test_operator_who_did_nothing_does_not_produce_a_success_with_no_outputs():
    """The approval gate stops a draft's commit and asks a human. If that human
    answers 'done' without having done anything, the old code returned
    status=success with the declared output silently absent -- a confidently
    wrong answer, which is worse than an honest failure."""
    surface = FakeSurface({"review": Screen("/review", "Review", ["Submit"])}, start="review")
    executor = ReplayExecutor(surface, Policy(), NullRecorder(),
                              escalation=_Escalation("completed_by_operator"))
    result = executor.replay(_draft_commit_capability(), {}, "http://app.test")

    assert result.status != SUCCESS
    assert result.outputs == {}
    assert surface.actions == []          # nothing was ever committed


def test_operator_who_really_finished_is_accepted_with_outputs_re_read():
    surface = FakeSurface(
        {"review": Screen("/review", "Review", ["Submit"]),
         "done": Screen("/done", "Sub-Account Opened", [],
                        labelled={"New Account Number": "SV-100237-02"})},
        start="review")

    def repair(s):
        s.current = "done"

    executor = ReplayExecutor(surface, Policy(), NullRecorder(),
                              escalation=_Escalation("completed_by_operator", repair))
    result = executor.replay(_draft_commit_capability(), {}, "http://app.test")

    assert result.status == SUCCESS
    assert result.outputs == {"new_account_number": "SV-100237-02"}


# ---------------------------------------------------------------------------
# The deny list was bypassable with an un-normalized path
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", [
    "/members/../_control/reset",
    "/servicing/../_control/inject",
    "/members/%2e%2e/_control/reset",
    "/members/./../_control/reset",
])
def test_traversal_cannot_reach_a_denied_path(path):
    """`fnmatch`'s `*` crosses `/`, so these missed the deny glob and matched
    an allow glob -- while the browser resolved them to the control plane and
    executed them."""
    policy = default_policy("http://127.0.0.1:5075")
    with pytest.raises(PolicyViolation):
        policy.check_location("http://127.0.0.1:5075" + path)


def test_normalization_does_not_break_legitimate_paths():
    policy = default_policy("http://127.0.0.1:5075")
    for path in ["/members/100237", "/servicing/members/100237", "/", "/login"]:
        policy.check_location("http://127.0.0.1:5075" + path)


# ---------------------------------------------------------------------------
# The recorder leaked the recorded record's identity into a fallback locator
# ---------------------------------------------------------------------------
def _record(trace, params):
    result = DiscoveryResult(status="completed", trace=trace, steps_taken=len(trace))
    return Recorder(params=params, tenant_id="t", vendor_product="v",
                    base_url="http://h").record(
        result, capability_id="c", name="C", description="d", goal="g",
        entry_path="/login", run_id="r", model_id="m")


def test_no_locator_strategy_hardcodes_the_recorded_record():
    """A key cell rendered as "Member 100237" parameterizes to
    "Member {member_id}", which does not *start* with a placeholder -- the old
    predicate therefore also emitted a raw variant pinned to member 100237.
    On replay for a different member, that fallback would resolve against the
    wrong row and click its link, reported as drift rather than failure."""
    cap = _record([TraceStep(
        index=0, action="click", role="link", name="Open",
        hints={"frame": "", "name_source": "text", "tag": "a",
               "row_anchor": "Member 100237 Active",
               "css": 'a[href="/members/100237"]'},
        risk=RiskLevel.REVERSIBLE,
        location_before="http://h/s", location_after="http://h/members/100237",
    )], {"member_id": "100237"})

    serialized = str(cap.steps[0].locator.to_dict())
    assert "100237" not in serialized, serialized
    assert cap.steps[0].locator.strategies[0]["row"] == "Member {member_id} Active"


def test_a_truly_unparameterizable_row_anchor_is_still_kept_as_a_fallback():
    cap = _record([TraceStep(
        index=0, action="click", role="link", name="Open",
        hints={"frame": "", "name_source": "text", "tag": "a",
               "row_anchor": "Primary Savings", "css": "a.x"},
        risk=RiskLevel.REVERSIBLE,
    )], {"member_id": "100237"})
    kinds = [s["kind"] for s in cap.steps[0].locator.strategies]
    assert "row_role_name" in kinds


# ---------------------------------------------------------------------------
# Params used only via a literal were dropped from the input contract
# ---------------------------------------------------------------------------
def test_a_param_used_only_as_a_literal_is_still_declared():
    """The planner is told never to inline a value, but `_parameterize` exists
    because models sometimes do. When that happened the param was recorded in
    the step but not in `inputs`, so `validate_params` either rejected the
    caller's argument as "unknown input" or -- worse -- accepted a call with it
    omitted and typed the literal "{member_id}" into the field, which the app
    answers with "no such member". A confident, plausible, wrong answer."""
    cap = _record([TraceStep(
        index=0, action="fill", role="textbox", name="Member Number",
        hints={"frame": "", "name_source": "aria-label", "tag": "input"},
        param=None, literal="100237", risk=RiskLevel.REVERSIBLE,
    )], {"member_id": "100237"})

    assert cap.steps[0].literal == "{member_id}"
    assert [i.name for i in cap.inputs] == ["member_id"]
    assert cap.validate_params({"member_id": "100240"}) == []
    assert cap.validate_params({}) != []          # and an omitted arg is refused


# ---------------------------------------------------------------------------
# Tenant relabelling chained substitutions and depended on JSON key order
# ---------------------------------------------------------------------------
def _relabel_capability(labels, base_path="", entry="/login"):
    return Capability(
        id="c", name="C", version=1, description="d",
        target=TargetBinding(vendor_product="v", entry_path=entry),
        steps=[Step("s1", ActionType.CLICK,
                    Locator([{"kind": "role_name", "role": "button",
                              "name": "Member Number Account Type"}]),
                    checkpoint=Condition("text_present", "{member_id}"))],
        overrides={"t": {"labels": labels, "base_path": base_path}},
    )


@pytest.mark.parametrize("labels", [
    {"Member Number": "Account Holder ID", "Account": "Client"},
    {"Account": "Client", "Member Number": "Account Holder ID"},   # reversed
])
def test_relabelling_does_not_depend_on_key_order(labels):
    """One rule's *output* used to be re-matched by a later rule, turning
    "Account Holder ID" into "Client Holder ID". Correctness depended on the
    ordering of keys in a JSON object."""
    eff = resolve_for_tenant(_relabel_capability(labels), "t")
    assert eff.steps[0].locator.strategies[0]["name"] == "Account Holder ID Client Type"


def test_relabelling_never_corrupts_a_parameter_placeholder():
    """A label key that is a substring of a param name would rewrite the
    placeholder into one `_fill` can never substitute -- and the checkpoint
    would then fail permanently and inexplicably."""
    eff = resolve_for_tenant(_relabel_capability({"member": "client"}), "t")
    assert eff.steps[0].checkpoint.value == "{member_id}"


def test_relabelling_is_idempotent():
    """A resolved capability must survive being resolved again -- otherwise
    "Account Type" becomes "Sub Sub Account Type"."""
    cap = _relabel_capability({"Account Type": "Sub Account Type"})
    once = resolve_for_tenant(cap, "t")
    once.overrides = cap.overrides
    twice = resolve_for_tenant(once, "t")
    assert (once.steps[0].locator.strategies[0]["name"]
            == twice.steps[0].locator.strategies[0]["name"])


def test_base_path_requires_a_segment_boundary():
    """`startswith` treated base_path "/cu" as already applied to
    "/customers/login", so the tenant was never prefixed and every run failed
    at step zero on a URL that does not exist on their install."""
    cap = _relabel_capability({}, base_path="/cu", entry="/customers/login")
    assert resolve_for_tenant(cap, "t").target.entry_path == "/cu/customers/login"


def test_base_path_is_not_applied_twice():
    cap = _relabel_capability({}, base_path="/cu", entry="/cu/login")
    assert resolve_for_tenant(cap, "t").target.entry_path == "/cu/login"


# ---------------------------------------------------------------------------
# A credential typed by an operator reached the evidence log in cleartext
# ---------------------------------------------------------------------------
def _operator_log(controls, name, value):
    root = tempfile.mkdtemp()
    recorder = EvidenceRecorder(root, "run", redactor=Redactor(), echo=False)
    surface = FakeSurface({"s": Screen("/login", "Sign in", controls)}, start="s")

    original = surface.observe

    def as_textboxes(i=0):
        obs = original(i)
        for c in obs.controls:
            c.role = "textbox"
        return obs

    surface.observe = as_textboxes
    OperatorExecutor(surface, recorder=recorder).execute("fill", name=name, value=value)
    with open(os.path.join(root, "run", "events.jsonl"), encoding="utf-8") as fh:
        return fh.read()


def test_a_passcode_typed_by_an_operator_never_reaches_the_log():
    """CLI-supplied secrets are pre-registered with the redactor; an
    operator-typed one never was, and a passcode matches no pattern."""
    assert "hunter2-typed" not in _operator_log(
        ["Operator ID", "Passcode"], "Passcode", "hunter2-typed")


def test_a_passcode_is_masked_even_when_the_fill_fails():
    """`execute` logs its args whether the action succeeded or not, so the
    failure path leaked too."""
    assert "hunter2-failed" not in _operator_log(
        ["Operator ID"], "Passcode", "hunter2-failed")


def test_non_sensitive_operator_values_are_still_readable():
    assert "100237" in _operator_log(["Member Number"], "Member Number", "100237")


# ---------------------------------------------------------------------------
# A malformed locator strategy escaped reclassification
# ---------------------------------------------------------------------------
def test_a_malformed_strategy_is_a_locator_failure_not_an_executor_crash():
    """A hand-edited artifact or a tenant `steps` patch can omit `name`. As a
    bare KeyError it bypassed the business-outcome and recovery
    reclassification entirely."""
    from cua.surfaces.base import SurfaceError
    from cua.surfaces.web import WebSurface

    locator = Locator([{"kind": "role_name", "role": "button"}],  # no "name"
                      description="button '?'")

    class _Frame:
        def __init__(self):
            self.name = ""
            self.url = "http://x/"

        def get_by_role(self, *_a, **_k):        # never reached: the strategy
            raise AssertionError("unreachable")  # dict is missing "name"

    class _Page:
        def __init__(self):
            self.main_frame = _Frame()
            self.frames = [self.main_frame]
            self.url = "http://x/"

        def on(self, *_a, **_k):
            return None

    with pytest.raises(SurfaceError) as exc:
        WebSurface(_Page()).resolve(locator, {})
    assert "malformed strategy" in str(exc.value)


# ---------------------------------------------------------------------------
# A re-read of an already-captured field was declared twice in the contract
# ---------------------------------------------------------------------------
def test_an_output_read_twice_is_declared_once():
    """Models re-read values they already have; it is cheap and makes them more
    certain. Recording it verbatim declared the same field twice in the return
    contract, which any caller generating a type from it would reject."""
    cap = _record([
        TraceStep(index=0, action="extract", output_name="new_account_number",
                  extract_label="New Account Number"),
        TraceStep(index=1, action="extract", output_name="new_account_number",
                  extract_label="New Account Number"),
    ], {})
    assert [o.name for o in cap.outputs] == ["new_account_number"]
