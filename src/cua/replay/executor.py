"""
Deterministic replay -- the production execution path.

**No model is imported or reachable from this module.** Every action, every
locator, every checkpoint and every recovery comes from the artifact. The only
choices the executor makes are mechanical and bounded: try the next locator
strategy, wait and retry once, apply a declared recovery rule, classify the
resulting state.

The control flow per step is deliberate and the ordering matters:

    1. Check for a declared BUSINESS OUTCOME on the current screen.
       Before acting. A "no such member" banner produced by the *previous*
       step must be read as an answer, not as the next step's locator failing.
       Getting this order wrong is exactly how "member not found" turns into
       a stack trace.

    2. Check for a declared RECOVERY condition and apply it, bounded by the
       rule's attempt cap.

    3. Enforce POLICY: allowlist, action type, and the risk gate.

    4. RESOLVE the locator through the ranked strategy list, recording which
       rank won (drift signal).

    5. ACT, with a bounded retry on timeout.

    6. VERIFY the checkpoint. If it fails, re-check business outcomes before
       declaring a hard failure -- the checkpoint may have failed *because*
       the app returned a legitimate outcome, and that must not be reported
       as breakage.

Determinism, concretely:
  - the step sequence is fixed by the artifact;
  - no wait is a bare sleep tied to wall-clock timing -- every wait is
    "until the surface settles" or a bounded retry;
  - locator resolution is ordered and total (it either resolves via a named
    strategy or raises);
  - the same inputs against the same app state produce the same result and
    the same outputs.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

from ..artifact.schema import ActionType, Capability, Condition, RiskLevel, Step
from ..escalation.manager import EscalationManager
from ..safety.policy import ConfirmationRequired, Policy, PolicyViolation
from ..surfaces.base import Surface, SurfaceError
from .result import (
    BUSINESS_OUTCOME,
    ESCALATED,
    HARD_FAILURE,
    POLICY_BLOCKED,
    SUCCESS,
    DetectedOutcome,
    DriftSignal,
    HardFailure,
    RecoveryEvent,
    ReplayResult,
)

_ACT_RETRIES = 2
_SETTLE_MS = 6000


class ReplayExecutor:
    def __init__(self, surface: Surface, policy: Policy, recorder,
                 escalation: Optional[EscalationManager] = None, lease=None):
        self.surface = surface
        self.policy = policy
        self.recorder = recorder
        self.escalation = escalation
        self.lease = lease

    def replay(self, capability: Capability, params: Dict[str, Any],
               base_url: str) -> ReplayResult:
        started = time.time()
        result = ReplayResult(
            status=SUCCESS,
            capability_id=capability.id,
            capability_version=capability.version,
            run_id=self.recorder.run_id,
            steps_total=len(capability.steps),
            evidence_dir=self.recorder.dir,
        )

        # Contract validation before touching the surface. A calling agent
        # that omitted a required input deserves that answer immediately, not
        # a locator timeout forty seconds in.
        problems = capability.validate_params(params)
        if problems:
            result.status = HARD_FAILURE
            result.failure = HardFailure(
                "Input does not satisfy the capability contract: " + "; ".join(problems),
                kind="contract_violation",
            )
            self.recorder.log("contract_violation", problems=problems)
            result.duration_s = time.time() - started
            return result

        params = {k: str(v) for k, v in params.items()}
        self.recorder.log("replay_started", capability=capability.id,
                          version=capability.version, status=capability.status,
                          base_url=base_url, steps=len(capability.steps))

        recovery_budget: Dict[str, int] = {}
        # One human retry per step. A person who hands back a session that is
        # still broken should not be asked about the same step forever.
        escalation_retries: set = set()

        try:
            entry = base_url.rstrip("/") + capability.target.entry_path
            self.policy.check_location(entry)
            self.surface.navigate(entry)
            self._settle()

            index = 0
            while index < len(capability.steps):
                step = capability.steps[index]
                self._guard_locations()
                if self.lease:
                    self.lease.assert_automation()

                # 1. Business outcome produced by everything so far.
                outcome = self._detect_outcome(capability, step.id)
                if outcome is not None:
                    return self._as_business_outcome(result, outcome, index, started)

                # 2. Declared recovery conditions.
                recovered = self._apply_recoveries(capability, step, index, result, recovery_budget, params)
                if recovered == "restarted":
                    # The prefix was re-run; retry the same step.
                    continue
                if recovered == "escalate":
                    _verb, escalated = self._escalate_failure(
                        result, capability,
                        HardFailure("A recovery rule needed to re-run a prefix containing an "
                                    "irreversible step; refusing to risk a double commit.",
                                    step_id=step.id, kind="recovery_refused"),
                        index, started, params, already_retried=True)
                    return escalated

                # 3. Policy.
                try:
                    self.policy.check_action(step.action.value)
                    self.policy.check_risk(
                        step.risk, mode="replay", context=step.notes or step.id,
                        capability_approved=(capability.status == "approved"),
                    )
                except PolicyViolation as violation:
                    self.recorder.log("policy_blocked", step_id=step.id, error=str(violation))
                    result.status = POLICY_BLOCKED
                    result.failure = HardFailure(str(violation), step_id=step.id,
                                                 kind="policy_violation")
                    result.steps_completed = index
                    result.duration_s = time.time() - started
                    return result
                except ConfirmationRequired as need:
                    # An irreversible step in an unapproved capability. This is
                    # the approval gate doing its job, and it routes to a human
                    # rather than simply refusing.
                    self.recorder.log("confirmation_required", step_id=step.id,
                                      risk=step.risk.value, reason=need.message)
                    outcome_h = self._escalate(
                        capability, reason=need.message, trigger="confirmation_required",
                        step_id=step.id, expected="human approval for an irreversible step",
                        observed="capability status is {!r}".format(capability.status))
                    if outcome_h is None or outcome_h.resolution == "aborted":
                        result.status = ESCALATED
                        result.escalation = outcome_h.to_dict() if outcome_h else {
                            "resolution": "unhandled", "actions": []}
                        result.failure = HardFailure(
                            need.message, step_id=step.id, kind="approval_required")
                        result.steps_completed = index
                        result.duration_s = time.time() - started
                        return result
                    if outcome_h.operator_finished_the_work:
                        # Do NOT just stamp success here. The operator says they
                        # committed it by hand; the capability's own success
                        # condition is what decides whether they did.
                        verified = self._accept_operator_completion(
                            result, capability, params, started, outcome_h)
                        if verified is not None:
                            return verified
                        result.status = ESCALATED
                        result.failure = HardFailure(
                            "The operator reported completing the step by hand, but the "
                            "capability's success condition does not hold.",
                            step_id=step.id, kind="checkpoint_failed",
                            expected=_describe(capability.final_checkpoint, params),
                            observed=self._location())
                        result.steps_completed = index
                        result.duration_s = time.time() - started
                        return result
                    result.escalation = outcome_h.to_dict()

                # 4-6. Resolve, act, verify.
                try:
                    self._execute(step, params, result)
                except HardFailure as failure:
                    # A step that just failed is the single most informative
                    # moment to reclassify. Ask, in order:
                    #   is this actually a declared business outcome?
                    #   is this a declared recoverable condition?
                    # Only if both say no is it genuinely breakage.
                    #
                    # Session expiry is why this ordering matters: the session
                    # dies partway through, the next checkpoint fails, and the
                    # correct response is to re-authenticate and carry on --
                    # not to page a human about a "checkpoint failure".
                    outcome = self._detect_outcome(capability, step.id)
                    if outcome is not None:
                        return self._as_business_outcome(result, outcome, index + 1, started)

                    recovered = self._apply_recoveries(
                        capability, step, index, result, recovery_budget, params)
                    if recovered in ("restarted", "recovered"):
                        self.recorder.log("recovered_after_failure", step_id=step.id,
                                          original_failure=failure.kind)
                        continue  # retry the same step
                    if recovered == "escalate":
                        failure = HardFailure(
                            "A recovery rule needed to re-run a prefix containing an "
                            "irreversible step; refusing to risk a double commit.",
                            step_id=step.id, kind="recovery_refused")

                    # Nothing automatic worked: bring in a human.
                    verb, escalated = self._escalate_failure(
                        result, capability, failure, index, started, params,
                        already_retried=step.id in escalation_retries)
                    if verb == "retry":
                        # The operator repaired the session by hand and handed
                        # control back. Resume on the *same* live session by
                        # re-attempting the step that failed -- the point of
                        # the handoff is that the run continues, not that it
                        # reports "a human looked at it".
                        escalation_retries.add(step.id)
                        continue
                    return escalated

                self.recorder.log("step_ok", step_id=step.id, action=step.action.value,
                                  location=self._location())
                index += 1

            # Final checkpoint.
            if capability.final_checkpoint is not None:
                if not self._check(capability.final_checkpoint, params):
                    outcome = self._detect_outcome(capability, "final")
                    if outcome is not None:
                        return self._as_business_outcome(result, outcome, len(capability.steps), started)
                    failure = HardFailure(
                        "Final checkpoint did not verify; the flow did not reach its success state.",
                        step_id="final",
                        expected="{}: {}".format(capability.final_checkpoint.kind,
                                                 _fill(capability.final_checkpoint.value, params)),
                        observed=self._location(),
                        kind="checkpoint_failed",
                    )
                    _verb, escalated = self._escalate_failure(
                        result, capability, failure, len(capability.steps), started, params,
                        already_retried=True)
                    return escalated

            return self._finish(result, capability, params, len(capability.steps), started)

        except PolicyViolation as violation:
            self.recorder.log("policy_blocked", error=str(violation))
            result.status = POLICY_BLOCKED
            result.failure = HardFailure(str(violation), kind="policy_violation")
            result.duration_s = time.time() - started
            return result
        except Exception as exc:  # pragma: no cover - last-resort net
            self.recorder.capture(self.surface, "unexpected_error")
            result.status = HARD_FAILURE
            result.failure = HardFailure(
                "Unexpected executor error: {}: {}".format(type(exc).__name__, exc),
                observed=self._location(), kind="surface_error")
            result.duration_s = time.time() - started
            self.recorder.log("unexpected_error", error=str(exc))
            return result

    # -- step execution ----------------------------------------------------
    def _execute(self, step: Step, params: Dict[str, str], result: ReplayResult) -> None:
        if step.action == ActionType.EXTRACT:
            value = self._extract(step, params)
            if value is None:
                raise HardFailure(
                    "Could not read the declared output {!r}.".format(
                        step.extract.output_name if step.extract else "?"),
                    step_id=step.id,
                    expected="a value next to the label {!r}".format(
                        step.extract.label if step.extract else ""),
                    observed=self._location(),
                    kind="checkpoint_failed",
                )
            result.outputs[step.extract.output_name] = value
            self.recorder.log("extracted", step_id=step.id,
                              output_name=step.extract.output_name, value=value)
            return

        value = self._value_for(step, params)

        if step.action == ActionType.NAVIGATE:
            url = _fill(step.literal or "", params)
            if not url.startswith("http"):
                url = self._base_from_location() + url
            self.policy.check_location(url)
            self.surface.navigate(url)
            self._settle()
        elif step.action == ActionType.ASSERT:
            pass
        else:
            target = self._resolve(step, params)
            if target.strategy_index > 0:
                result.drift.append(DriftSignal(
                    step_id=step.id, strategy_index=target.strategy_index,
                    strategy_kind=target.strategy_kind,
                    description="primary locator strategy no longer resolves",
                ))
                self.recorder.log("locator_drift", step_id=step.id,
                                  strategy_index=target.strategy_index,
                                  strategy_kind=target.strategy_kind)
            self._act(step, target, value)

        self._guard_locations()

        if step.checkpoint is not None and not self._check(step.checkpoint, params):
            raise HardFailure(
                "Checkpoint did not verify after {}.".format(step.action.value),
                step_id=step.id,
                expected="{}: {}".format(step.checkpoint.kind, _fill(step.checkpoint.value, params)),
                observed=self._location(),
                kind="checkpoint_failed",
            )

    def _act(self, step: Step, target, value: Optional[str]) -> None:
        """Perform the action, retrying a bounded number of times on timeout.

        A timeout is not a failure until the retries are spent. This is the
        'transient slowness' arm of the taxonomy and it is handled here rather
        than as a declared rule because it has no on-screen signal to detect.
        """
        last: Optional[Exception] = None
        for attempt in range(_ACT_RETRIES + 1):
            try:
                if step.action == ActionType.CLICK:
                    self.surface.click(target)
                elif step.action == ActionType.FILL:
                    self.surface.fill(target, value or "")
                elif step.action == ActionType.SELECT:
                    self.surface.select(target, value or "")
                self._settle()
                return
            except Exception as exc:
                if not _is_timeout(exc):
                    raise HardFailure(
                        "Action {} failed: {}: {}".format(step.action.value, type(exc).__name__, exc),
                        step_id=step.id, observed=self._location(), kind="surface_error")
                last = exc
                self.recorder.log("transient_timeout", step_id=step.id, attempt=attempt + 1)
                time.sleep(1.0 + attempt)
        raise HardFailure(
            "Action {} timed out after {} attempts.".format(step.action.value, _ACT_RETRIES + 1),
            step_id=step.id, expected="the screen to respond",
            observed="{} ({})".format(self._location(), last), kind="surface_error")

    def _resolve(self, step: Step, params: Dict[str, str]):
        try:
            return self.surface.resolve(step.locator, params)
        except SurfaceError as exc:
            raise HardFailure(
                str(exc), step_id=step.id,
                expected=step.locator.description if step.locator else "",
                observed=self._location(), kind="locator_unresolved")

    def _extract(self, step: Step, params: Dict[str, str]) -> Optional[str]:
        spec = step.extract
        if spec is None:
            return None
        if spec.method == "labelled_value":
            return self.surface.read_labelled_value(_fill(spec.label, params))
        if spec.method == "regex":
            import re
            match = re.search(_fill(spec.pattern, params), self.surface.text())
            return match.group(1) if match else None
        if spec.method == "text_of" and spec.locator is not None:
            try:
                return self.surface.read_text_of(self.surface.resolve(spec.locator, params))
            except SurfaceError:
                return None
        return None

    def _value_for(self, step: Step, params: Dict[str, str]) -> Optional[str]:
        if step.param:
            return params.get(step.param, "")
        if step.literal is not None:
            return _fill(step.literal, params)
        return None

    # -- conditions --------------------------------------------------------
    def _check(self, condition: Condition, params: Dict[str, str]) -> bool:
        value = _fill(condition.value, params)
        if condition.kind == "url_contains":
            return value in self._location()
        if condition.kind == "text_present":
            return value.lower() in self.surface.text().lower()
        if condition.kind == "text_absent":
            return value.lower() not in self.surface.text().lower()
        if condition.kind == "control_present":
            obs = self.surface.observe()
            return any(value.lower() in c.name.lower() for c in obs.controls)
        if condition.kind == "on_timeout":
            # Only the retry path engages this; it is never true from a page read.
            return False
        return False

    def _detect_outcome(self, capability: Capability, step_id: str) -> Optional[DetectedOutcome]:
        text = self.surface.text().lower()
        location = self._location()
        for outcome in capability.business_outcomes:
            detect = outcome.detect
            hit = False
            if detect.kind == "text_present":
                hit = detect.value.lower() in text
            elif detect.kind == "url_contains":
                hit = detect.value in location
            elif detect.kind == "text_absent":
                hit = detect.value.lower() not in text
            if hit:
                return DetectedOutcome(name=outcome.name, description=outcome.description,
                                       step_id=step_id, matched=detect.value)
        return None

    # -- recovery ----------------------------------------------------------
    def _apply_recoveries(self, capability: Capability, step: Step, index: int,
                          result: ReplayResult, budget: Dict[str, int],
                          params: Dict[str, str]) -> Optional[str]:
        for rule in capability.recovery_rules:
            if rule.detect.kind == "on_timeout":
                continue  # handled inside _act
            if not self._check(rule.detect, params):
                continue
            used = budget.get(rule.name, 0)
            if used >= rule.max_attempts:
                self.recorder.log("recovery_exhausted", rule=rule.name, step_id=step.id)
                continue
            budget[rule.name] = used + 1

            kind = rule.action.get("kind")
            self.recorder.log("recovery_triggered", rule=rule.name, step_id=step.id,
                              action=kind, attempt=used + 1)

            if kind == "click":
                from ..artifact.schema import Locator
                locator = Locator.from_dict(rule.action["locator"])
                try:
                    target = self.surface.resolve(locator, params)
                    self.surface.click(target)
                    self._settle()
                except SurfaceError as exc:
                    self.recorder.log("recovery_failed", rule=rule.name, error=str(exc))
                    continue
                result.recoveries.append(RecoveryEvent(
                    rule=rule.name, step_id=step.id, action="click", attempt=used + 1,
                    detail=rule.description))
                return "recovered"

            if kind == "retry_step":
                time.sleep(rule.action.get("delay_ms", 1000) / 1000.0)
                result.recoveries.append(RecoveryEvent(
                    rule=rule.name, step_id=step.id, action="retry_step", attempt=used + 1))
                return "recovered"

            if kind == "restart_from_step":
                start_id = rule.action.get("step_id", "@first")
                start = 0 if start_id == "@first" else _index_of(capability, start_id)
                if start is None:
                    continue
                prefix = capability.steps[start:index]
                # Never re-run a commit in order to recover. Doing so would
                # post the transaction twice -- the recovery would cause a
                # worse failure than the one it is fixing.
                if any(s.risk == RiskLevel.IRREVERSIBLE for s in prefix):
                    self.recorder.log("recovery_refused", rule=rule.name, step_id=step.id,
                                      reason="prefix contains an irreversible step")
                    return "escalate"
                self.recorder.log("recovery_restart", rule=rule.name, from_step=start,
                                  to_step=index)
                for prior in prefix:
                    try:
                        self._execute(prior, params, result)
                    except HardFailure as failure:
                        self.recorder.log("recovery_failed", rule=rule.name,
                                          step_id=prior.id, error=str(failure))
                        return None
                result.recoveries.append(RecoveryEvent(
                    rule=rule.name, step_id=step.id, action="restart_from_step",
                    attempt=used + 1,
                    detail="re-ran {} step(s) to restore session state".format(len(prefix))))
                return "restarted"
        return None

    # -- terminal paths ----------------------------------------------------
    def _finish(self, result: ReplayResult, capability: Capability, params: Dict[str, str],
                steps_done: int, started: float,
                escalation: Optional[Dict[str, Any]] = None) -> ReplayResult:
        result.status = SUCCESS
        result.steps_completed = steps_done
        result.duration_s = time.time() - started
        if escalation:
            result.escalation = escalation
        redacted = {}
        for field_def in capability.outputs:
            if field_def.name in result.outputs:
                redacted[field_def.name] = ("[REDACTED]" if field_def.redact_in_evidence
                                            else result.outputs[field_def.name])
        self.recorder.log("replay_succeeded", outputs=redacted,
                          drift=len(result.drift), recoveries=len(result.recoveries))
        return result

    def _as_business_outcome(self, result: ReplayResult, outcome: DetectedOutcome,
                             steps_done: int, started: float) -> ReplayResult:
        result.status = BUSINESS_OUTCOME
        result.business_outcome = outcome
        result.steps_completed = steps_done
        result.duration_s = time.time() - started
        self.recorder.log("business_outcome", name=outcome.name, step_id=outcome.step_id,
                          matched=outcome.matched)
        # Evidence is captured for outcomes too. It is not a failure, but a
        # spike in `member_not_found` is something an operator will want to
        # look at, and by then the session is long gone.
        self.recorder.capture(self.surface, "outcome_{}".format(outcome.name))
        return result

    def _escalate_failure(self, result: ReplayResult, capability: Capability,
                          failure: HardFailure, index: int, started: float,
                          params: Dict[str, str], already_retried: bool = False):
        """Route a hard failure to a human.

        Returns ("retry", None) if the operator fixed things and handed back,
        or ("return", result) if the run is over.
        """
        self.recorder.log("hard_failure", **failure.to_dict())
        self.recorder.capture(self.surface, "hard_failure_{}".format(failure.step_id or index))

        outcome = self._escalate(
            capability, reason=failure.message, trigger="hard_failure",
            step_id=failure.step_id, expected=failure.expected, observed=failure.observed)

        result.steps_completed = index
        result.duration_s = time.time() - started
        result.failure = failure

        if outcome is None:
            result.status = HARD_FAILURE
            return "return", result

        result.escalation = outcome.to_dict()

        if outcome.should_continue and not already_retried:
            self.recorder.log("resuming_after_handoff", step_id=failure.step_id,
                              operator_actions=len(outcome.actions))
            result.failure = None
            return "retry", None

        if outcome.operator_finished_the_work:
            verified = self._accept_operator_completion(
                result, capability, params, started, outcome)
            if verified is not None:
                return "return", verified

        result.status = ESCALATED if outcome.resolution != "aborted" else HARD_FAILURE
        return "return", result

    def _accept_operator_completion(self, result: ReplayResult, capability: Capability,
                                    params: Dict[str, str], started: float, outcome):
        """An operator reported finishing the work by hand. Verify it.

        Returns the completed result, or None if the capability's success
        condition does not hold.

        The contract is the contract regardless of who executed the steps, so
        this re-checks `final_checkpoint` and re-reads every declared output
        from the live surface rather than taking the human's word for it. An
        operator who clicks "I finished it" without having submitted anything
        must not produce `status=success` with the declared outputs silently
        missing -- that is a wrong answer delivered confidently, which is worse
        than an honest failure.
        """
        if capability.final_checkpoint is not None and not self._check(
                capability.final_checkpoint, params):
            self.recorder.log("operator_claimed_done_but_checkpoint_failed",
                              expected=_describe(capability.final_checkpoint, params),
                              observed=self._location())
            return None

        for step in capability.steps:
            if step.action == ActionType.EXTRACT and step.extract:
                value = self._extract(step, params)
                if value:
                    result.outputs[step.extract.output_name] = value

        missing = [o.name for o in capability.outputs if o.name not in result.outputs]
        if missing:
            self.recorder.log("operator_claimed_done_but_outputs_missing", missing=missing)
            return None

        result.status = SUCCESS
        result.failure = None
        result.escalation = outcome.to_dict()
        result.steps_completed = len(capability.steps)
        result.duration_s = time.time() - started
        self.recorder.log("replay_completed_by_operator",
                          outputs=list(result.outputs.keys()))
        return result

    def _escalate(self, capability: Capability, *, reason: str, trigger: str,
                  step_id: str = "", expected: str = "", observed: str = ""):
        if self.escalation is None:
            return None
        return self.escalation.escalate(
            self.surface, reason=reason, trigger=trigger, mode="replay",
            capability_id=capability.id, step_id=step_id,
            expected=expected, observed=observed,
        )

    # -- plumbing ----------------------------------------------------------
    def _settle(self) -> None:
        try:
            self.surface.wait_settled(timeout_ms=_SETTLE_MS)
        except Exception:
            pass

    def _location(self) -> str:
        try:
            return self.surface.location()
        except Exception:
            return "<unavailable>"

    def _base_from_location(self) -> str:
        from urllib.parse import urlparse
        parsed = urlparse(self._location())
        return "{}://{}".format(parsed.scheme, parsed.netloc)

    def _guard_locations(self) -> None:
        for url in self.surface.locations():
            self.policy.check_location(url)


def _describe(condition, params: Dict[str, str]) -> str:
    """Render a condition for a human reading an error, with params filled in.

    `url_contains: /members/100237` is debuggable; `url_contains:
    /members/{member_id}` makes the reader do the substitution themselves.
    """
    if condition is None:
        return "(no success condition declared)"
    return "{}: {}".format(condition.kind, _fill(condition.value, params))


def _fill(value: str, params: Dict[str, str]) -> str:
    out = value or ""
    for key, val in (params or {}).items():
        out = out.replace("{" + key + "}", str(val))
    return out


def _index_of(capability: Capability, step_id: str) -> Optional[int]:
    for i, step in enumerate(capability.steps):
        if step.id == step_id:
            return i
    return None


def _is_timeout(exc: Exception) -> bool:
    return "timeout" in type(exc).__name__.lower() or "Timeout" in str(exc)
