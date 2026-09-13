"""
Run wiring: build a surface, a lease, an evidence recorder and an escalation
channel, and tear them down cleanly.

Kept in one place because discovery and replay need *identical* wiring --
same surface implementation, same policy enforcement, same evidence format,
same escalation path. If the two diverged, a capability could pass discovery
and fail replay for reasons that have nothing to do with the capability.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional
from urllib import error as urlerror
from urllib import request as urlrequest

from .escalation.channels import (
    ConsoleOperatorChannel,
    ScriptedOperatorChannel,
    TakeoverOperatorChannel,
    WebOperatorChannel,
)
from .escalation.lease import SessionLease
from .escalation.manager import EscalationManager
from .evidence.recorder import EvidenceRecorder, new_run_id
from .safety.policy import Policy
from .safety.redaction import Redactor
from .surfaces.web import WebSurface

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_EVIDENCE_ROOT = os.path.join(REPO_ROOT, "evidence")
DEFAULT_ARTIFACT_ROOT = os.path.join(REPO_ROOT, "artifacts")
DEFAULT_CONFIG_ROOT = os.path.join(REPO_ROOT, "config")


class RunContext:
    def __init__(self, surface, recorder, lease, policy, escalation, redactor):
        self.surface = surface
        self.recorder = recorder
        self.lease = lease
        self.policy = policy
        self.escalation = escalation
        self.redactor = redactor


def load_dotenv(path: Optional[str] = None) -> None:
    """Load `KEY=VALUE` lines from a `.env` at the repo root, if one exists.

    Deliberately tiny and dependency-free. Two rules worth stating:

      * **The real environment always wins.** An explicit `export GROQ_API_KEY=…`
        overrides the file, so a developer can switch provider for one command
        without editing anything.
      * `.env` is gitignored and nothing ever writes to it. Provider keys are
        read here and never reach an artifact, a log, or the repo.
    """
    path = path or os.path.join(REPO_ROOT, ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


def load_tenants(path: Optional[str] = None) -> Dict[str, Any]:
    path = path or os.path.join(DEFAULT_CONFIG_ROOT, "tenants.json")
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return {k: v for k, v in data.items() if not k.startswith("_")}


def tenant_config(tenant_id: str, path: Optional[str] = None) -> Dict[str, Any]:
    tenants = load_tenants(path)
    if tenant_id not in tenants:
        raise KeyError("Unknown tenant {!r}. Known: {}".format(tenant_id, sorted(tenants)))
    return tenants[tenant_id]


def build_channel(kind: str, evidence_dir: str, script_path: Optional[str] = None,
                  port: int = 5099, open_browser: bool = False):
    if kind in ("none", "off"):
        return None
    if kind == "console":
        return ConsoleOperatorChannel(evidence_dir=evidence_dir)
    if kind == "takeover":
        return TakeoverOperatorChannel()
    if kind == "web":
        return WebOperatorChannel(evidence_dir=evidence_dir, port=port, open_browser=open_browser)
    if kind == "script":
        if not script_path:
            raise ValueError("--operator script requires --operator-script <file.json>")
        return ScriptedOperatorChannel.from_file(script_path)
    raise ValueError("Unknown operator channel {!r}".format(kind))


@contextmanager
def run_session(*, run_prefix: str, base_url: str, headed: bool = False,
                operator: str = "console", operator_script: Optional[str] = None,
                operator_port: int = 5099, open_console: bool = False,
                evidence_root: Optional[str] = None,
                policy: Optional[Policy] = None,
                secrets: Optional[Dict[str, str]] = None,
                echo: bool = True,
                slow_mo_ms: int = 0,
                run_id: Optional[str] = None) -> Iterator[RunContext]:
    from playwright.sync_api import sync_playwright

    from .safety.policy import default_policy

    evidence_root = evidence_root or DEFAULT_EVIDENCE_ROOT
    # A caller-supplied run id gives evidence a stable, meaningful directory
    # name. Used when regenerating the checked-in evidence, so a reviewer gets
    # `evidence/replay_session_timeout/` rather than a timestamp they have to
    # decode.
    run_id = run_id or new_run_id(run_prefix)

    redactor = Redactor()
    # Register secrets *before* anything can be logged, so there is no window
    # in which a credential could reach disk unmasked.
    for value in (secrets or {}).values():
        redactor.register_secret(str(value))

    recorder = EvidenceRecorder(evidence_root, run_id, redactor=redactor, echo=echo)
    policy = policy or default_policy(base_url)
    recorder.log("policy_loaded", **policy.to_dict())

    lease = SessionLease(session_id=run_id)
    lease.on_transition = lambda t: recorder.log(
        "control_transfer", **{"from": t.from_owner, "to": t.to_owner, "reason": t.reason})

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed, slow_mo=slow_mo_ms)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        surface = WebSurface(page, evidence_dir=recorder.dir)

        channel = build_channel(operator, recorder.dir, operator_script,
                                port=operator_port, open_browser=open_console)
        escalation = EscalationManager(
            lease=lease, channel=channel, recorder=recorder, policy=policy,
            enabled=channel is not None,
        ) if channel is not None else EscalationManager(
            lease=lease, channel=None, recorder=recorder, policy=policy, enabled=False)

        try:
            yield RunContext(surface, recorder, lease, policy, escalation, redactor)
        finally:
            try:
                context.close()
            finally:
                browser.close()


# --------------------------------------------------------------------------
# Target-app control plane (test harness only)
# --------------------------------------------------------------------------
def control(base_url: str, endpoint: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Drive the sandbox app's fault injection.

    This deliberately bypasses the automation entirely and is not reachable
    from any capability: `/_control/*` is on the policy's *denied* path list,
    so even a compromised artifact could not call it. It exists so a demo or a
    test can put the application into a specific runtime state -- session
    expired, maintenance interstitial up, back end erroring -- and then watch
    replay classify it.
    """
    url = base_url.rstrip("/") + "/_control/" + endpoint.lstrip("/")
    body = json.dumps(payload or {}).encode("utf-8")
    req = urlrequest.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urlrequest.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urlerror.URLError, ValueError) as exc:
        raise RuntimeError(
            "Could not reach the target app control plane at {}. Is it running? ({})".format(
                url, exc))


def check_target(base_url: str) -> Dict[str, Any]:
    try:
        with urlrequest.urlopen(base_url.rstrip("/") + "/_control/health", timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(
            "The target application is not reachable at {}.\n"
            "Start it with:  python target_app/app.py --port {} --tenant <id>\n"
            "({})".format(base_url, base_url.rsplit(":", 1)[-1], exc))
