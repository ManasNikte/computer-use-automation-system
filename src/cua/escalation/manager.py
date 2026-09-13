"""
Escalation: detect stuck, route an intervention request, transfer control of
the live session to a human, take it back.

**How "stuck" is detected.** Four distinct triggers, all of which converge here:

  1. The planner itself declares `stuck` during discovery (it can see the
     screen and cannot find a way forward).
  2. The agent loop makes no progress -- the surface location and control set
     are unchanged across consecutive steps, or the step budget runs out.
  3. Replay raises a hard failure: no locator strategy resolved, or a
     checkpoint did not verify and the state matches no declared business
     outcome.
  4. The policy raises `ConfirmationRequired` for an irreversible action.

Only #3 and #4 can occur in production replay, and that is deliberate: the
production path escalates on *mechanical* failure or *authorisation*, never
because a model was uncertain.

**What gets routed.** An `InterventionRequest` carrying the capability/goal,
the current step, the live location, a screenshot, the flattened control tree,
and the reason. An operator should be able to act on it without opening the
code.

**How control transfers.** See `lease.py` -- the owning thread keeps driving
the surface but starts taking commands from the operator channel. The operator
is acting on the *same* browser session: same cookies, same page, same
half-filled form. Nothing is re-created.

**How it comes back.** The operator signals `resume` (automation continues from
the next step), `done` (the operator finished the work; the run is complete),
or `abort`. Every operator action is recorded into the evidence log and, on
discovery runs, is available to the recorder so a human-assisted flow can
still become an artifact -- flagged `human_intervened` so a reviewer knows.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..surfaces.base import Surface, SurfaceError
from .lease import AUTOMATION, OPERATOR, SessionLease


@dataclass
class InterventionRequest:
    """Everything a human needs to pick this up cold."""

    id: str
    run_id: str
    reason: str
    trigger: str                      # planner_stuck | no_progress | hard_failure | confirmation_required
    mode: str                         # discovery | replay
    goal: str = ""
    capability_id: str = ""
    step_id: str = ""
    location: str = ""
    expected: str = ""
    observed: str = ""
    screenshot: Optional[str] = None
    controls: Optional[str] = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        bits = [
            "Intervention {} ({})".format(self.id, self.trigger),
            "  mode      : {}".format(self.mode),
            "  reason    : {}".format(self.reason),
        ]
        if self.capability_id:
            bits.append("  capability: {} step={}".format(self.capability_id, self.step_id or "-"))
        if self.goal:
            bits.append("  goal      : {}".format(self.goal))
        bits.append("  location  : {}".format(self.location))
        if self.expected or self.observed:
            bits.append("  expected  : {}".format(self.expected))
            bits.append("  observed  : {}".format(self.observed))
        if self.screenshot:
            bits.append("  screenshot: {}".format(self.screenshot))
        return "\n".join(bits)


@dataclass
class OperatorAction:
    """One thing the human did while holding the lease."""

    verb: str
    args: Dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    detail: str = ""
    at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class InterventionOutcome:
    resolution: str                   # resumed | completed_by_operator | aborted
    actions: List[OperatorAction] = field(default_factory=list)
    notes: str = ""

    @property
    def should_continue(self) -> bool:
        return self.resolution == "resumed"

    @property
    def operator_finished_the_work(self) -> bool:
        return self.resolution == "completed_by_operator"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "resolution": self.resolution,
            "notes": self.notes,
            "actions": [a.to_dict() for a in self.actions],
        }


class OperatorChannel:
    """How an intervention reaches a human and how their commands come back.

    Implementations: `ConsoleOperatorChannel` (terminal), `WebOperatorChannel`
    (browser console for a remote operator), `ScriptedOperatorChannel`
    (replayable, for tests and reproducible evidence).

    `handle` runs **on the thread that owns the surface**. It is handed an
    `executor` callable rather than the surface itself, so a channel cannot
    accidentally drive the session from a background thread.
    """

    name = "abstract"

    def handle(self, request: InterventionRequest, executor: "OperatorExecutor") -> InterventionOutcome:
        raise NotImplementedError


class OperatorExecutor:
    """The verbs an operator can invoke against the live session.

    Deliberately the same primitive set the automation has -- an operator is
    not a privileged path around the guardrails. Policy is still enforced:
    a human driving this console still cannot navigate off the allowlist.
    That is a real decision, and arguably the wrong one for a true emergency;
    the escape hatch is that the operator can always use the actual browser
    window in `--headed` mode, which this system does not mediate at all.
    """

    def __init__(self, surface: Surface, policy=None, recorder=None, lease: Optional[SessionLease] = None):
        self._surface = surface
        self._policy = policy
        self._recorder = recorder
        self._lease = lease
        self.actions: List[OperatorAction] = []

    # -- introspection -----------------------------------------------------
    def controls(self) -> List[Dict[str, str]]:
        obs = self._surface.observe()
        return [{"ref": c.ref, "role": c.role, "name": c.name} for c in obs.controls]

    def location(self) -> str:
        return self._surface.location()

    def text(self) -> str:
        return self._surface.text()

    def screenshot(self, path: str) -> Optional[str]:
        return self._surface.screenshot(path)

    # -- action ------------------------------------------------------------
    def execute(self, verb: str, **args: Any) -> OperatorAction:
        action = OperatorAction(verb=verb, args=dict(args))
        # Register a credential the operator typed BEFORE anything is attempted
        # or logged. It has to happen here rather than deeper in `_dispatch`
        # because this method logs `args` whether the action succeeded or not:
        # an operator who mistypes a field name still typed their passcode, and
        # the failure path would otherwise write it to disk in cleartext.
        if (self._recorder and verb in ("fill", "select")
                and _looks_sensitive(str(args.get("name", "")))):
            self._recorder.redactor.register_secret(str(args.get("value", "")))
        try:
            detail = self._dispatch(verb, args)
            action.ok = True
            action.detail = detail or "ok"
        except Exception as exc:  # surfaced to the operator, never fatal
            action.ok = False
            action.detail = "{}: {}".format(type(exc).__name__, exc)
        self.actions.append(action)
        if self._recorder:
            self._recorder.log(
                "operator_action", verb=verb, args=args, ok=action.ok, detail=action.detail,
                location=self._safe_location(),
            )
        return action

    def _dispatch(self, verb: str, args: Dict[str, Any]) -> str:
        if verb == "click":
            target, matched = self._find(args.get("name", ""), args.get("role"),
                                         prefer_roles=("button", "link"))
            self._surface.click(target)
            self._settle()
            return "clicked {!r} -> {}".format(matched, self._safe_location())
        if verb == "fill":
            target, matched = self._find(args.get("name", ""), "textbox")
            value = str(args.get("value", ""))
            # The control's *resolved* name can differ from what the operator
            # typed ("pass" -> "Passcode"), so re-check it here too. `execute`
            # already registered based on the typed name; this catches the case
            # where only the real caption reveals it is a credential field.
            if self._recorder and _looks_sensitive(matched):
                self._recorder.redactor.register_secret(value)
            self._surface.fill(target, value)
            return "filled {!r}".format(matched)
        if verb == "select":
            target, matched = self._find(args.get("name", ""), "combobox")
            self._surface.select(target, str(args.get("value", "")))
            return "selected {!r} in {!r}".format(args.get("value"), matched)
        if verb == "goto":
            url = str(args.get("url", ""))
            if self._policy:
                self._policy.check_location(url)
            self._surface.navigate(url)
            return "navigated to {}".format(self._safe_location())
        raise ValueError("Unknown operator verb {!r}".format(verb))

    def _find(self, name: str, role: Optional[str] = None,
              prefer_roles: Tuple[str, ...] = ()):
        """Resolve a control by the name the operator typed.

        The naive version -- first control whose name contains the typed
        string -- is wrong in exactly the environment this system targets. A
        frameset console has a nav strip whose links duplicate the working
        area's vocabulary: typing `click Search` matches the nav link "Member
        Search" before the search form's "Search" button, and the operator
        silently reloads the page instead of running their query.

        So matches are ranked: exact name beats prefix beats substring, the
        working frame beats other frames, and for a click a button or link
        beats anything else. Ties are reported so the operator can see which
        control they actually hit rather than guessing.
        """
        from ..artifact.schema import Locator

        obs = self._surface.observe()
        wanted = (name or "").strip().lower()
        if not wanted:
            raise SurfaceError("No control name given.")

        working_frame = obs.working_context

        def score(c) -> Optional[tuple]:
            low = c.name.lower()
            if wanted == low:
                name_rank = 0
            elif low.startswith(wanted):
                name_rank = 1
            elif wanted in low:
                name_rank = 2
            else:
                return None
            if role is not None and c.role != role:
                return None
            role_rank = 0 if (not prefer_roles or c.role in prefer_roles) else 1
            frame_rank = 0 if c.hints.get("frame", "") == working_frame else 1
            return (role_rank, name_rank, frame_rank, len(c.name))

        scored = [(score(c), c) for c in obs.controls]
        matches = sorted(((s, c) for s, c in scored if s is not None), key=lambda pair: pair[0])
        if not matches:
            raise SurfaceError("No control matching {!r}{}".format(
                name, " with role {}".format(role) if role else ""))
        control = matches[0][1]
        if len(matches) > 1:
            others = ", ".join("{} {!r}".format(c.role, c.name) for _s, c in matches[1:4])
            self._ambiguity = "also matched: {}".format(others)
        locator = Locator(
            strategies=[{
                "kind": "role_name",
                "role": control.role,
                "name": control.name,
                "frame": control.hints.get("frame", ""),
            }],
            description=control.name,
        )
        return self._surface.resolve(locator, {}), control.name

    def _settle(self) -> None:
        try:
            self._surface.wait_settled(timeout_ms=5000)
        except Exception:
            pass

    def _safe_location(self) -> str:
        try:
            return self._surface.location()
        except Exception:
            return "<unavailable>"


class EscalationManager:
    """Owns the lease and brokers the handoff."""

    def __init__(self, lease: SessionLease, channel: OperatorChannel, recorder,
                 policy=None, enabled: bool = True):
        self.lease = lease
        self.channel = channel
        self.recorder = recorder
        self.policy = policy
        self.enabled = enabled
        self.requests: List[InterventionRequest] = []

    def escalate(self, surface: Surface, *, reason: str, trigger: str, mode: str,
                 goal: str = "", capability_id: str = "", step_id: str = "",
                 expected: str = "", observed: str = "") -> InterventionOutcome:
        request = InterventionRequest(
            id="itv_" + uuid.uuid4().hex[:8],
            run_id=self.recorder.run_id,
            reason=reason,
            trigger=trigger,
            mode=mode,
            goal=goal,
            capability_id=capability_id,
            step_id=step_id,
            location=_safe(surface.location),
            expected=expected,
            observed=observed,
        )

        # Capture *before* transferring control, so the evidence shows the
        # state the automation actually got stuck in rather than whatever the
        # operator has already changed it to.
        captured = self.recorder.capture(surface, "intervention_{}".format(request.id))
        request.screenshot = captured.get("screenshot")
        request.controls = captured.get("controls")
        self.requests.append(request)

        self.recorder.log("intervention_raised", **request.to_dict())

        if not self.enabled:
            # Escalation disabled (e.g. an unattended batch run with no
            # operator on shift). Say so explicitly rather than hanging.
            self.recorder.log("intervention_unhandled", id=request.id,
                              note="escalation disabled; no operator channel")
            return InterventionOutcome(resolution="aborted",
                                       notes="Escalation disabled; no operator available.")

        self.lease.transfer(OPERATOR, "intervention {}".format(request.id),
                            holder_label=self.channel.name)
        executor = OperatorExecutor(surface, policy=self.policy, recorder=self.recorder,
                                    lease=self.lease)
        try:
            outcome = self.channel.handle(request, executor)
        finally:
            self.lease.transfer(AUTOMATION, "intervention {} closed".format(request.id),
                                holder_label="agent")

        outcome.actions = executor.actions
        self.recorder.log("intervention_resolved", id=request.id,
                          resolution=outcome.resolution,
                          operator_actions=len(outcome.actions),
                          notes=outcome.notes,
                          location=_safe(surface.location))
        self.recorder.capture(surface, "handback_{}".format(request.id))
        return outcome


# Field captions that mean "whatever is typed here must never be written down".
_SENSITIVE_FIELD_HINTS = ("passcode", "password", "pin", "secret", "token",
                          "ssn", "tax id", "credential", "security code")


def _looks_sensitive(field_name: str) -> bool:
    low = (field_name or "").lower()
    return any(hint in low for hint in _SENSITIVE_FIELD_HINTS)


def _safe(fn) -> str:
    try:
        return fn()
    except Exception:
        return "<unavailable>"
