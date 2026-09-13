"""
Playwright-backed implementation of `Surface` for web applications.

This is the only module in the system that knows what a CSS selector, a
frame, or a browser is.

Two things here are doing more work than they look like they are:

* **Frame flattening.** `observe()` walks every frame in the page and returns
  one flat control list, tagging each control with the frame it came from.
  The agent loop and the replay executor never learn that framesets exist.
  This is what lets the same code drive a modern SPA and a 2003-era frameset.

* **Ranked locator resolution.** `resolve()` walks a capability's ordered
  strategy list and reports which one actually worked. That index is the
  system's drift signal: a capability that has quietly started resolving via
  its third-choice strategy is one app upgrade away from breaking, and we
  want to know before it does.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Dict, List, Optional

from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout

from .base import Control, Observation, ResolvedTarget, Surface, SurfaceError

_PROBE_PATH = os.path.join(os.path.dirname(__file__), "web_probe.js")
with open(_PROBE_PATH, "r", encoding="utf-8") as _f:
    _PROBE_JS = _f.read()

# Short, because a miss on strategy 1 should fall through to strategy 2 fast
# rather than spending Playwright's 30s default on every candidate.
_RESOLVE_TIMEOUT_MS = 1500


class WebSurface(Surface):
    kind = "web"

    def __init__(self, page, evidence_dir: Optional[str] = None):
        self._page = page
        self._evidence_dir = evidence_dir
        # Navigation counter. A click in a frameset app navigates a *child*
        # frame, so `page.wait_for_load_state()` on the main frame returns
        # immediately and we would observe the pre-click screen -- recording a
        # checkpoint for a page we never actually saw. Counting frame
        # navigations lets us wait for the real thing instead of sleeping.
        self._nav_count = 0
        self._nav_requests = 0
        page.on("framenavigated", self._on_navigated)
        page.on("request", self._on_request)

    def _on_navigated(self, _frame) -> None:
        self._nav_count += 1

    def _on_request(self, request) -> None:
        # `framenavigated` only fires once a navigation *commits*, which is
        # after the server has responded. On a slow back end that can be
        # several seconds, far longer than it is reasonable to block a
        # non-navigating click for. Counting navigation *requests* tells us
        # immediately that something is in flight, so we can wait patiently
        # when there is a reason to and return promptly when there isn't.
        try:
            if request.is_navigation_request():
                self._nav_requests += 1
        except PWError:
            pass

    # -- perception --------------------------------------------------------
    def observe(self, step_index: int = 0) -> Observation:
        controls: List[Control] = []
        texts: List[str] = []
        title = ""
        ref = 0
        working = self._working_frame()

        for frame in self._page.frames:
            frame_key = self._frame_key(frame)
            try:
                data = frame.evaluate(_PROBE_JS)
            except PWError:
                # A frame can detach mid-walk (navigation in progress). Skip
                # it rather than failing the whole observation.
                continue
            if not data:
                continue
            # The *working* frame's title, not the top document's. A frameset's
            # own <title> is set once and never changes as the operator moves
            # between screens, so using it would make every title-based
            # checkpoint trivially true.
            if frame is working and data.get("title"):
                title = data["title"]
            if data.get("text"):
                texts.append(data["text"])
            for c in data.get("controls", []):
                if not c.get("name"):
                    continue
                controls.append(
                    Control(
                        ref=ref,
                        role=c["role"],
                        name=c["name"],
                        value=c.get("value", ""),
                        enabled=bool(c.get("enabled", True)),
                        hints={
                            "frame": frame_key,
                            "name_source": c.get("name_source", ""),
                            "tag": c.get("tag", ""),
                            "type": c.get("type", ""),
                            "name_attr": c.get("name_attr", ""),
                            "href": c.get("href", ""),
                            "css": c.get("css", ""),
                            "row_anchor": c.get("row_anchor", ""),
                        },
                    )
                )
                ref += 1

        return Observation(
            surface_kind=self.kind,
            location=self.location(),
            title=title or self._safe_title(),
            controls=controls,
            text=" ".join(texts),
            step_index=step_index,
            working_context=self._frame_key(working),
        )

    def _working_frame(self):
        """The frame the operator is actually working in.

        In a frameset the top-level document is the frameset itself: its URL
        and title are set once and never change, so treating it as "the page"
        makes every url- and title-based checkpoint trivially true. The working
        area is taken to be the largest child frame by rendered area -- a
        property of how these consoles are laid out (a nav strip on top, the
        work area filling the rest) rather than of any one application. With no
        child frames this is just the main frame, so single-document apps are
        unaffected.
        """
        best, best_area = None, 0.0
        for frame in self._page.frames:
            if frame is self._page.main_frame:
                continue
            try:
                box = frame.frame_element().bounding_box()
            except PWError:
                continue
            if not box:
                continue
            area = float(box.get("width", 0)) * float(box.get("height", 0))
            if area > best_area:
                best_area, best = area, frame
        return best or self._page.main_frame

    def location(self) -> str:
        return self._working_frame().url or self._page.url

    def locations(self) -> List[str]:
        return [f.url for f in self._page.frames if f.url]

    def text(self) -> str:
        chunks = []
        for frame in self._page.frames:
            try:
                t = frame.evaluate("() => document.body ? document.body.innerText : ''")
            except PWError:
                continue
            if t:
                chunks.append(" ".join(t.split()))
        return " ".join(chunks)

    # -- addressing --------------------------------------------------------
    def resolve(self, locator, params: Dict[str, str]) -> ResolvedTarget:
        attempted: List[str] = []
        for idx, strat in enumerate(locator.strategies):
            kind = strat.get("kind", "")
            try:
                candidate = self._build(strat, params)
            except (PWError, ValueError, KeyError) as exc:
                # KeyError matters: a hand-edited artifact or a tenant `steps`
                # patch can omit `role`/`name`/`value`. Left uncaught it escapes
                # as a bare KeyError rather than a SurfaceError, so the executor
                # never converts it to `locator_unresolved` -- and crucially
                # skips the business-outcome and recovery reclassification.
                attempted.append(f"{idx}:{kind} (malformed strategy: {exc})")
                continue
            if candidate is None:
                attempted.append(f"{idx}:{kind} (frame not found)")
                continue
            try:
                candidate.wait_for(state="visible", timeout=_RESOLVE_TIMEOUT_MS)
            except (PWTimeout, PWError):
                attempted.append(f"{idx}:{kind} (no visible match)")
                continue
            return ResolvedTarget(
                handle=candidate,
                strategy_index=idx,
                strategy_kind=kind,
                description=locator.description,
            )
        raise SurfaceError(
            "No locator strategy resolved for {!r}. Tried: {}".format(
                locator.description or "<control>", "; ".join(attempted) or "<none>"
            )
        )

    def _build(self, strat: Dict[str, str], params: Dict[str, str]):
        """Turn one strategy dict into a Playwright locator, or None if the
        strategy names a frame that isn't present."""
        frame = self._frame_for(strat.get("frame", ""))
        if frame is None:
            return None
        sub = lambda s: _substitute(s, params)  # noqa: E731
        kind = strat.get("kind")

        if kind == "role_name":
            return frame.get_by_role(
                strat["role"], name=sub(strat["name"]), exact=False
            ).first

        if kind == "row_role_name":
            # "the <role> named <name> inside the row containing <row>".
            # The row anchor is usually parameterized, which is what makes a
            # results-grid capability work for any record, not just the one
            # it was recorded against.
            rows = frame.locator("tr").filter(has_text=sub(strat["row"]))
            return rows.get_by_role(strat["role"], name=sub(strat["name"]), exact=False).first

        if kind == "attr":
            attr, value = strat["attr"], sub(strat["value"])
            tag = strat.get("tag", "")
            return frame.locator('{}[{}="{}"]'.format(tag, attr, value)).first

        if kind == "css":
            return frame.locator(sub(strat["value"])).first

        if kind == "text":
            # Scoped to *interactive* elements on purpose. An unscoped text
            # match will happily resolve to inert prose -- when an app hides a
            # button it is not entitled to show, the surrounding sentence often
            # still contains the button's caption ("Open Sub-Account is
            # unavailable for this record"). Clicking that silently does
            # nothing and the run fails several steps later with a misleading
            # error. Requiring the match to be clickable turns that into an
            # honest "no strategy resolved" at the right step.
            clickable = ("a, button, input[type=submit], input[type=button], "
                         "input[type=reset], [role=button], [role=link]")
            return frame.locator(clickable).filter(has_text=sub(strat["value"])).first

        raise ValueError("Unknown locator strategy kind: {!r}".format(kind))

    # -- action ------------------------------------------------------------
    def click(self, target: ResolvedTarget) -> None:
        nav_before, req_before = self._nav_count, self._nav_requests
        target.handle.click(timeout=5000)
        self._await_navigation(nav_before, req_before)

    def _await_navigation(self, nav_before: int, req_before: int,
                          start_window_ms: int = 600,
                          load_timeout_ms: int = 15000) -> None:
        """Wait for a click's navigation to actually land.

        Three outcomes, all handled explicitly rather than by sleeping:

          * a navigation request was issued -> wait for it to commit and load,
            with a generous timeout, because the wait is now justified: we
            know something is in flight;
          * a frame navigated without us seeing the request -> wait for load;
          * nothing at all within a short window -> it was a non-navigating
            click, return immediately.

        Splitting "is anything happening?" from "how long am I willing to wait
        for it?" is what lets this be both fast on a click that does nothing
        and patient with a back end that takes five seconds to answer. Waiting
        a flat interval would have to be either too short (and read the
        previous screen) or too slow on every single action.

        Getting this wrong is subtle and expensive: during discovery it records
        a checkpoint describing a page the step never reached, and during
        replay it reports a checkpoint failure for a step that actually worked.
        """
        deadline = time.time() + start_window_ms / 1000.0
        while (self._nav_count == nav_before and self._nav_requests == req_before
               and time.time() < deadline):
            self._page.wait_for_timeout(30)

        if self._nav_count == nav_before and self._nav_requests == req_before:
            return  # nothing navigated; a genuinely local click

        if self._nav_count == nav_before:
            # Requested but not yet committed -- the server is still thinking.
            commit_deadline = time.time() + load_timeout_ms / 1000.0
            while self._nav_count == nav_before and time.time() < commit_deadline:
                self._page.wait_for_timeout(50)

        try:
            self._page.wait_for_load_state("load", timeout=load_timeout_ms)
        except (PWTimeout, PWError):
            pass
        # Child frames of a frameset finish after the top document does.
        for frame in list(self._page.frames):
            try:
                frame.wait_for_load_state("load", timeout=5000)
            except (PWTimeout, PWError):
                continue

    def fill(self, target: ResolvedTarget, value: str) -> None:
        target.handle.fill(value, timeout=5000)

    def select(self, target: ResolvedTarget, value: str) -> None:
        # Awaits navigation for the same reason `click` does: a legacy
        # `<select onchange="form.submit()">` navigates, and without this the
        # next observation reads the pre-navigation screen.
        nav_before, req_before = self._nav_count, self._nav_requests
        try:
            target.handle.select_option(label=value, timeout=5000)
        except PWError:
            target.handle.select_option(value=value, timeout=5000)
        self._await_navigation(nav_before, req_before)

    def navigate(self, location: str) -> None:
        self._page.goto(location, wait_until="load")

    def wait_settled(self, timeout_ms: int = 5000) -> None:
        try:
            self._page.wait_for_load_state("load", timeout=timeout_ms)
        except PWTimeout:
            raise
        except PWError:
            pass

    # -- extraction --------------------------------------------------------
    def read_labelled_value(self, label: str) -> Optional[str]:
        """Find the cell whose text is `label` and return the next cell's text.

        This is how these applications present every read-only field. Doing it
        in one portable operation means capability artifacts declare
        `{"method": "labelled_value", "label": "New Account Number"}` rather
        than embedding a table XPath that breaks the moment a column moves.
        """
        js = """
        (label) => {
          const norm = (s) => (s || "").replace(/\\s+/g, " ").trim();
          const want = norm(label).toLowerCase().replace(/:$/, "");
          const cells = document.querySelectorAll("td, th");
          for (let i = 0; i < cells.length; i++) {
            const c = cells[i];
            if (norm(c.textContent).toLowerCase().replace(/:$/, "") !== want) continue;
            const next = c.nextElementSibling;
            if (next && norm(next.textContent)) return norm(next.textContent);
          }
          return null;
        }
        """
        for frame in self._page.frames:
            try:
                found = frame.evaluate(js, label)
            except PWError:
                continue
            if found:
                return found
        return None

    def read_text_of(self, target: ResolvedTarget) -> str:
        return " ".join((target.handle.inner_text(timeout=5000) or "").split())

    # -- evidence ----------------------------------------------------------
    def screenshot(self, path: str) -> Optional[str]:
        try:
            self._page.screenshot(path=path, full_page=True)
            return path
        except PWError:
            return None

    def structure_snapshot(self, path: str) -> Optional[str]:
        """Dump the flattened control tree of every frame.

        Deliberately *not* raw HTML: raw HTML from a servicing screen is full
        of member names, balances and tax IDs. The control tree is the part
        that is actually useful for debugging a locator failure, and it is
        still routed through redaction before it hits disk.
        """
        frames = []
        for frame in self._page.frames:
            try:
                data = frame.evaluate(_PROBE_JS)
            except PWError:
                continue
            frames.append({
                "frame": self._frame_key(frame),
                "url": frame.url,
                "controls": (data or {}).get("controls", []),
            })
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"url": self._page.url, "frames": frames}, fh, indent=2)
            return path
        except OSError:
            return None

    # -- frame plumbing ----------------------------------------------------
    def _frame_key(self, frame) -> str:
        """A stable identifier for a frame.

        Frame *name* is what framesets give us and it is stable across
        navigations, so prefer it. Falling back to the URL path keeps
        unnamed iframes addressable, at the cost of being route-sensitive.
        """
        if frame is self._page.main_frame:
            return ""
        if frame.name:
            return "name={}".format(frame.name)
        return "urlpath={}".format(re.sub(r"^https?://[^/]+", "", frame.url or ""))

    def _frame_for(self, key: str):
        if not key:
            return self._page.main_frame
        for frame in self._page.frames:
            if self._frame_key(frame) == key:
                return frame
        # A named frame that has navigated may briefly report a different key;
        # match on bare name as a second pass.
        if key.startswith("name="):
            want = key[5:]
            for frame in self._page.frames:
                if frame.name == want:
                    return frame
        # The recorded frame is gone entirely. This happens legitimately -- an
        # operator taking over during an intervention may navigate straight to
        # a deep link, leaving the frameset behind. Degrade to the working
        # frame rather than failing every strategy: the frame is a hint about
        # where the control lived, not part of its identity.
        return self._working_frame()

    def _safe_title(self) -> str:
        try:
            return self._page.title()
        except PWError:
            return ""


def _substitute(value: str, params: Dict[str, str]) -> str:
    """Fill {param} placeholders in a locator strategy value.

    Parameterized locators are what let one recorded artifact address any
    record: the row anchor for "member 100237" is stored as "{member_id}".
    """
    if not isinstance(value, str):
        return value
    out = value
    for key, val in (params or {}).items():
        out = out.replace("{" + key + "}", str(val))
    return out
