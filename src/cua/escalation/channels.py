"""
Operator channels -- the three ways a human can pick up an intervention.

All three end up calling the same `OperatorExecutor` on the same thread that
owns the surface, so they are interchangeable and none of them is a
second-class path.

  `ConsoleOperatorChannel`  Terminal REPL. Works headless, over SSH, in CI.
  `TakeoverOperatorChannel` Hands the actual browser window to the person
                            sitting in front of it. The most "real" handoff
                            available without building co-browsing.
  `WebOperatorChannel`      A minimal remote operator console: live screenshot
                            of the running session plus a command form. This
                            is the one that models how a real deployment would
                            work, where the operator is not on the machine
                            running the automation.
  `ScriptedOperatorChannel` Replays a recorded command list. Used for tests and
                            to regenerate evidence reproducibly.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from typing import Any, Dict, List, Optional

from .manager import InterventionOutcome, InterventionRequest, OperatorChannel, OperatorExecutor

_HELP = """
Operator console. You now hold the session lease -- you are driving the same
live browser session the automation was using.

  ls                      list the interactive controls visible right now
  where                   print the current location
  read                    print the visible text
  click <name>            click the control whose name contains <name>
  fill <name> = <value>   type <value> into the field named <name>
  select <name> = <value> choose <value> in the dropdown named <name>
  goto <url>              navigate (still subject to the allowlist)
  shot                    save a screenshot into the evidence directory
  resume                  hand control back; automation continues
  done                    hand control back; the work is finished
  abort                   give up; the run fails
  help                    this text
"""


def _parse(line: str):
    """`fill Operator ID = svc_admin` -> ("fill", "Operator ID", "svc_admin")"""
    line = line.strip()
    if not line:
        return None, "", ""
    verb, _, rest = line.partition(" ")
    verb = verb.strip().lower()
    if "=" in rest:
        name, _, value = rest.partition("=")
        return verb, name.strip(), value.strip()
    return verb, rest.strip(), ""


class ConsoleOperatorChannel(OperatorChannel):
    name = "console"

    def __init__(self, evidence_dir: str = "."):
        self.evidence_dir = evidence_dir

    def handle(self, request: InterventionRequest, executor: OperatorExecutor) -> InterventionOutcome:
        print("\n" + "=" * 72)
        print("HUMAN INTERVENTION REQUESTED")
        print("=" * 72)
        print(request.summary())
        print(_HELP)

        shots = 0
        while True:
            try:
                line = input("operator> ")
            except EOFError:
                return InterventionOutcome(resolution="aborted", notes="operator disconnected")
            verb, name, value = _parse(line)
            if verb is None:
                continue
            if verb == "help":
                print(_HELP)
            elif verb == "ls":
                for c in executor.controls():
                    print("  [{ref}] {role:<9} {name}".format(**c))
            elif verb == "where":
                print("  " + executor.location())
            elif verb == "read":
                print("  " + executor.text()[:1500])
            elif verb == "shot":
                shots += 1
                path = os.path.join(self.evidence_dir, "operator_shot_{}.png".format(shots))
                print("  saved " + str(executor.screenshot(path)))
            elif verb in ("click", "fill", "select", "goto"):
                if verb == "goto":
                    action = executor.execute("goto", url=name)
                elif verb == "click":
                    action = executor.execute("click", name=name)
                else:
                    action = executor.execute(verb, name=name, value=value)
                print("  {} {}".format("ok  " if action.ok else "FAIL", action.detail))
            elif verb == "resume":
                return InterventionOutcome(resolution="resumed", notes="operator handed control back")
            elif verb == "done":
                return InterventionOutcome(resolution="completed_by_operator",
                                           notes="operator completed the work manually")
            elif verb == "abort":
                return InterventionOutcome(resolution="aborted", notes="operator aborted the run")
            else:
                print("  unknown command; type `help`")


class TakeoverOperatorChannel(OperatorChannel):
    """Hand the literal browser window to the person at the keyboard.

    No mediation at all: the automation stops touching the page, the human
    clicks around in the real Chromium window, and presses Enter when done.
    On hand-back the system re-observes the surface to see where the human
    left it, and records the before/after location as the operator's action.

    This only makes sense with `--headed`, and it is the thinnest possible
    real control transfer -- which is exactly why it is worth having
    alongside the richer channels.
    """

    name = "takeover"

    def handle(self, request: InterventionRequest, executor: OperatorExecutor) -> InterventionOutcome:
        before = executor.location()
        print("\n" + "=" * 72)
        print("HUMAN INTERVENTION REQUESTED -- BROWSER HANDED OVER")
        print("=" * 72)
        print(request.summary())
        print("\nThe automation has stopped. Use the open browser window directly.")
        print("When you are finished, come back here and press Enter to hand control back.")
        print("(Type `done` then Enter if you completed the whole task, `abort` to give up.)")
        try:
            answer = input("\npress Enter to resume > ").strip().lower()
        except EOFError:
            answer = "abort"

        after = executor.location()
        executor.actions.append(
            _manual_action(before, after)
        )
        if answer == "abort":
            return InterventionOutcome(resolution="aborted", notes="operator aborted after takeover")
        if answer == "done":
            return InterventionOutcome(resolution="completed_by_operator",
                                       notes="operator completed the work in the browser window")
        return InterventionOutcome(resolution="resumed",
                                   notes="operator worked directly in the browser and handed back")


class ScriptedOperatorChannel(OperatorChannel):
    """Replays a recorded operator session.

    Exists so the escalation path is testable and so the checked-in evidence
    can be regenerated byte-for-similar rather than depending on somebody
    typing the same commands again.

    Script format (JSON): {"commands": ["click Sign In", "fill Operator ID = x"],
                           "resolution": "resumed"}
    """

    name = "scripted"

    def __init__(self, script: Dict[str, Any]):
        self.script = script

    @staticmethod
    def from_file(path: str) -> "ScriptedOperatorChannel":
        with open(path, "r", encoding="utf-8") as fh:
            return ScriptedOperatorChannel(json.load(fh))

    def handle(self, request: InterventionRequest, executor: OperatorExecutor) -> InterventionOutcome:
        print("\n[escalation] intervention {} handled by scripted operator".format(request.id))
        print(request.summary())
        for line in self.script.get("commands", []):
            verb, name, value = _parse(line)
            if verb in ("click",):
                action = executor.execute("click", name=name)
            elif verb in ("fill", "select"):
                action = executor.execute(verb, name=name, value=value)
            elif verb == "goto":
                action = executor.execute("goto", url=name)
            else:
                continue
            print("  operator: {:<40} {}".format(line, "ok" if action.ok else "FAILED: " + action.detail))
        return InterventionOutcome(
            resolution=self.script.get("resolution", "resumed"),
            notes=self.script.get("notes", "scripted operator session"),
        )


# --------------------------------------------------------------------------
# Remote web console
# --------------------------------------------------------------------------
class _Bridge:
    """Marshals commands from the HTTP thread onto the surface-owning thread.

    This exists because of the constraint described in `lease.py`: the browser
    session can only be driven from one thread. The Flask request handler does
    not touch the surface. It puts a command on a queue and blocks on an
    event; the owning thread drains the queue, executes, and sets the event.

    Everything an operator sees is produced by the owning thread too --
    including the screenshot, which it refreshes on a timer while idling.
    """

    def __init__(self) -> None:
        self.inbox: "queue.Queue" = queue.Queue()
        self.decision: Optional[str] = None
        self.transcript: List[Dict[str, Any]] = []
        self.shot_path: Optional[str] = None
        self.shot_version = 0
        self.controls: List[Dict[str, str]] = []
        self.location = ""

    def submit(self, verb: str, **args: Any) -> Dict[str, Any]:
        holder: Dict[str, Any] = {}
        done = threading.Event()
        self.inbox.put((verb, args, holder, done))
        done.wait(timeout=30)
        return holder.get("result", {"ok": False, "detail": "timed out waiting for the session thread"})


class WebOperatorChannel(OperatorChannel):
    """A minimal remote operator console.

    Not a co-browsing product -- it is a screenshot refreshed on a timer plus
    a command form. But the two properties that matter are real: the operator
    is looking at and acting on the *actual live session* (same cookies, same
    half-filled form), and control genuinely transfers and comes back.

    What a production version would change is the transport (WebRTC/CDP screen
    streaming with real input forwarding instead of poll-a-PNG), not the
    control-transfer model. See REPORT.md "Escalation & handoff".
    """

    name = "web"

    def __init__(self, evidence_dir: str, port: int = 5099, open_browser: bool = False):
        self.evidence_dir = evidence_dir
        self.port = port
        self.open_browser = open_browser

    def handle(self, request: InterventionRequest, executor: OperatorExecutor) -> InterventionOutcome:
        bridge = _Bridge()
        bridge.location = executor.location()
        bridge.controls = executor.controls()
        bridge.shot_path = os.path.join(self.evidence_dir, "operator_live.png")
        executor.screenshot(bridge.shot_path)
        bridge.shot_version = 1

        server = _start_console_server(request, bridge, self.port)
        url = "http://127.0.0.1:{}/".format(self.port)
        print("\n" + "=" * 72)
        print("HUMAN INTERVENTION REQUESTED")
        print("=" * 72)
        print(request.summary())
        print("\n  Operator console: {}".format(url))
        print("  The automation is paused and holds the page open for you.\n")
        if self.open_browser:
            import webbrowser
            webbrowser.open(url)

        last_refresh = time.time()
        try:
            while bridge.decision is None:
                try:
                    verb, args, holder, done = bridge.inbox.get(timeout=0.4)
                except queue.Empty:
                    if time.time() - last_refresh > 1.5:
                        self._refresh(executor, bridge)
                        last_refresh = time.time()
                    continue
                action = executor.execute(verb, **args)
                holder["result"] = {"ok": action.ok, "detail": action.detail}
                bridge.transcript.append(
                    {"verb": verb, "args": args, "ok": action.ok, "detail": action.detail}
                )
                done.set()
                self._refresh(executor, bridge)
                last_refresh = time.time()
        finally:
            server.shutdown()

        resolution = bridge.decision or "aborted"
        return InterventionOutcome(
            resolution=resolution,
            notes="handled via the web operator console",
        )

    def _refresh(self, executor: OperatorExecutor, bridge: _Bridge) -> None:
        try:
            executor.screenshot(bridge.shot_path)
            bridge.shot_version += 1
            bridge.location = executor.location()
            bridge.controls = executor.controls()
        except Exception:
            pass


_CONSOLE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Operator Console &mdash; {req_id}</title>
<style>
 body {{ font: 13px -apple-system, Segoe UI, sans-serif; margin: 0; background: #11161d; color: #dde3ea; }}
 header {{ background: #7a1f1f; padding: 10px 16px; }}
 header b {{ font-size: 15px; }}
 .wrap {{ display: flex; gap: 16px; padding: 16px; align-items: flex-start; }}
 .left {{ flex: 1 1 60%; }} .right {{ flex: 1 1 40%; }}
 img {{ width: 100%; border: 1px solid #33404f; background: #fff; }}
 .card {{ background: #1a212b; border: 1px solid #2c3744; padding: 12px; margin-bottom: 12px; }}
 .k {{ color: #8fa3bb; display: inline-block; min-width: 84px; }}
 input[type=text] {{ width: 100%; padding: 7px; background: #0d1219; color: #dde3ea;
                     border: 1px solid #33404f; font: 13px monospace; box-sizing: border-box; }}
 button {{ padding: 7px 14px; border: 0; cursor: pointer; font-weight: 600; }}
 .go {{ background: #2f6f4f; color: #fff; }}
 .resume {{ background: #2b5f9e; color: #fff; }}
 .done {{ background: #4a5f2b; color: #fff; }}
 .abort {{ background: #7a1f1f; color: #fff; }}
 code {{ color: #9fd0a0; }} .bad {{ color: #ff8f8f; }}
 ul {{ margin: 4px 0; padding-left: 18px; max-height: 220px; overflow: auto; }}
 li {{ margin: 2px 0; }}
</style></head>
<body>
<header><b>Operator intervention required</b> &nbsp; <code>{req_id}</code> &nbsp; {trigger}</header>
<div class="wrap">
  <div class="left">
    <div class="card">
      <div><span class="k">live session</span> <code>{location}</code></div>
      <img src="/shot.png?v={shot_version}" alt="live session">
      <div style="color:#8fa3bb;margin-top:6px">Refreshes automatically. This is the
        same session the automation was driving.</div>
    </div>
  </div>
  <div class="right">
    <div class="card">
      <div><span class="k">reason</span> {reason}</div>
      <div><span class="k">mode</span> {mode}</div>
      <div><span class="k">capability</span> {capability_id} / step <code>{step_id}</code></div>
      <div><span class="k">expected</span> {expected}</div>
      <div><span class="k">observed</span> {observed}</div>
    </div>
    <div class="card">
      <form method="post" action="/cmd">
        <input type="text" name="line" autofocus autocomplete="off"
               placeholder="click Sign In  |  fill Operator ID = svc_admin  |  goto http://...">
        <div style="margin-top:8px"><button class="go" type="submit">Run</button></div>
      </form>
      <div style="color:#8fa3bb;margin-top:8px">
        <code>click &lt;name&gt;</code> &middot;
        <code>fill &lt;name&gt; = &lt;value&gt;</code> &middot;
        <code>select &lt;name&gt; = &lt;value&gt;</code> &middot;
        <code>goto &lt;url&gt;</code>
      </div>
    </div>
    <div class="card">
      <b>Controls on screen</b>
      <ul>{controls}</ul>
    </div>
    <div class="card">
      <b>What you did</b>
      <ul>{transcript}</ul>
    </div>
    <div class="card">
      <form method="post" action="/finish" style="display:flex;gap:8px">
        <button class="resume" name="resolution" value="resumed" type="submit">Hand back &amp; resume</button>
        <button class="done" name="resolution" value="completed_by_operator" type="submit">I finished it</button>
        <button class="abort" name="resolution" value="aborted" type="submit">Abort run</button>
      </form>
    </div>
  </div>
</div>
<script>
 // Only poll for the screenshot; never reload while the operator is typing.
 setInterval(function () {{
   var img = document.querySelector('img');
   if (img) img.src = '/shot.png?v=' + Date.now();
 }}, 1500);
</script>
</body></html>"""


def _start_console_server(request: InterventionRequest, bridge: _Bridge, port: int):
    """Serve the console on a daemon thread.

    Flask is already a dependency (the target app uses it) so this costs
    nothing extra. `werkzeug`'s server object is kept so the main thread can
    shut it down cleanly the moment the operator hands control back.
    """
    from flask import Flask, Response, redirect, request as flask_request
    from werkzeug.serving import make_server

    app = Flask("cua-operator-console")
    app.logger.disabled = True

    def render() -> str:
        controls = "".join(
            "<li><code>{}</code> {}</li>".format(c["role"], _esc(c["name"]))
            for c in bridge.controls[:40]
        ) or "<li>(none visible)</li>"
        transcript = "".join(
            '<li>{} <code>{}</code> {}</li>'.format(
                "" if t["ok"] else '<span class="bad">FAILED</span>',
                _esc("{} {}".format(t["verb"], t["args"])),
                _esc(t["detail"]),
            )
            for t in bridge.transcript
        ) or "<li>(nothing yet)</li>"
        return _CONSOLE_HTML.format(
            req_id=_esc(request.id),
            trigger=_esc(request.trigger),
            reason=_esc(request.reason),
            mode=_esc(request.mode),
            capability_id=_esc(request.capability_id or "-"),
            step_id=_esc(request.step_id or "-"),
            expected=_esc(request.expected or "-"),
            observed=_esc(request.observed or "-"),
            location=_esc(bridge.location),
            shot_version=bridge.shot_version,
            controls=controls,
            transcript=transcript,
        )

    @app.get("/")
    def index():
        return render()

    @app.get("/shot.png")
    def shot():
        try:
            with open(bridge.shot_path, "rb") as fh:
                return Response(fh.read(), mimetype="image/png")
        except OSError:
            return Response(b"", mimetype="image/png")

    @app.post("/cmd")
    def cmd():
        verb, name, value = _parse(flask_request.form.get("line", ""))
        if verb == "click":
            bridge.submit("click", name=name)
        elif verb in ("fill", "select"):
            bridge.submit(verb, name=name, value=value)
        elif verb == "goto":
            bridge.submit("goto", url=name)
        return redirect("/")

    @app.post("/finish")
    def finish():
        bridge.decision = flask_request.form.get("resolution", "resumed")
        return ("<html><body style='font:14px sans-serif;padding:24px'>"
                "Control handed back to the automation. You can close this tab."
                "</body></html>")

    server = make_server("127.0.0.1", port, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _esc(s: Any) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _manual_action(before: str, after: str):
    from .manager import OperatorAction
    return OperatorAction(
        verb="manual_browser_session",
        args={"location_before": before, "location_after": after},
        ok=True,
        detail="Operator worked directly in the browser window; "
               "location moved {} -> {}".format(before, after),
    )
