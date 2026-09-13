"""
The replay result contract.

The brief names the most common design mistake in this problem: conflating an
expected business answer with a crash. So the three categories are modelled as
mutually exclusive, first-class outcomes rather than as one exception type
with a message field:

  SUCCESS           The flow completed and the final checkpoint verified.
                    Carries the declared outputs.

  BUSINESS_OUTCOME  The application gave a legitimate non-happy-path answer
                    that this capability *declared in its contract* --
                    "no such member", "not authorized", "deposit exceeds the
                    branch limit". The automation worked perfectly. The caller
                    must handle it. This is data, not an error.

  HARD_FAILURE      The automation itself could not proceed: no locator
                    strategy resolved, a checkpoint did not verify and the
                    state matches no declared outcome, or an unrecoverable app
                    error. Carries step id, expected, observed -- enough to
                    debug without re-running.

  POLICY_BLOCKED    Refused by a guardrail before acting. Separate from a hard
                    failure because nothing went wrong: the system correctly
                    declined.

  ESCALATED         Stopped and handed to a human, with the resolution.

Recoverable conditions are deliberately *not* a status. Dismissing an
interstitial or re-authenticating after a session timeout is something the
executor handled; the run continues and still ends in one of the statuses
above. They are recorded in `recoveries` so they are visible in evidence --
a capability that silently re-authenticates on every run is telling you
something, and you only find out if you count.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

SUCCESS = "success"
BUSINESS_OUTCOME = "business_outcome"
HARD_FAILURE = "hard_failure"
POLICY_BLOCKED = "policy_blocked"
ESCALATED = "escalated"


class HardFailure(Exception):
    """Raised internally by the executor; converted to a result at the top."""

    def __init__(self, message: str, step_id: str = "", expected: str = "",
                 observed: str = "", kind: str = "unknown"):
        super().__init__(message)
        self.message = message
        self.step_id = step_id
        self.expected = expected
        self.observed = observed
        # Coarse classification so callers can route without string-matching:
        # locator_unresolved | checkpoint_failed | app_error | surface_error |
        # contract_violation | recovery_refused
        self.kind = kind

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "step_id": self.step_id,
            "message": self.message,
            "expected": self.expected,
            "observed": self.observed,
        }

    def __str__(self) -> str:
        return "[{}] {} (expected: {!r}, observed: {!r})".format(
            self.step_id or "-", self.message, self.expected, self.observed)


@dataclass
class DetectedOutcome:
    name: str
    description: str = ""
    step_id: str = ""
    matched: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "step_id": self.step_id, "matched": self.matched}


@dataclass
class RecoveryEvent:
    rule: str
    step_id: str
    action: str
    attempt: int = 1
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"rule": self.rule, "step_id": self.step_id, "action": self.action,
                "attempt": self.attempt, "detail": self.detail}


@dataclass
class DriftSignal:
    """A step that resolved via a fallback locator strategy.

    Not an error -- the step worked. It is an early warning: the primary way
    of addressing that control has stopped working, and the capability is now
    running on its backup. Surfacing this is how you find out before the
    backup fails too.
    """

    step_id: str
    strategy_index: int
    strategy_kind: str
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"step_id": self.step_id, "strategy_index": self.strategy_index,
                "strategy_kind": self.strategy_kind, "description": self.description}


@dataclass
class ReplayResult:
    status: str
    capability_id: str = ""
    capability_version: int = 0
    run_id: str = ""
    outputs: Dict[str, Any] = field(default_factory=dict)
    business_outcome: Optional[DetectedOutcome] = None
    failure: Optional[HardFailure] = None
    escalation: Optional[Dict[str, Any]] = None
    recoveries: List[RecoveryEvent] = field(default_factory=list)
    drift: List[DriftSignal] = field(default_factory=list)
    steps_completed: int = 0
    steps_total: int = 0
    duration_s: float = 0.0
    evidence_dir: str = ""

    @property
    def ok(self) -> bool:
        """True only for a clean success.

        A business outcome is deliberately not `ok`: the caller got a valid
        answer, but the thing they asked for did not happen.
        """
        return self.status == SUCCESS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "capability": {"id": self.capability_id, "version": self.capability_version},
            "run_id": self.run_id,
            "outputs": self.outputs,
            "business_outcome": self.business_outcome.to_dict() if self.business_outcome else None,
            "failure": self.failure.to_dict() if self.failure else None,
            "escalation": self.escalation,
            "recoveries": [r.to_dict() for r in self.recoveries],
            "drift": [d.to_dict() for d in self.drift],
            "steps_completed": self.steps_completed,
            "steps_total": self.steps_total,
            "duration_s": round(self.duration_s, 2),
            "evidence_dir": self.evidence_dir,
        }

    def summary(self) -> str:
        head = {
            SUCCESS: "SUCCESS",
            BUSINESS_OUTCOME: "BUSINESS OUTCOME",
            HARD_FAILURE: "HARD FAILURE",
            POLICY_BLOCKED: "POLICY BLOCKED",
            ESCALATED: "ESCALATED",
        }.get(self.status, self.status.upper())
        lines = ["{}  ({}/{} steps, {:.1f}s)".format(
            head, self.steps_completed, self.steps_total, self.duration_s)]
        if self.outputs:
            for k, v in self.outputs.items():
                lines.append("  output   {} = {}".format(k, v))
        if self.business_outcome:
            lines.append("  outcome  {}".format(self.business_outcome.name))
            if self.business_outcome.description:
                lines.append("           {}".format(self.business_outcome.description))
        if self.failure:
            lines.append("  failure  {} at step {!r}".format(self.failure.kind, self.failure.step_id))
            lines.append("           {}".format(self.failure.message))
            if self.failure.expected:
                lines.append("           expected: {}".format(self.failure.expected))
            if self.failure.observed:
                lines.append("           observed: {}".format(self.failure.observed))
        for r in self.recoveries:
            lines.append("  recovery {} at {} ({})".format(r.rule, r.step_id, r.detail or r.action))
        for d in self.drift:
            lines.append("  drift    {} resolved via fallback #{} ({})".format(
                d.step_id, d.strategy_index, d.strategy_kind))
        if self.escalation:
            lines.append("  human    {} ({} action(s))".format(
                self.escalation.get("resolution"), len(self.escalation.get("actions", []))))
        if self.evidence_dir:
            lines.append("  evidence {}".format(self.evidence_dir))
        return "\n".join(lines)
