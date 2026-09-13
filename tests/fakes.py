"""A scripted in-memory Surface.

Lets the replay executor's classification logic -- which is where the
interesting correctness lives -- be tested exhaustively and in milliseconds,
without a browser or the target app. Each "screen" is a location, some visible
text, and a set of control names; acting on a control moves to another screen.

This is also the cheapest possible demonstration that the surface abstraction
holds: the executor runs unmodified against something that is not a browser at
all and has no DOM.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from cua.surfaces.base import Control, Observation, ResolvedTarget, Surface, SurfaceError


class Screen:
    def __init__(self, location: str, text: str = "", controls: Optional[List[str]] = None,
                 title: str = "", labelled: Optional[Dict[str, str]] = None):
        self.location = location
        self.text = text
        self.controls = controls or []
        self.title = title
        self.labelled = labelled or {}


class FakeSurface(Surface):
    kind = "fake"

    def __init__(self, screens: Dict[str, Screen], start: str,
                 transitions: Optional[Dict[Any, str]] = None,
                 transitions_once: Optional[Dict[Any, str]] = None):
        self.screens = screens
        self.current = start
        # (screen_key, control_name) -> next screen key
        self.transitions = transitions or {}
        # Same, but consumed on first use. Models a one-shot runtime condition
        # -- a session that expires once, a back end that errors once -- which
        # is what makes recovery testable: without it, a rule that recovers
        # correctly still loops forever because the fault never clears.
        self.transitions_once = dict(transitions_once or {})
        self.actions: List[Any] = []
        self.unresolvable: set = set()
        self.timeout_on: set = set()
        self.screenshots: List[str] = []

    # -- perception --------------------------------------------------------
    @property
    def screen(self) -> Screen:
        return self.screens[self.current]

    def observe(self, step_index: int = 0) -> Observation:
        return Observation(
            surface_kind=self.kind, location=self.screen.location, title=self.screen.title,
            controls=[Control(ref=i, role="button", name=n)
                      for i, n in enumerate(self.screen.controls)],
            text=self.screen.text, step_index=step_index,
        )

    def location(self) -> str:
        return self.screen.location

    def locations(self) -> List[str]:
        return [self.screen.location]

    def text(self) -> str:
        return self.screen.text

    # -- addressing --------------------------------------------------------
    def resolve(self, locator, params: Dict[str, str]) -> ResolvedTarget:
        for idx, strat in enumerate(locator.strategies):
            name = _fill(strat.get("name") or strat.get("value") or "", params)
            if name in self.unresolvable:
                continue
            if name in self.screen.controls:
                return ResolvedTarget(handle=name, strategy_index=idx,
                                      strategy_kind=strat.get("kind", ""),
                                      description=locator.description)
        raise SurfaceError("No locator strategy resolved for {!r}".format(locator.description))

    # -- action ------------------------------------------------------------
    def _perform(self, name: str) -> None:
        if name in self.timeout_on:
            self.timeout_on.discard(name)  # transient: succeeds on retry
            raise FakeTimeout("timeout acting on {}".format(name))
        self.actions.append(name)
        key = (self.current, name)
        if key in self.transitions_once:
            self.current = self.transitions_once.pop(key)
            return
        nxt = self.transitions.get(key)
        if nxt:
            self.current = nxt

    def click(self, target: ResolvedTarget) -> None:
        self._perform(target.handle)

    def fill(self, target: ResolvedTarget, value: str) -> None:
        self._perform(target.handle)

    def select(self, target: ResolvedTarget, value: str) -> None:
        self._perform(target.handle)

    def navigate(self, location: str) -> None:
        for key, screen in self.screens.items():
            if screen.location == location or location.endswith(screen.location):
                self.current = key
                return
        self.actions.append("navigate:" + location)

    def wait_settled(self, timeout_ms: int = 5000) -> None:
        return None

    # -- extraction --------------------------------------------------------
    def read_labelled_value(self, label: str) -> Optional[str]:
        return self.screen.labelled.get(label)

    def read_text_of(self, target: ResolvedTarget) -> str:
        return str(target.handle)

    # -- evidence ----------------------------------------------------------
    def screenshot(self, path: str) -> Optional[str]:
        self.screenshots.append(path)
        return path

    def structure_snapshot(self, path: str) -> Optional[str]:
        return None


class FakeTimeout(Exception):
    """Named so the executor's timeout sniffing recognises it."""


def _fill(value: str, params: Dict[str, str]) -> str:
    out = value or ""
    for key, val in (params or {}).items():
        out = out.replace("{" + key + "}", str(val))
    return out


class NullRecorder:
    """Evidence recorder that keeps events in memory."""

    def __init__(self) -> None:
        self.run_id = "test-run"
        self.dir = "/tmp/test-run"
        self.events: List[Dict[str, Any]] = []

    def log(self, event: str, **fields: Any) -> Dict[str, Any]:
        record = dict(event=event, **fields)
        self.events.append(record)
        return record

    def capture(self, surface, label: str) -> Dict[str, Optional[str]]:
        self.log("evidence_captured", label=label)
        return {"screenshot": None, "controls": None}

    def write_json(self, name: str, payload: Any) -> str:
        return name

    def kinds(self) -> List[str]:
        return [e["event"] for e in self.events]
