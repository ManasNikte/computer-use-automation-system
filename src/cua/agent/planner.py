"""
The planner -- the one place in the entire system an LLM is called.

Everything upstream (observation) and downstream (acting, recording, replay)
is provider-agnostic and model-free, so the blast radius of a model change is
this file. That is the point: discovery is the only phase where a model is
allowed to decide anything, and production replay never enters this module.

What the model does and does not see:

  * It sees: the goal, the current location, the visible text, and a numbered
    list of controls as **role + accessible name**. Nothing else.
  * It does not see: HTML, CSS selectors, frame structure, or any addressing
    material. It cannot invent a selector, because it is never shown one --
    locator synthesis is the recorder's job, from hints the model never reads.
  * It does not see: secret values. Parameters are referenced by name
    (`{passcode}`), and the real value is substituted by the executor after
    the decision is made. A prompt log therefore cannot leak a credential.

The role+name-only interface is also what makes the design portable: the exact
same prompt works against a UIA tree from a desktop app, because "button named
Submit Request" means the same thing there.

Providers are called over stdlib HTTP rather than through vendor SDKs, so the
runtime dependency list stays at Flask + Playwright and switching provider is
a flag, not an install.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..surfaces.base import Observation

# Kept small on purpose: a discovery run is one call per step, and free
# provider tiers meter tokens per minute. The control list is what the
# planner actually decides from; the text is context.
_MAX_TEXT = 700
_TIMEOUT_S = 60


@dataclass
class Decision:
    """One planner decision. Deliberately a small closed vocabulary -- if a
    flow needs a verb that isn't here, that is a signal the flow needs a
    capability boundary, not a richer action language."""

    action: str                       # click|fill|select|navigate|extract|done|stuck
    ref: Optional[int] = None
    value: Optional[str] = None       # may be "{param_name}"
    output_name: Optional[str] = None # for extract
    label: Optional[str] = None       # for extract: the on-screen label to read next to
    reasoning: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


class PlannerError(Exception):
    pass


class RateLimited(PlannerError):
    """The provider asked us to slow down.

    Distinct from a generic PlannerError because the correct response is
    different: wait the amount the provider asked for and try again, rather
    than burning a retry immediately and then declaring the agent stuck. Free
    tiers have tight per-minute token budgets, and a discovery run makes one
    call per step, so hitting this mid-flow is expected rather than exceptional.
    """

    def __init__(self, message: str, retry_after: float = 5.0):
        super().__init__(message)
        self.retry_after = retry_after


def _retry_after_from(exc, body: str) -> float:
    header = exc.headers.get("Retry-After") if getattr(exc, "headers", None) else None
    if header:
        try:
            return float(header)
        except (TypeError, ValueError):
            pass
    # Providers often put the precise wait in the message: "try again in 4.605s"
    match = re.search(r"try again in ([\d.]+)\s*s", body)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass
    return 5.0


SYSTEM_PROMPT = """\
You are a computer-use agent operating a back-office banking application on \
behalf of a stated goal. You perceive the screen as a list of interactive \
controls, each with an accessible ROLE and NAME, exactly as a screen reader \
would present it.

Reply with ONE JSON object and nothing else. No prose. No markdown fences.

{
  "action": "click" | "fill" | "select" | "extract" | "done" | "stuck",
  "ref": <integer control ref, or null>,
  "value": "<string or null>",
  "output_name": "<snake_case name, only for extract>",
  "label": "<the on-screen label whose adjacent value you want, only for extract>",
  "reasoning": "<one short sentence>"
}

Rules:
- Only use a `ref` that appears in the CONTROLS list. Never invent one.
- To enter a value that came from the caller's input parameters, set `value` \
to the parameter name in braces, e.g. "{member_id}". Never write a literal \
credential.
- Use "select" for controls with role `combobox`, "fill" for role `textbox`, \
"click" for `button` and `link`.
- Use "extract" to capture a piece of data the goal asked for. Set \
`output_name` to a snake_case field name and `label` to the exact on-screen \
label text that sits next to the value (these screens display data as \
label/value pairs). Do not put the value itself in the response.
- Use "done" only when every part of the goal is satisfied, including any \
data the goal asked you to report.
- Prefer one step at a time. Do not repeat an action you have already taken \
unless the screen shows it did not take effect.

IMPORTANT -- these applications are multi-screen wizards. The control you \
ultimately need is often two or three screens away, reached by opening a \
record, then a form, then a review screen, then confirming. If the specific \
control for your immediate sub-task is not on this screen, do NOT give up: \
click the control that moves you toward it. A button captioned like the task \
you are trying to start (for example "Open Sub-Account" when you need to \
create a sub-account) is how you get to the form you are looking for.

"stuck" is a LAST RESORT, not a way to report that this particular screen \
isn't the one you wanted. Before choosing it, check every control in the \
list and ask whether any of them could plausibly advance the goal. Choose \
"stuck" only if none could -- for example the screen shows an application \
error, the record does not exist, you are refused permission, or the list of \
controls is genuinely empty.
"""


def render_prompt(obs: Observation, goal: str, params: Dict[str, str],
                  history: List[str], collected: Dict[str, str]) -> str:
    lines = [
        "GOAL: {}".format(goal),
        "",
        "CURRENT LOCATION: {}".format(obs.location),
        "SCREEN TITLE: {}".format(obs.title),
        "",
        "VISIBLE TEXT:",
        obs.text[:_MAX_TEXT],
        "",
        "CONTROLS:",
    ]
    if obs.controls:
        for c in obs.controls:
            suffix = ""
            if c.value:
                suffix = '  (current value: "{}")'.format(c.value[:40])
            lines.append('  [{}] {}: "{}"{}'.format(c.ref, c.role, c.name, suffix))
    else:
        lines.append("  (no interactive controls detected)")

    lines += ["", "AVAILABLE INPUT PARAMETERS (reference by name in braces, never inline a value):"]
    lines.append("  " + (", ".join("{{{}}}".format(k) for k in params) or "(none)"))

    if collected:
        lines += ["", "DATA ALREADY CAPTURED:"]
        for k, v in collected.items():
            lines.append("  {} = {}".format(k, v))

    if history:
        lines += ["", "ACTIONS SO FAR (most recent last):"]
        lines += ["  {}".format(h) for h in history[-8:]]

    return "\n".join(lines)


def _parse_decision(text: str) -> Decision:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"```\s*$", "", raw).strip()
    # Models occasionally prepend a sentence; take the first JSON object.
    if not raw.startswith("{"):
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            raw = match.group(0)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise PlannerError("Planner returned non-JSON: {!r}".format(text[:300])) from exc

    action = str(data.get("action") or data.get("type") or "stuck").lower()
    ref = data.get("ref")
    if ref is None:
        ref = data.get("target_ref")
    try:
        ref = int(ref) if ref is not None else None
    except (TypeError, ValueError):
        ref = None
    return Decision(
        action=action,
        ref=ref,
        value=data.get("value"),
        output_name=data.get("output_name"),
        label=data.get("label"),
        reasoning=str(data.get("reasoning") or "")[:300],
        raw=data,
    )


# Several providers sit behind a CDN that rejects the default
# `Python-urllib/3.x` agent outright (Groq answers HTTP 403 "error code: 1010",
# which looks exactly like an auth failure and isn't). Identify ourselves
# properly instead.
_USER_AGENT = "cua-automation/1.0 (+https://github.com/) Python-urllib"


def _http_json(url: str, payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", _USER_AGENT)
    req.add_header("Accept", "application/json")
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        if exc.code == 429:
            raise RateLimited(
                "Rate limited by the provider: {}".format(detail),
                retry_after=_retry_after_from(exc, detail)) from exc
        raise PlannerError("LLM provider returned HTTP {}: {}".format(exc.code, detail)) from exc
    except urllib.error.URLError as exc:
        raise PlannerError("Could not reach the LLM provider: {}".format(exc.reason)) from exc


class Planner:
    """Interface. `model_id` is recorded in artifact provenance so a reviewer
    knows which model discovered a capability."""

    model_id = "abstract"
    is_llm = True

    def decide(self, obs: Observation, goal: str, params: Dict[str, str],
               history: List[str], collected: Dict[str, str]) -> Decision:
        raise NotImplementedError


class OpenAICompatPlanner(Planner):
    """Any OpenAI-compatible /chat/completions endpoint.

    Covers Groq (the recommended free option -- no card, fast, generous
    limits), OpenAI itself, Together, Fireworks, and a local Ollama. Only the
    base URL and model name change.
    """

    is_llm = True

    def __init__(self, model: str, api_key: str, base_url: str, label: str = "openai-compat"):
        if not api_key:
            raise PlannerError(
                "No API key found for the {} planner. Set the relevant environment "
                "variable (see README) or use --planner sandbox to run offline.".format(label)
            )
        self.model_id = "{}:{}".format(label, model)
        self._model = model
        self._key = api_key
        self._url = base_url.rstrip("/") + "/chat/completions"

    def decide(self, obs, goal, params, history, collected) -> Decision:
        data = _http_json(
            self._url,
            {
                "model": self._model,
                "temperature": 0,
                "max_tokens": 400,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": render_prompt(obs, goal, params, history, collected)},
                ],
            },
            {"Authorization": "Bearer {}".format(self._key)},
        )
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise PlannerError("Unexpected provider response: {}".format(str(data)[:300])) from exc
        return _parse_decision(content)


class AnthropicPlanner(Planner):
    is_llm = True

    def __init__(self, model: str = "claude-sonnet-5", api_key: str = ""):
        if not api_key:
            raise PlannerError("ANTHROPIC_API_KEY is not set.")
        self.model_id = "anthropic:{}".format(model)
        self._model = model
        self._key = api_key
        self._url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/") \
            + "/v1/messages"

    def decide(self, obs, goal, params, history, collected) -> Decision:
        data = _http_json(
            self._url,
            {
                "model": self._model,
                "max_tokens": 400,
                "temperature": 0,
                "system": SYSTEM_PROMPT,
                "messages": [
                    {"role": "user",
                     "content": render_prompt(obs, goal, params, history, collected)},
                ],
            },
            {"x-api-key": self._key, "anthropic-version": "2023-06-01"},
        )
        try:
            text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
        except (KeyError, TypeError) as exc:
            raise PlannerError("Unexpected Anthropic response: {}".format(str(data)[:300])) from exc
        return _parse_decision(text)


class GeminiPlanner(Planner):
    is_llm = True

    def __init__(self, model: str = "gemini-2.0-flash", api_key: str = ""):
        if not api_key:
            raise PlannerError("GEMINI_API_KEY is not set.")
        self.model_id = "google:{}".format(model)
        self._url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "{}:generateContent?key={}".format(model, api_key)
        )

    def decide(self, obs, goal, params, history, collected) -> Decision:
        prompt = SYSTEM_PROMPT + "\n\n" + render_prompt(obs, goal, params, history, collected)
        data = _http_json(
            self._url,
            {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0, "maxOutputTokens": 400,
                                     "responseMimeType": "application/json"},
            },
            {},
        )
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise PlannerError("Unexpected Gemini response: {}".format(str(data)[:300])) from exc
        return _parse_decision(text)


class RecordedPlanner(Planner):
    """Replays the decisions a real model made in a previous discovery run.

    This is the honest way to get a deterministic, offline discovery run: it
    is a *recording of an actual LLM session*, read back from that run's
    `events.jsonl`, not a hand-written imitation of one. Used in tests and to
    regenerate evidence without spending tokens.

    It fails loudly if the surface has diverged from what was recorded, which
    is the correct behaviour -- a silent fallback would make a broken app look
    like a working one.
    """

    is_llm = False

    def __init__(self, decisions: List[Dict[str, Any]], source: str = ""):
        self.model_id = "recorded:{}".format(source or "session")
        self._decisions = decisions
        self._i = 0

    @staticmethod
    def from_events(path: str) -> "RecordedPlanner":
        decisions: List[Dict[str, Any]] = []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("event") == "planner_decision":
                    decisions.append(rec)
        if not decisions:
            raise PlannerError("No planner_decision events found in {}".format(path))
        return RecordedPlanner(decisions, source=os.path.basename(os.path.dirname(path)))

    def decide(self, obs, goal, params, history, collected) -> Decision:
        if self._i >= len(self._decisions):
            return Decision(action="stuck", reasoning="Recorded session ended before the goal was met.")
        rec = self._decisions[self._i]
        self._i += 1
        return Decision(
            action=rec.get("action", "stuck"),
            ref=rec.get("ref"),
            value=rec.get("value"),
            output_name=rec.get("output_name"),
            label=rec.get("label"),
            reasoning=rec.get("reasoning", "") + " [replayed from recorded session]",
        )


class SandboxPlanner(Planner):
    """A deterministic, non-LLM stand-in so the pipeline is runnable with no
    API key at all.

    Stated plainly: **this is not a model and does not pretend to be one.** It
    is a small rule set that reads the live observation -- matching on control
    role and accessible name, the same inputs a model gets -- and picks the
    next control. It is bound to the bundled sandbox application's vocabulary,
    so it will not generalise to another app; a real model will.

    It exists because the assignment asks for a clean mock at the boundary
    when live access isn't available, and because a reviewer with no API key
    should still be able to run `discover` and get a real artifact out. When
    `GROQ_API_KEY` (or another provider key) is present, the LLM path is the
    default and this is never used.
    """

    is_llm = False
    model_id = "sandbox-rules:v1"

    def decide(self, obs, goal, params, history, collected) -> Decision:
        # `history` entries are produced by AgentLoop._history_line:
        #   fill/select -> "<action>:<param_name>",  click -> "click:<control name>"
        done = set(history)

        def find(role: str, *needles: str):
            for c in obs.controls:
                if c.role != role:
                    continue
                low = c.name.lower()
                if all(n.lower() in low for n in needles):
                    return c
            return None

        def todo(key: str) -> bool:
            return key not in done

        text = obs.text.lower()

        # --- confirmation screen: read the result, then stop -----------------
        if "new account number" in text:
            if "new_account_number" not in collected:
                return Decision("extract", output_name="new_account_number",
                                label="New Account Number",
                                reasoning="Capture the confirmed account number.")
            return Decision("done", reasoning="Account opened and the new number captured.")

        # --- authentication --------------------------------------------------
        signin = find("button", "sign in")
        if signin:
            field = find("textbox", "operator")
            if field and todo("fill:operator_id"):
                return Decision("fill", field.ref, "{operator_id}",
                                reasoning="Enter the operator id.")
            field = find("textbox", "passcode")
            if field and todo("fill:passcode"):
                return Decision("fill", field.ref, "{passcode}",
                                reasoning="Enter the passcode.")
            return Decision("click", signin.ref, reasoning="Submit the sign-in form.")

        # --- review screen: the irreversible commit --------------------------
        if "cannot be reversed" in text:
            submit = find("button", "submit")
            if submit:
                return Decision("click", submit.ref,
                                reasoning="Submit the sub-account request.")

        # --- sub-account form ------------------------------------------------
        type_field = find("combobox")
        deposit = find("textbox", "deposit")
        if type_field or deposit:
            if type_field and todo("select:account_type"):
                return Decision("select", type_field.ref, "{account_type}",
                                reasoning="Choose the requested account type.")
            if deposit and todo("fill:initial_deposit"):
                return Decision("fill", deposit.ref, "{initial_deposit}",
                                reasoning="Enter the initial deposit amount.")
            cont = find("button", "continue") or find("button", "next")
            if cont:
                return Decision("click", cont.ref, reasoning="Continue to the review screen.")

        # --- member record ---------------------------------------------------
        open_sub = find("button", "sub account") or find("button", "sub-account")
        if open_sub:
            return Decision("click", open_sub.ref,
                            reasoning="Start the sub-account request.")

        # --- member search ----------------------------------------------------
        search_field = find("textbox", "member") or find("textbox", "holder")
        if search_field and todo("fill:member_id"):
            return Decision("fill", search_field.ref, "{member_id}",
                            reasoning="Enter the member number to look up.")
        search_btn = find("button", "search") or find("button", "find")
        if search_btn and todo("click:{}".format(search_btn.name.lower())):
            return Decision("click", search_btn.ref, reasoning="Run the member search.")
        open_link = find("link", "open")
        if open_link:
            return Decision("click", open_link.ref,
                            reasoning="Open the matching member record.")

        return Decision("stuck", reasoning="No known next control on this screen.")


_PROVIDERS = {
    # Provider catalogues churn -- Groq retired llama-3.3-70b-versatile, and a
    # stale default surfaces as an HTTP 404 that reads like an auth problem.
    # `cua models` lists what a key can actually reach; `--model` overrides.
    "groq": ("GROQ_API_KEY", "https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
    "openai": ("OPENAI_API_KEY", "https://api.openai.com/v1", "gpt-4o-mini"),
    "together": ("TOGETHER_API_KEY", "https://api.together.xyz/v1",
                 "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
    "openrouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1",
                   "meta-llama/llama-3.3-70b-instruct:free"),
}


def available_provider() -> Optional[str]:
    """Pick a provider from whichever key is present in the environment.

    Order matters: Groq first because it is the free tier we recommend in the
    README, so a reviewer who followed the setup instructions gets the LLM
    path without passing any flags.
    """
    for name, (env_var, _url, _model) in _PROVIDERS.items():
        if os.environ.get(env_var):
            return name
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return "gemini"
    return None


def list_models(kind: str = "auto") -> List[str]:
    """Ask the provider which models a key can actually reach.

    Exists because provider catalogues churn: Groq retired the model this
    defaulted to, and the resulting HTTP 404 reads exactly like an auth
    failure. `cua models` answers "is my key fine and is the model gone?"
    in one command.
    """
    if kind == "auto":
        kind = available_provider() or "sandbox"
    if kind not in _PROVIDERS:
        raise PlannerError(
            "Listing models is only supported for OpenAI-compatible providers "
            "({}); got {!r}.".format(", ".join(sorted(_PROVIDERS)), kind))
    env_var, base_url, _ = _PROVIDERS[kind]
    key = os.environ.get(env_var, "")
    if not key:
        raise PlannerError("{} is not set.".format(env_var))
    req = urllib.request.Request(base_url.rstrip("/") + "/models")
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("User-Agent", _USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise PlannerError("HTTP {} listing models: {}".format(
            exc.code, exc.read().decode("utf-8", "replace")[:300])) from exc
    return sorted(m["id"] for m in data.get("data", []) if "id" in m)


def build_planner(kind: str = "auto", model: Optional[str] = None,
                  recorded_from: Optional[str] = None) -> Planner:
    if kind == "auto":
        kind = available_provider() or "sandbox"

    if kind == "sandbox":
        return SandboxPlanner()
    if kind == "recorded":
        if not recorded_from:
            raise PlannerError("--planner recorded requires --from <events.jsonl>")
        return RecordedPlanner.from_events(recorded_from)
    if kind == "anthropic":
        return AnthropicPlanner(model=model or "claude-sonnet-5",
                                api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    if kind == "gemini":
        return GeminiPlanner(
            model=model or "gemini-2.0-flash",
            api_key=os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", ""),
        )
    if kind in _PROVIDERS:
        env_var, base_url, default_model = _PROVIDERS[kind]
        return OpenAICompatPlanner(
            model=model or default_model,
            api_key=os.environ.get(env_var, ""),
            base_url=base_url,
            label=kind,
        )
    raise PlannerError(
        "Unknown planner {!r}. Choose from: auto, sandbox, recorded, {}, anthropic, gemini".format(
            kind, ", ".join(sorted(_PROVIDERS)))
    )
