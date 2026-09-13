"""
Session control lease.

The brief asks for "a way to know who is (or should be) in control". This is
that, and it is deliberately more than a boolean.

The constraint that shapes it: **the surface has exactly one owning thread.**
Playwright's sync API is not thread-safe, and the same is true of essentially
every OS-level automation API. So "handing control to a human" cannot mean
"let the operator's HTTP thread call page.click()". It means the owning thread
keeps driving the surface but starts taking its instructions from a person
instead of from the planner or the artifact.

That inversion is the whole control-transfer model:

    automation  --escalate-->  operator  --hand back-->  automation
        |                          |                          |
    planner/artifact          command queue             artifact resumes
    drives the surface        drives the surface        at the next step

The lease records who currently holds it, why, and since when, and emits an
event on every transition so the evidence log shows exactly which actions in
the run were taken by a machine and which by a person -- which is the audit
question a bank will actually ask.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

AUTOMATION = "automation"
OPERATOR = "operator"
NOBODY = "none"


@dataclass
class LeaseTransition:
    at: float
    from_owner: str
    to_owner: str
    reason: str


@dataclass
class SessionLease:
    """Who holds the right to drive this session right now."""

    session_id: str
    owner: str = AUTOMATION
    holder_label: str = "agent"
    since: float = field(default_factory=time.time)
    reason: str = "run started"
    history: List[LeaseTransition] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Called on every transition so the evidence log stays in sync without the
    # lease needing to know what a recorder is.
    on_transition: Optional[Callable[[LeaseTransition], None]] = field(default=None, repr=False)

    def transfer(self, to_owner: str, reason: str, holder_label: str = "") -> LeaseTransition:
        with self._lock:
            transition = LeaseTransition(
                at=time.time(), from_owner=self.owner, to_owner=to_owner, reason=reason
            )
            self.owner = to_owner
            self.holder_label = holder_label or to_owner
            self.since = transition.at
            self.reason = reason
            self.history.append(transition)
        if self.on_transition:
            self.on_transition(transition)
        return transition

    def held_by_automation(self) -> bool:
        return self.owner == AUTOMATION

    def assert_automation(self) -> None:
        """Guard on the automation's own action path.

        Cheap insurance: if a future refactor ever lets the loop keep acting
        after an escalation started, this turns a silent race over the live
        session into a loud failure.
        """
        if self.owner != AUTOMATION:
            raise RuntimeError(
                "Automation attempted to act while the session lease is held by "
                "{!r} (since {:.0f}, reason: {})".format(self.owner, self.since, self.reason)
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "owner": self.owner,
            "holder_label": self.holder_label,
            "since": self.since,
            "reason": self.reason,
            "transitions": [
                {"at": t.at, "from": t.from_owner, "to": t.to_owner, "reason": t.reason}
                for t in self.history
            ],
        }
