"""
The discovery loop: observe -> decide -> act, until the goal is met or a
stopping condition fires.

This is the only phase with a model in it. Its job is not just to accomplish
the goal once -- it is to accomplish it *while producing a trace rich enough
to record a deterministic capability from*. That shapes two things that a
plain agent loop wouldn't have:

  * Every executed step captures the **addressing hints** of the control it
    acted on (frame, form-field name, containing-row text, CSS path) alongside
    the role/name the model chose. The model never sees these; the recorder
    needs all of them to synthesize a ranked locator.
  * Every executed step captures the **location and visible text before and
    after**, which is what lets the recorder infer a checkpoint -- a statement
    of what should be true if this step worked.

Stopping conditions, all of which route to escalation rather than dying:
  - the planner says `stuck`
  - no progress: the location and control set are unchanged for two
    consecutive steps (the model is looping)
  - the step budget is exhausted
  - policy demands confirmation for an irreversible action
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..artifact.schema import RiskLevel
from ..escalation.manager import EscalationManager
from ..safety.policy import ConfirmationRequired, Policy, PolicyViolation
from ..surfaces.base import Control, Observation, Surface, SurfaceError
from .planner import Decision, Planner, PlannerError, RateLimited

_MAX_PLANNER_RETRIES = 2
# Rate-limit waits are counted separately and allowed to be more
# generous: a free provider tier throttles a normal run several times.
_MAX_RATE_LIMIT_WAITS = 8


@dataclass
class TraceStep:
    """One executed action plus everything the recorder will need."""

    index: int
    action: str
    role: str = ""
    name: str = ""
    hints: Dict[str, str] = field(default_factory=dict)
    param: Optional[str] = None
    literal: Optional[str] = None
    risk: str = RiskLevel.SAFE.value
    reasoning: str = ""
    location_before: str = ""
    location_after: str = ""
    title_before: str = ""
    title_after: str = ""
    text_before: str = ""
    text_after: str = ""
    # extract steps only
    output_name: str = ""
    extract_label: str = ""
    by_operator: bool = False


@dataclass
class DiscoveryResult:
    status: str                       # completed | stuck | aborted | policy_blocked | max_steps
    trace: List[TraceStep] = field(default_factory=list)
    outputs: Dict[str, str] = field(default_factory=dict)
    final_location: str = ""
    final_title: str = ""
    final_text: str = ""
    reason: str = ""
    human_intervened: bool = False
    steps_taken: int = 0


class AgentLoop:
    def __init__(self, surface: Surface, planner: Planner, policy: Policy,
                 recorder, escalation: Optional[EscalationManager] = None,
                 lease=None, max_steps: int = 25, max_escalations: int = 3):
        self.surface = surface
        self.planner = planner
        self.policy = policy
        self.recorder = recorder
        self.escalation = escalation
        self.lease = lease
        self.max_steps = min(max_steps, policy.max_steps)
        self.max_escalations = max_escalations
        self._escalations = 0

    def run(self, goal: str, params: Dict[str, str], entry_url: str) -> DiscoveryResult:
        trace: List[TraceStep] = []
        outputs: Dict[str, str] = {}
        history: List[str] = []
        human_intervened = False
        repeats = 0
        last_signature = None
        consecutive_failures = 0

        self._check_locations()
        self.policy.check_location(entry_url)
        self.surface.navigate(entry_url)
        self.surface.wait_settled()
        self.recorder.log("discovery_started", goal=goal, entry_url=entry_url,
                          planner=self.planner.model_id, max_steps=self.max_steps)

        for index in range(self.max_steps):
            self._check_locations()
            if self.lease:
                self.lease.assert_automation()

            obs = self.surface.observe(index)
            decision = self._decide(obs, goal, params, history, outputs)
            self.recorder.log(
                "planner_decision", step=index, location=obs.location,
                action=decision.action, ref=decision.ref, value=decision.value,
                output_name=decision.output_name, label=decision.label,
                reasoning=decision.reasoning, controls_seen=len(obs.controls),
            )

            # "Stuck" is not "the screen didn't change" -- typing into a form
            # legitimately changes nothing about the control set. The signal
            # that actually means the agent is looping is the planner proposing
            # the *same action on the same screen* over and over.
            signature = (obs.location, decision.action, decision.ref, decision.value)
            repeats = repeats + 1 if signature == last_signature else 0
            last_signature = signature

            if repeats >= 2 or consecutive_failures >= 3:
                why = ("The planner proposed the same action on the same screen three times "
                       "in a row." if repeats >= 2 else
                       "Three consecutive actions failed to execute.")
                outcome = self._escalate(reason=why, trigger="no_progress", goal=goal, obs=obs)
                if outcome is None or not outcome.should_continue:
                    return self._finish("stuck", trace, outputs, obs,
                                        reason="{} Escalation did not resume the run.".format(why),
                                        human_intervened=human_intervened or outcome is not None,
                                        steps=index)
                human_intervened = True
                trace.extend(self._operator_trace(outcome, index))
                repeats = 0
                consecutive_failures = 0
                continue

            if decision.action == "done":
                return self._finish("completed", trace, outputs, obs,
                                    reason=decision.reasoning,
                                    human_intervened=human_intervened, steps=index)

            if decision.action == "stuck":
                outcome = self._escalate(
                    reason=decision.reasoning or "The planner could not find a way forward.",
                    trigger="planner_stuck", goal=goal, obs=obs)
                if outcome is None or outcome.resolution == "aborted":
                    return self._finish("stuck", trace, outputs, obs,
                                        reason=decision.reasoning,
                                        human_intervened=outcome is not None, steps=index)
                human_intervened = True
                trace.extend(self._operator_trace(outcome, index))
                if outcome.operator_finished_the_work:
                    obs = self.surface.observe(index)
                    return self._finish("completed", trace, outputs, obs,
                                        reason="completed by operator after escalation",
                                        human_intervened=True, steps=index)
                continue

            if decision.action == "extract":
                # A model will sometimes re-read a field it already has. Don't
                # record the repeat: a second identical extract step adds a
                # redundant action to every future replay and would otherwise
                # need de-duplicating in the contract anyway.
                if (decision.output_name in outputs
                        and outputs[decision.output_name]):
                    history.append("extract:{} (already captured)".format(decision.output_name))
                    self.recorder.log("extract_skipped_duplicate", step=index,
                                      output_name=decision.output_name)
                    continue
                value = self._extract(decision, obs)
                if value is None:
                    history.append("extract:{} FAILED (no value next to label {!r})".format(
                        decision.output_name, decision.label))
                    self.recorder.log("extract_failed", step=index,
                                      output_name=decision.output_name, label=decision.label)
                    continue
                outputs[decision.output_name] = value
                trace.append(TraceStep(
                    index=index, action="extract", output_name=decision.output_name or "",
                    extract_label=decision.label or "", reasoning=decision.reasoning,
                    location_before=obs.location, location_after=obs.location,
                    title_before=obs.title, title_after=obs.title,
                    text_before=obs.text, text_after=obs.text,
                    risk=RiskLevel.SAFE.value,
                ))
                history.append("extract:{}".format(decision.output_name))
                self.recorder.log("extracted", step=index, output_name=decision.output_name,
                                  label=decision.label, value=value)
                continue

            control = obs.control(decision.ref) if decision.ref is not None else None
            if control is None and decision.action != "navigate":
                history.append("{}:INVALID_REF {}".format(decision.action, decision.ref))
                self.recorder.log("invalid_ref", step=index, ref=decision.ref)
                consecutive_failures += 1
                continue

            risk = self._risk_of(decision, control, obs)
            try:
                self.policy.check_action(decision.action)
                self.policy.check_risk(risk, mode="discovery",
                                       context="{} {!r}".format(decision.action,
                                                                control.name if control else ""))
            except ConfirmationRequired as need:
                self.recorder.log("confirmation_required", step=index, risk=risk.value,
                                  control=control.name if control else "", reason=need.message)
                outcome = self._escalate(
                    reason="{} Proposed action: {} {!r}.".format(
                        need.message, decision.action, control.name if control else ""),
                    trigger="confirmation_required", goal=goal, obs=obs)
                if outcome is None or outcome.resolution == "aborted":
                    return self._finish("aborted", trace, outputs, obs,
                                        reason="irreversible action was not confirmed",
                                        human_intervened=outcome is not None, steps=index)
                human_intervened = True
                trace.extend(self._operator_trace(outcome, index))
                if outcome.operator_finished_the_work:
                    obs = self.surface.observe(index)
                    return self._finish("completed", trace, outputs, obs,
                                        reason="irreversible step completed by operator",
                                        human_intervened=True, steps=index)
                # Operator confirmed and handed back: perform the step, and
                # record it as confirmed-risky so the artifact carries the
                # correct risk level into replay.
                self.recorder.log("confirmation_granted", step=index, risk=risk.value)
            except PolicyViolation as violation:
                self.recorder.log("policy_blocked", step=index, error=str(violation))
                return self._finish("policy_blocked", trace, outputs, obs,
                                    reason=str(violation), human_intervened=human_intervened,
                                    steps=index)

            step = self._act(index, decision, control, obs, params, risk)
            if step is None:
                history.append("{}:FAILED".format(decision.action))
                consecutive_failures += 1
                continue
            consecutive_failures = 0
            trace.append(step)
            history.append(self._history_line(decision, control))

        obs = self.surface.observe(self.max_steps)
        outcome = self._escalate(
            reason="Step budget of {} exhausted without reaching the goal.".format(self.max_steps),
            trigger="no_progress", goal=goal, obs=obs)
        return self._finish("max_steps", trace, outputs, obs,
                            reason="step budget exhausted",
                            human_intervened=human_intervened or bool(outcome),
                            steps=self.max_steps)

    # -- internals ---------------------------------------------------------
    def _decide(self, obs, goal, params, history, outputs) -> Decision:
        """Call the planner, tolerating a malformed response once or twice.

        A model that emits prose instead of JSON is a transient annoyance, not
        a reason to fail a run. Persistent failure becomes `stuck`, which
        routes to a human like every other dead end.
        """
        last_error = ""
        attempt = 0
        rate_limit_waits = 0
        while attempt <= _MAX_PLANNER_RETRIES:
            try:
                return self.planner.decide(obs, goal, params, history, outputs)
            except RateLimited as limited:
                # Waiting out a rate limit is not a failed attempt -- the
                # provider told us exactly when it will answer. Burning a
                # retry here would strand a perfectly healthy run as "stuck"
                # purely because a free tier throttled us mid-flow.
                if rate_limit_waits >= _MAX_RATE_LIMIT_WAITS:
                    last_error = str(limited)
                    break
                rate_limit_waits += 1
                delay = min(limited.retry_after + 0.5, 30.0)
                self.recorder.log("planner_rate_limited", wait_s=round(delay, 2),
                                  waits=rate_limit_waits)
                time.sleep(delay)
                continue
            except PlannerError as exc:
                last_error = str(exc)
                self.recorder.log("planner_error", attempt=attempt, error=last_error)
                time.sleep(0.5 * (attempt + 1))
                attempt += 1
        return Decision(action="stuck",
                        reasoning="Planner failed after retries: {}".format(last_error))

    def _risk_of(self, decision: Decision, control: Optional[Control], obs: Observation) -> RiskLevel:
        """Classify the risk of the proposed action.

        Read from the *surface*, not from the model's prose. Letting a model's
        self-description decide whether something is irreversible would mean a
        model that says "just a small click" gets to bypass the guardrail.
        The signals used here are the ones the application itself puts on
        screen: an explicit irreversibility warning near a submit control.
        """
        if decision.action in ("extract", "navigate"):
            return RiskLevel.SAFE
        if decision.action in ("fill", "select"):
            return RiskLevel.REVERSIBLE
        # click
        name = (control.name if control else "").lower()
        text = obs.text.lower()
        commits = any(k in name for k in ("submit", "confirm", "post", "authorize", "finish"))
        if not commits:
            return RiskLevel.REVERSIBLE
        # A commit-shaped caption alone is not enough: "Submit" is also what a
        # search button on a read-only screen is called, and treating those as
        # irreversible means pausing for a human on every lookup. The screen's
        # own warning is the corroborating signal, and it is the one the
        # application deliberately puts there before a real commit.
        warned = any(k in text for k in ("cannot be reversed", "cannot be undone",
                                         "cannot be cancelled", "irreversible",
                                         "opens the account immediately",
                                         "will be posted"))
        return RiskLevel.IRREVERSIBLE if warned else RiskLevel.REVERSIBLE

    def _act(self, index: int, decision: Decision, control: Optional[Control],
             obs: Observation, params: Dict[str, str], risk: RiskLevel) -> Optional[TraceStep]:
        from ..artifact.schema import Locator

        param_name = _param_ref(decision.value)
        literal = None if param_name else decision.value
        actual = params.get(param_name, "") if param_name else decision.value

        step = TraceStep(
            index=index, action=decision.action,
            role=control.role if control else "",
            name=control.name if control else "",
            hints=dict(control.hints) if control else {},
            param=param_name, literal=literal, risk=risk.value,
            reasoning=decision.reasoning,
            location_before=obs.location, title_before=obs.title, text_before=obs.text,
        )

        try:
            if decision.action == "navigate":
                url = actual or ""
                self.policy.check_location(url)
                self.surface.navigate(url)
            else:
                locator = Locator(
                    strategies=[{
                        "kind": "role_name",
                        "role": control.role,
                        "name": control.name,
                        "frame": control.hints.get("frame", ""),
                    }],
                    description=control.name,
                )
                target = self.surface.resolve(locator, {})
                if decision.action == "click":
                    self.surface.click(target)
                elif decision.action == "fill":
                    self.surface.fill(target, actual or "")
                elif decision.action == "select":
                    self.surface.select(target, actual or "")
            self.surface.wait_settled()
        except SurfaceError as exc:
            self.recorder.log("action_failed", step=index, action=decision.action,
                              control=control.name if control else "", error=str(exc))
            return None
        except Exception as exc:  # playwright timeouts etc.
            self.recorder.log("action_failed", step=index, action=decision.action,
                              control=control.name if control else "",
                              error="{}: {}".format(type(exc).__name__, exc))
            return None

        self._check_locations()
        after = self.surface.observe(index)
        step.location_after = after.location
        step.title_after = after.title
        step.text_after = after.text
        self.recorder.log("action", step=index, action=decision.action,
                          control=control.name if control else "",
                          param=param_name, risk=risk.value,
                          location_after=after.location)
        return step

    def _extract(self, decision: Decision, obs: Observation) -> Optional[str]:
        if decision.label:
            value = self.surface.read_labelled_value(decision.label)
            if value:
                return value
        # The model named a field but the label lookup missed. Fall back to
        # reading the value it claimed to see, but only if that text is
        # actually present on screen -- never take a model's word for data.
        claimed = (decision.value or "").strip()
        if claimed and claimed in obs.text:
            return claimed
        return None

    def _escalate(self, *, reason: str, trigger: str, goal: str, obs: Observation):
        """Raise an intervention, up to a hard cap per run.

        The cap matters because an operator channel that answers `resume`
        without changing anything -- a scripted one, or a human who glanced at
        it and clicked through -- otherwise produces an escalate/retry/escalate
        loop that burns the whole step budget and a model call each time. After
        `max_escalations` the run stops and says so, which is both cheaper and
        more honest than asking a twentieth time.
        """
        if self.escalation is None:
            return None
        if self._escalations >= self.max_escalations:
            self.recorder.log("escalation_budget_exhausted",
                              raised=self._escalations, trigger=trigger)
            return None
        self._escalations += 1
        return self.escalation.escalate(
            self.surface, reason=reason, trigger=trigger, mode="discovery",
            goal=goal, observed=obs.location,
        )

    def _operator_trace(self, outcome, index: int) -> List[TraceStep]:
        """Fold the operator's actions into the trace.

        A human-assisted discovery run can still become a capability -- the
        operator's steps were real steps against the real app. They are marked
        `by_operator` so the recorder can flag the artifact for review rather
        than quietly presenting it as machine-discovered.
        """
        steps: List[TraceStep] = []
        for action in outcome.actions:
            if action.verb not in ("click", "fill", "select") or not action.ok:
                continue
            steps.append(TraceStep(
                index=index, action=action.verb,
                name=str(action.args.get("name", "")),
                literal=str(action.args.get("value")) if action.args.get("value") else None,
                reasoning="performed by human operator during intervention",
                risk=RiskLevel.REVERSIBLE.value, by_operator=True,
            ))
        return steps

    def _check_locations(self) -> None:
        for url in self.surface.locations():
            self.policy.check_location(url)

    def _finish(self, status: str, trace, outputs, obs, *, reason: str,
                human_intervened: bool, steps: int) -> DiscoveryResult:
        self.recorder.log("discovery_finished", status=status, reason=reason,
                          steps=steps, outputs=list(outputs.keys()),
                          human_intervened=human_intervened)
        return DiscoveryResult(
            status=status, trace=trace, outputs=outputs,
            final_location=obs.location, final_title=obs.title, final_text=obs.text,
            reason=reason, human_intervened=human_intervened, steps_taken=steps,
        )

    def _history_line(self, decision: Decision, control: Optional[Control]) -> str:
        if decision.action in ("fill", "select") and decision.value:
            key = _param_ref(decision.value) or "literal"
            return "{}:{}".format(decision.action, key)
        return "{}:{}".format(decision.action, (control.name if control else "").lower())


def _param_ref(value: Optional[str]) -> Optional[str]:
    if isinstance(value, str) and value.startswith("{") and value.endswith("}"):
        return value[1:-1]
    return None
