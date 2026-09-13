"""
The surface abstraction -- the single most important boundary in this system.

Everything above this line (the agent loop, the recorder, the replay executor,
the escalation manager) is written against `Surface`, `Observation`, `Control`
and `Locator`. None of them import Playwright, know what a CSS selector is, or
assume there is a DOM. Everything below this line is surface-specific.

That seam is what makes the "extends to legacy web / native desktop" claim in
REPORT.md real rather than aspirational: a `WindowsUiaSurface` that produced
`Control(role="button", name="Open Sub-Account", hints={"automation_id": ...})`
from a UIA tree would drop straight in, and the recorded artifacts would not
change shape at all.

The vocabulary is deliberately the vocabulary of an *accessibility tree*
(role + accessible name), not of HTML, because that is the one representation
that exists on all three surfaces we care about: modern web, legacy web, and
native desktop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:  # pragma: no cover - import cycle guard only
    from ..artifact.schema import Locator

# Roles are normalized to this closed set across every surface implementation.
# A UIA `Edit` control and an HTML `<input type=text>` both become "textbox".
ROLES = (
    "button",
    "link",
    "textbox",
    "combobox",
    "checkbox",
    "radio",
    "menuitem",
    "tab",
)


@dataclass
class Control:
    """One interactive control as perceived on a surface.

    `ref` is a per-observation handle handed to the planner. It is
    intentionally ephemeral -- refs are never persisted into an artifact,
    because "the 4th control on the page" is exactly the kind of brittle
    identity we are trying to avoid recording.

    `hints` carries surface-specific addressing material (frame name, CSS
    path, form field name, containing-row text, UIA automation id...). The
    planner never sees it; only the recorder does, to synthesize durable
    locator strategies.
    """

    ref: int
    role: str
    name: str
    value: str = ""
    enabled: bool = True
    hints: Dict[str, str] = field(default_factory=dict)


@dataclass
class Observation:
    """A single perception of the surface at one moment."""

    surface_kind: str            # "web" | "desktop" | ...
    location: str                # URL for web; window path for desktop
    title: str
    controls: List[Control]
    text: str                    # visible text, normalized whitespace
    step_index: int = 0
    # Which frame/view/window is the working area. Matches `hints["frame"]` on
    # the controls that live there. Callers use it to disambiguate: a nav strip
    # and the work area routinely share vocabulary.
    working_context: str = ""

    def control(self, ref: int) -> Optional[Control]:
        for c in self.controls:
            if c.ref == ref:
                return c
        return None


@dataclass
class ResolvedTarget:
    """An opaque handle to a control that a surface has actually located.

    Callers may not introspect `handle` -- for the web surface it is a
    Playwright Locator, for a desktop surface it would be a UIA element. The
    only portable facts are which strategy found it and how it was described.
    """

    handle: Any
    strategy_index: int
    strategy_kind: str
    description: str = ""


class SurfaceError(Exception):
    """Raised by a surface when an operation cannot be performed at all.

    Distinct from the replay error taxonomy: this means the *mechanism*
    failed (element not found by any strategy, frame gone), not that the
    application returned an unwelcome answer.
    """


class Surface:
    """Interface every surface implementation satisfies.

    Kept deliberately small. Anything that can't be expressed in these eight
    operations doesn't belong in a recorded artifact either.
    """

    kind = "abstract"

    # -- perception --------------------------------------------------------
    def observe(self, step_index: int = 0) -> Observation:
        raise NotImplementedError

    def location(self) -> str:
        """Where the *working area* currently is.

        Not necessarily the top-level document. In a frameset the outer URL
        never changes while the user works, so a naive `page.url` would report
        a constant and every url-based checkpoint would be useless. Each
        surface decides what "the working area" means for it; for a desktop
        app it would be the active window/view path.
        """
        raise NotImplementedError

    def locations(self) -> List[str]:
        """Every location currently loaded, across all frames/views.

        The allowlist is enforced over this whole set, not just the working
        one -- a hidden frame pointed at an unapproved host is exactly the
        kind of thing a guardrail should catch.
        """
        raise NotImplementedError

    def text(self) -> str:
        raise NotImplementedError

    # -- addressing --------------------------------------------------------
    def resolve(self, locator: "Locator", params: Dict[str, str]) -> ResolvedTarget:
        """Find a control from a ranked list of strategies, trying each in
        order. Raises SurfaceError if every strategy misses."""
        raise NotImplementedError

    # -- action ------------------------------------------------------------
    def click(self, target: ResolvedTarget) -> None:
        raise NotImplementedError

    def fill(self, target: ResolvedTarget, value: str) -> None:
        raise NotImplementedError

    def select(self, target: ResolvedTarget, value: str) -> None:
        raise NotImplementedError

    def navigate(self, location: str) -> None:
        raise NotImplementedError

    def wait_settled(self, timeout_ms: int = 5000) -> None:
        """Block until the surface is quiescent. Web: load state. Desktop:
        window responsive / no busy cursor."""
        raise NotImplementedError

    # -- extraction --------------------------------------------------------
    def read_labelled_value(self, label: str) -> Optional[str]:
        """Read the value displayed next to a given label.

        On legacy web this means "the cell to the right of the cell whose
        text is `label`" -- the dominant way these apps present read-only
        data, since there is no semantic markup tying label to value. On a
        desktop surface this is the sibling Text element of a Label. Modelling
        it as one portable operation keeps extraction out of the artifact's
        surface-specific details.
        """
        raise NotImplementedError

    def read_text_of(self, target: ResolvedTarget) -> str:
        raise NotImplementedError

    # -- evidence ----------------------------------------------------------
    def screenshot(self, path: str) -> Optional[str]:
        raise NotImplementedError

    def structure_snapshot(self, path: str) -> Optional[str]:
        """A richer, surface-native dump for debugging: DOM for web, UIA tree
        for desktop."""
        raise NotImplementedError
