"""
The guardrail model.

Three orthogonal gates, checked at different moments:

  1. **Navigation allowlist.** Where the automation is permitted to be. Checked
     before every navigation *and* re-checked after every action, because a
     click can navigate you somewhere a `goto` check never saw. An app that
     redirects to an external SSO host should stop the run, not follow it.

  2. **Action allowlist.** What verbs are permitted at all. A read-only
     capability can be pinned to `navigate`/`click`/`extract` so that a
     mis-recorded `fill` is refused rather than executed.

  3. **Risk gate.** How irreversible actions are treated, which differs by
     execution mode:

       - During **discovery**, an irreversible step is *not* silently
         performed. It raises `ConfirmationRequired`, which the agent loop
         turns into a human intervention request. A model gets to propose
         "submit this account opening"; it does not get to do it unsupervised.

       - During **replay**, an irreversible step runs only if the capability
         has been explicitly approved (draft → approved is a human review
         gate). An unapproved artifact stops at the irreversible step and
         escalates.

     So: every irreversible action in this system has a human behind it, either
     live at discovery time or earlier via approval of the artifact. That is
     the justification for choosing "confirm" over "block" or "flag" -- blocking
     outright would make the system unable to record the flows that matter
     most, and flag-and-proceed is not a guardrail.

The policy is data, loaded from JSON, so a per-app or per-tenant policy is a
config change rather than a code change.
"""
from __future__ import annotations

import fnmatch
import json
import posixpath
from dataclasses import dataclass, field
from typing import Any, Dict, List
from urllib.parse import unquote, urlparse

from ..artifact.schema import RiskLevel


class PolicyViolation(Exception):
    """A hard stop. The automation attempted something outside the allowlist."""


class ConfirmationRequired(Exception):
    """Not a violation -- a pause. A human must decide before this proceeds.

    Carries enough context for the escalation manager to build a useful
    intervention request without re-deriving it.
    """

    def __init__(self, message: str, risk: str = "", context: str = ""):
        super().__init__(message)
        self.message = message
        self.risk = risk
        self.context = context


@dataclass
class Policy:
    name: str = "default"
    # Origins the automation may be on, e.g. "http://127.0.0.1:5075".
    allowed_origins: List[str] = field(default_factory=list)
    # Glob patterns over the URL path. "*" permits everything on an allowed
    # origin; narrower patterns are how you keep a servicing capability out of
    # the admin console.
    allowed_path_globs: List[str] = field(default_factory=lambda: ["*"])
    # Paths that are refused even if they match an allow glob. Deny wins.
    denied_path_globs: List[str] = field(default_factory=list)
    allowed_actions: List[str] = field(
        default_factory=lambda: ["navigate", "click", "fill", "select", "extract", "assert"]
    )
    # Risk handling.
    allow_irreversible_in_discovery: bool = False   # False => confirm via human
    allow_irreversible_in_replay: bool = True       # True => but only when approved
    require_approval_for_irreversible: bool = True
    max_steps: int = 30

    # -- construction ------------------------------------------------------
    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Policy":
        base = Policy()
        return Policy(
            name=d.get("name", base.name),
            allowed_origins=list(d.get("allowed_origins", base.allowed_origins)),
            allowed_path_globs=list(d.get("allowed_path_globs", base.allowed_path_globs)),
            denied_path_globs=list(d.get("denied_path_globs", base.denied_path_globs)),
            allowed_actions=list(d.get("allowed_actions", base.allowed_actions)),
            allow_irreversible_in_discovery=bool(
                d.get("allow_irreversible_in_discovery", base.allow_irreversible_in_discovery)),
            allow_irreversible_in_replay=bool(
                d.get("allow_irreversible_in_replay", base.allow_irreversible_in_replay)),
            require_approval_for_irreversible=bool(
                d.get("require_approval_for_irreversible", base.require_approval_for_irreversible)),
            max_steps=int(d.get("max_steps", base.max_steps)),
        )

    @staticmethod
    def load(path: str) -> "Policy":
        with open(path, "r", encoding="utf-8") as fh:
            return Policy.from_dict(json.load(fh))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "allowed_origins": self.allowed_origins,
            "allowed_path_globs": self.allowed_path_globs,
            "denied_path_globs": self.denied_path_globs,
            "allowed_actions": self.allowed_actions,
            "allow_irreversible_in_discovery": self.allow_irreversible_in_discovery,
            "allow_irreversible_in_replay": self.allow_irreversible_in_replay,
            "require_approval_for_irreversible": self.require_approval_for_irreversible,
            "max_steps": self.max_steps,
        }

    # -- gates -------------------------------------------------------------
    def check_location(self, url: str) -> None:
        """Assert the automation is allowed to be at `url`.

        Called before navigating *and* after every action that could have
        navigated. `about:blank` is permitted because that is where a fresh
        browser context starts.
        """
        if not url or url == "about:blank":
            return
        parsed = urlparse(url)
        origin = "{}://{}".format(parsed.scheme, parsed.netloc)
        if self.allowed_origins and origin not in self.allowed_origins:
            raise PolicyViolation(
                "Navigation to origin {!r} is outside the allowlist {}".format(
                    origin, self.allowed_origins)
            )
        # Normalize before matching, or the deny list is trivially bypassable.
        # `fnmatch`'s `*` crosses `/`, so "/members/../_control/reset" misses
        # the deny glob "/_control/*" and *matches* the allow glob "/members/*"
        # -- while the browser resolves it to /_control/reset and executes it.
        # Percent-encoding hides the same trick (`%2e%2e`), so unquote first.
        path = posixpath.normpath(unquote(parsed.path or "/"))
        if parsed.path.endswith("/") and not path.endswith("/"):
            path += "/"
        if not path.startswith("/"):
            # normpath can produce a relative result from enough "..";
            # anything that escapes the root is refused outright.
            raise PolicyViolation(
                "Path {!r} escapes the site root after normalization".format(parsed.path))
        for deny in self.denied_path_globs:
            if fnmatch.fnmatch(path, deny):
                raise PolicyViolation(
                    "Path {!r} matches denied pattern {!r}".format(path, deny))
        if self.allowed_path_globs:
            if not any(fnmatch.fnmatch(path, g) for g in self.allowed_path_globs):
                raise PolicyViolation(
                    "Path {!r} does not match any allowed pattern {}".format(
                        path, self.allowed_path_globs)
                )

    def check_action(self, action: str) -> None:
        if action not in self.allowed_actions:
            raise PolicyViolation(
                "Action {!r} is not permitted by policy {!r} (allowed: {})".format(
                    action, self.name, self.allowed_actions)
            )

    def check_risk(
        self,
        risk: RiskLevel,
        mode: str,
        context: str = "",
        capability_approved: bool = False,
    ) -> None:
        """`mode` is "discovery" or "replay"."""
        if risk != RiskLevel.IRREVERSIBLE:
            return

        if mode == "discovery":
            if self.allow_irreversible_in_discovery:
                return
            raise ConfirmationRequired(
                "Irreversible action requires human confirmation during discovery.",
                risk=risk.value,
                context=context,
            )

        # replay
        if not self.allow_irreversible_in_replay:
            raise PolicyViolation(
                "Policy {!r} forbids irreversible actions during replay.".format(self.name))
        if self.require_approval_for_irreversible and not capability_approved:
            raise ConfirmationRequired(
                "Irreversible step in a capability that has not been approved "
                "for unattended replay (status is 'draft').",
                risk=risk.value,
                context=context,
            )


def default_policy(base_url: str) -> Policy:
    """Policy for the bundled sandbox target.

    Scoped to the one origin the demo runs on, with the control plane denied:
    `/_control/*` can reset data and inject faults, so it is exactly the kind
    of route an automation must never be able to reach even though it lives on
    an allowed host.
    """
    parsed = urlparse(base_url)
    origin = "{}://{}".format(parsed.scheme, parsed.netloc)
    return Policy(
        name="meridiancore-sandbox",
        allowed_origins=[origin],
        allowed_path_globs=["/", "/login", "/logout", "/nav", "/members/*", "/maintenance/*",
                            "/servicing", "/servicing/*"],
        denied_path_globs=["/_control/*"],
        allowed_actions=["navigate", "click", "fill", "select", "extract", "assert"],
        allow_irreversible_in_discovery=False,
        allow_irreversible_in_replay=True,
        require_approval_for_irreversible=True,
        max_steps=30,
    )
