"""Replay: the result contract and the error taxonomy.

The central question these cover is the one the brief calls out as the most
commonly botched: is this a legitimate business answer, something the executor
should quietly recover from, or genuine breakage?
"""
from __future__ import annotations

from fakes import FakeSurface, NullRecorder, Screen

from cua.artifact.schema import (
    ActionType,
    BusinessOutcome,
    Capability,
    Condition,
    ExtractSpec,
    InputParam,
    Locator,
    OutputField,
    ParamType,
    RecoveryRule,
    RiskLevel,
    Step,
    TargetBinding,
)
from cua.replay.executor import ReplayExecutor
from cua.replay.result import BUSINESS_OUTCOME, HARD_FAILURE, POLICY_BLOCKED, SUCCESS
from cua.safety.policy import Policy

BASE = "http://app.test"


def locator(name: str, *fallbacks: str) -> Locator:
    strategies = [{"kind": "role_name", "role": "button", "name": name, "frame": ""}]
    for f in fallbacks:
        strategies.append({"kind": "text", "value": f, "frame": ""})
    return Locator(strategies, description="button {!r}".format(name))


def capability(steps=None, outcomes=None, recoveries=None, status="approved",
               final=None, inputs=None, outputs=None) -> Capability:
    return Capability(
        id="cap", name="Cap", version=1, description="d",
        target=TargetBinding(vendor_product="test", entry_path="/start"),
        inputs=inputs if inputs is not None else [
            InputParam("member_id", ParamType.STRING, True, "member")],
        outputs=outputs or [],
        steps=steps or [],
        business_outcomes=outcomes or [],
        recovery_rules=recoveries or [],
        final_checkpoint=final,
        status=status,
    )


_DEFAULT_PARAMS = {"member_id": "100237"}


def run(cap, surface, params=_DEFAULT_PARAMS, policy=None, escalation=None):
    recorder = NullRecorder()
    executor = ReplayExecutor(surface, policy or Policy(), recorder, escalation=escalation)
    result = executor.replay(cap, params, BASE)
    return result, recorder


# -- success ----------------------------------------------------------------
def test_completes_the_happy_path_and_returns_declared_outputs():
    surface = FakeSurface(
        {"start": Screen("/start", "Start", ["Go"]),
         "done": Screen("/done", "Done", [], labelled={"Account": "SV-1"})},
        start="start", transitions={("start", "Go"): "done"})
    cap = capability(
        steps=[
            Step("s1", ActionType.CLICK, locator("Go"),
                 checkpoint=Condition("url_contains", "/done")),
            Step("s2", ActionType.EXTRACT,
                 extract=ExtractSpec("account", "labelled_value", label="Account")),
        ],
        outputs=[OutputField("account", ParamType.STRING)],
        final=Condition("text_present", "Done"))
    result, _ = run(cap, surface)
    assert result.status == SUCCESS
    assert result.ok is True
    assert result.outputs == {"account": "SV-1"}
    assert result.steps_completed == 2


def test_no_model_is_reachable_from_the_replay_path():
    """The production path must not be able to call an LLM even by accident."""
    import cua.replay.executor as mod
    source = open(mod.__file__, encoding="utf-8").read()
    assert "planner" not in source.lower().replace("# ", "")


# -- business outcomes ------------------------------------------------------
def test_reports_a_declared_business_outcome_as_data_not_as_a_failure():
    surface = FakeSurface(
        {"start": Screen("/start", "Start", ["Search"]),
         "empty": Screen("/start", "No member matching that number was found", [])},
        start="start", transitions={("start", "Search"): "empty"})
    cap = capability(
        steps=[Step("s1", ActionType.CLICK, locator("Search")),
               Step("s2", ActionType.CLICK, locator("Open"))],
        outcomes=[BusinessOutcome("member_not_found",
                                  Condition("text_present", "No member matching"),
                                  "no such member")])
    result, _ = run(cap, surface)
    assert result.status == BUSINESS_OUTCOME
    assert result.ok is False
    assert result.business_outcome.name == "member_not_found"
    assert result.failure is None


def test_a_business_outcome_is_detected_before_the_next_step_mistakes_it_for_breakage():
    """The banner is produced by step one; step two's control is simply absent.
    Without checking outcomes first this surfaces as a locator failure -- the
    exact misclassification that turns 'no such member' into a page-out."""
    surface = FakeSurface(
        {"start": Screen("/start", "Start", ["Search"]),
         "empty": Screen("/start", "No member matching that number was found", [])},
        start="start", transitions={("start", "Search"): "empty"})
    cap = capability(
        steps=[Step("s1", ActionType.CLICK, locator("Search")),
               Step("s2", ActionType.CLICK, locator("Open"))],
        outcomes=[BusinessOutcome("member_not_found",
                                  Condition("text_present", "No member matching"))])
    result, recorder = run(cap, surface)
    assert result.status == BUSINESS_OUTCOME
    assert "hard_failure" not in recorder.kinds()


def test_a_failed_checkpoint_is_reclassified_when_it_is_really_an_outcome():
    surface = FakeSurface(
        {"start": Screen("/start", "Start", ["Submit"]),
         "denied": Screen("/start", "You are not authorized", [])},
        start="start", transitions={("start", "Submit"): "denied"})
    cap = capability(
        steps=[Step("s1", ActionType.CLICK, locator("Submit"),
                    checkpoint=Condition("url_contains", "/confirm"))],
        outcomes=[BusinessOutcome("not_authorized",
                                  Condition("text_present", "not authorized"))])
    result, _ = run(cap, surface)
    assert result.status == BUSINESS_OUTCOME
    assert result.business_outcome.name == "not_authorized"


# -- hard failures ----------------------------------------------------------
def test_an_undeclared_dead_end_is_a_hard_failure_with_debuggable_detail():
    surface = FakeSurface({"start": Screen("/start", "Something went wrong", [])},
                          start="start")
    cap = capability(steps=[Step("s1", ActionType.CLICK, locator("Go"))])
    result, _ = run(cap, surface)
    assert result.status == HARD_FAILURE
    assert result.failure.kind == "locator_unresolved"
    assert result.failure.step_id == "s1"
    assert result.failure.observed == "/start"


def test_a_failed_checkpoint_with_no_matching_outcome_is_a_hard_failure():
    surface = FakeSurface(
        {"start": Screen("/start", "Start", ["Go"]),
         "wrong": Screen("/elsewhere", "Elsewhere", [])},
        start="start", transitions={("start", "Go"): "wrong"})
    cap = capability(steps=[Step("s1", ActionType.CLICK, locator("Go"),
                                 checkpoint=Condition("url_contains", "/done"))])
    result, _ = run(cap, surface)
    assert result.status == HARD_FAILURE
    assert result.failure.kind == "checkpoint_failed"
    assert "/done" in result.failure.expected
    assert "/elsewhere" in result.failure.observed


def test_captures_richer_evidence_on_a_hard_failure():
    surface = FakeSurface({"start": Screen("/start", "", [])}, start="start")
    cap = capability(steps=[Step("s1", ActionType.CLICK, locator("Go"))])
    _result, recorder = run(cap, surface)
    assert "evidence_captured" in recorder.kinds()


def test_a_missing_required_input_fails_at_the_boundary_before_acting():
    surface = FakeSurface({"start": Screen("/start", "", ["Go"])}, start="start")
    cap = capability(steps=[Step("s1", ActionType.CLICK, locator("Go"))])
    result, _ = run(cap, surface, params={})
    assert result.status == HARD_FAILURE
    assert result.failure.kind == "contract_violation"
    assert surface.actions == []  # never touched the app


# -- drift ------------------------------------------------------------------
def test_falling_back_to_a_secondary_locator_still_succeeds_but_reports_drift():
    surface = FakeSurface({"start": Screen("/start", "Start", ["Proceed"])}, start="start")
    surface.unresolvable.add("Go")
    cap = capability(steps=[Step("s1", ActionType.CLICK, locator("Go", "Proceed"))])
    result, _ = run(cap, surface)
    assert result.status == SUCCESS
    assert len(result.drift) == 1
    assert result.drift[0].step_id == "s1"
    assert result.drift[0].strategy_index == 1


# -- recoverable conditions -------------------------------------------------
def test_retries_a_transient_timeout_rather_than_failing():
    surface = FakeSurface(
        {"start": Screen("/start", "Start", ["Go"]), "done": Screen("/done", "Done", [])},
        start="start", transitions={("start", "Go"): "done"})
    surface.timeout_on.add("Go")
    cap = capability(steps=[Step("s1", ActionType.CLICK, locator("Go"),
                                 checkpoint=Condition("url_contains", "/done"))])
    result, _ = run(cap, surface)
    assert result.status == SUCCESS


def test_dismisses_a_known_interstitial_and_carries_on():
    surface = FakeSurface(
        {"blocked": Screen("/start", "Scheduled Maintenance Notice", ["Acknowledge"]),
         "start": Screen("/start", "Start", ["Go"]),
         "done": Screen("/done", "Done", [])},
        start="blocked",
        transitions={("blocked", "Acknowledge"): "start", ("start", "Go"): "done"})
    cap = capability(
        steps=[Step("s1", ActionType.CLICK, locator("Go"),
                    checkpoint=Condition("url_contains", "/done"))],
        recoveries=[RecoveryRule(
            "dismiss_notice", Condition("text_present", "Scheduled Maintenance Notice"),
            {"kind": "click", "locator": locator("Acknowledge").to_dict()})])
    result, _ = run(cap, surface)
    assert result.status == SUCCESS
    assert [r.rule for r in result.recoveries] == ["dismiss_notice"]


def test_reauthenticates_by_re_running_the_safe_prefix():
    """The session dies mid-flow. The correct response is to sign in again and
    carry on, not to page a human about a checkpoint failure."""
    surface = FakeSurface(
        {"login": Screen("/login", "Sign in", ["Sign In"]),
         "work": Screen("/work", "Work", ["Act"]),
         "expired": Screen("/login", "Your session has expired", ["Sign In"]),
         "done": Screen("/done", "Done", [])},
        start="login",
        transitions={("login", "Sign In"): "work", ("expired", "Sign In"): "work",
                     ("work", "Act"): "done"},
        # The expiry happens exactly once, as a real one would.
        transitions_once={("work", "Act"): "expired"})
    cap = capability(
        steps=[Step("s1", ActionType.CLICK, locator("Sign In"), risk=RiskLevel.SAFE),
               Step("s2", ActionType.CLICK, locator("Act"),
                    checkpoint=Condition("url_contains", "/done"))],
        recoveries=[RecoveryRule(
            "reauth", Condition("text_present", "Your session has expired"),
            {"kind": "restart_from_step", "step_id": "@first"})])
    result, _ = run(cap, surface)
    assert [r.rule for r in result.recoveries] == ["reauth"]
    assert surface.actions.count("Sign In") == 2


def test_refuses_to_re_run_a_prefix_that_would_repeat_an_irreversible_step():
    """A recovery must never be able to cause a worse failure than the one it
    is recovering from. Re-running a prefix containing a commit would post the
    transaction twice."""
    surface = FakeSurface(
        {"start": Screen("/start", "Start", ["Submit"]),
         "expired": Screen("/login", "Your session has expired", ["Sign In"])},
        start="start", transitions={("start", "Submit"): "expired"})
    cap = capability(
        steps=[Step("s1", ActionType.CLICK, locator("Submit"), risk=RiskLevel.IRREVERSIBLE),
               Step("s2", ActionType.CLICK, locator("Next"),
                    checkpoint=Condition("url_contains", "/done"))],
        recoveries=[RecoveryRule(
            "reauth", Condition("text_present", "Your session has expired"),
            {"kind": "restart_from_step", "step_id": "@first"})])
    result, recorder = run(cap, surface)
    assert result.status == HARD_FAILURE
    assert result.failure.kind == "recovery_refused"
    assert surface.actions.count("Submit") == 1
    assert "recovery_refused" in recorder.kinds()


def test_a_recovery_rule_is_bounded_by_its_attempt_cap():
    surface = FakeSurface(
        {"stuck": Screen("/start", "Scheduled Maintenance Notice", ["Acknowledge", "Go"])},
        start="stuck")  # Acknowledge never clears it
    cap = capability(
        steps=[Step("s1", ActionType.CLICK, locator("Go"),
                    checkpoint=Condition("url_contains", "/done"))],
        recoveries=[RecoveryRule(
            "dismiss", Condition("text_present", "Scheduled Maintenance Notice"),
            {"kind": "click", "locator": locator("Acknowledge").to_dict()},
            max_attempts=1)])
    result, _ = run(cap, surface)
    assert result.status == HARD_FAILURE
    assert surface.actions.count("Acknowledge") == 1


# -- policy -----------------------------------------------------------------
def test_refuses_an_irreversible_step_in_an_unapproved_capability():
    surface = FakeSurface({"start": Screen("/start", "", ["Submit"])}, start="start")
    cap = capability(
        steps=[Step("s1", ActionType.CLICK, locator("Submit"), risk=RiskLevel.IRREVERSIBLE)],
        status="draft")
    result, _ = run(cap, surface)
    assert result.failure.kind == "approval_required"
    assert surface.actions == []


def test_runs_an_irreversible_step_once_the_capability_is_approved():
    surface = FakeSurface({"start": Screen("/start", "", ["Submit"])}, start="start")
    cap = capability(
        steps=[Step("s1", ActionType.CLICK, locator("Submit"), risk=RiskLevel.IRREVERSIBLE)],
        status="approved")
    result, _ = run(cap, surface)
    assert result.status == SUCCESS
    assert surface.actions == ["Submit"]


def test_blocks_an_action_type_the_policy_forbids():
    surface = FakeSurface({"start": Screen("/start", "", ["Field"])}, start="start")
    cap = capability(steps=[Step("s1", ActionType.FILL, locator("Field"), param="member_id")])
    result, _ = run(cap, surface,
                    policy=Policy(allowed_actions=["click", "navigate", "extract"]))
    assert result.status == POLICY_BLOCKED
    assert surface.actions == []


def test_blocks_navigation_off_the_allowlist():
    surface = FakeSurface({"start": Screen("/start", "", [])}, start="start")
    cap = capability(steps=[])
    result, _ = run(cap, surface,
                    policy=Policy(allowed_origins=["http://only-this.test"]))
    assert result.status == POLICY_BLOCKED


# -- escalation -------------------------------------------------------------
class StubEscalation:
    def __init__(self, resolution, repair=None):
        self.resolution = resolution
        self.repair = repair
        self.requests = []

    def escalate(self, surface, **kw):
        from cua.escalation.manager import InterventionOutcome, OperatorAction
        self.requests.append(kw)
        if self.repair:
            self.repair(surface)
        return InterventionOutcome(resolution=self.resolution,
                                   actions=[OperatorAction(verb="click", args={})])


def breaking_surface() -> FakeSurface:
    """The app errors partway through: step one works, then the screen it
    lands on has nothing step two can act on.

    The fault has to occur *mid-flow* rather than at the entry point, because
    replay always navigates to the entry point first -- a run that is broken
    before it starts is not the interesting case.
    """
    return FakeSurface(
        {"start": Screen("/start", "Start", ["Go"]),
         "broken": Screen("/error", "MRDN-5001 Application Error", []),
         "recovered": Screen("/recovered", "Recovered", ["Next"]),
         "done": Screen("/done", "Done", [], labelled={"Account": "SV-9"})},
        start="start",
        transitions={("start", "Go"): "broken", ("recovered", "Next"): "done"})


def two_step_capability(**kw) -> Capability:
    return capability(
        steps=[Step("s1", ActionType.CLICK, locator("Go")),
               Step("s2", ActionType.CLICK, locator("Next"),
                    checkpoint=Condition("url_contains", "/done"))],
        **kw)


def test_resumes_the_same_run_after_an_operator_repairs_the_session():
    """The point of the handoff is that the run *continues*, not that a human
    was notified. After hand-back the failed step is retried on the same
    session and the capability finishes."""
    surface = breaking_surface()

    def repair(s):
        s.current = "recovered"

    result, recorder = run(two_step_capability(), surface,
                           escalation=StubEscalation("resumed", repair))
    assert result.status == SUCCESS
    assert result.escalation["resolution"] == "resumed"
    assert result.failure is None
    assert "resuming_after_handoff" in recorder.kinds()
    assert surface.actions == ["Go", "Next"]


def test_an_operator_who_aborts_leaves_the_run_as_a_hard_failure():
    result, _ = run(two_step_capability(), breaking_surface(),
                    escalation=StubEscalation("aborted"))
    assert result.status == HARD_FAILURE
    assert result.failure.kind == "locator_unresolved"


def test_a_human_is_asked_about_a_given_step_only_once():
    """An operator who hands back a session that is still broken should not be
    asked about the same step forever."""
    escalation = StubEscalation("resumed")  # no repair: still broken on retry
    result, _ = run(two_step_capability(), breaking_surface(), escalation=escalation)
    assert result.status != SUCCESS
    assert len(escalation.requests) == 2  # the original, then one retry


def test_an_operator_claiming_completion_is_still_checked_against_the_contract():
    """The success condition is the contract regardless of who executed the
    steps -- a human saying 'done' does not make it so."""
    result, recorder = run(two_step_capability(final=Condition("text_present", "Done")),
                           breaking_surface(),
                           escalation=StubEscalation("completed_by_operator"))
    assert result.status != SUCCESS
    assert "operator_claimed_done_but_checkpoint_failed" in recorder.kinds()


def test_an_operator_who_genuinely_finished_yields_success_with_outputs():
    surface = breaking_surface()

    def repair(s):
        s.current = "done"

    cap = capability(
        steps=[Step("s1", ActionType.CLICK, locator("Go")),
               Step("s2", ActionType.CLICK, locator("Next")),
               Step("s3", ActionType.EXTRACT,
                    extract=ExtractSpec("account", "labelled_value", label="Account"))],
        outputs=[OutputField("account", ParamType.STRING)],
        final=Condition("text_present", "Done"))
    result, _ = run(cap, surface,
                    escalation=StubEscalation("completed_by_operator", repair))
    assert result.status == SUCCESS
    assert result.outputs == {"account": "SV-9"}


def test_the_intervention_request_carries_enough_context_to_act_on():
    escalation = StubEscalation("aborted")
    run(two_step_capability(), breaking_surface(), escalation=escalation)
    request = escalation.requests[0]
    assert request["capability_id"] == "cap"
    assert request["step_id"] == "s2"
    assert request["mode"] == "replay"
    assert request["trigger"] == "hard_failure"
    assert request["observed"] == "/error"
    assert request["expected"] == "button 'Next'"


# -- determinism ------------------------------------------------------------
def test_the_same_inputs_produce_the_same_actions_every_time():
    def fresh():
        return FakeSurface(
            {"start": Screen("/start", "Start", ["A", "B"]),
             "mid": Screen("/mid", "Mid", ["C"]),
             "done": Screen("/done", "Done", [], labelled={"Account": "SV-1"})},
            start="start",
            transitions={("start", "A"): "mid", ("mid", "C"): "done"})

    cap = capability(
        steps=[Step("s1", ActionType.CLICK, locator("A")),
               Step("s2", ActionType.CLICK, locator("C")),
               Step("s3", ActionType.EXTRACT,
                    extract=ExtractSpec("account", "labelled_value", label="Account"))],
        outputs=[OutputField("account", ParamType.STRING)],
        final=Condition("text_present", "Done"))

    runs = []
    for _ in range(5):
        surface = fresh()
        result, _ = run(cap, surface)
        runs.append((result.status, tuple(surface.actions), tuple(sorted(result.outputs.items()))))
    assert len(set(runs)) == 1
