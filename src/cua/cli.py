"""
Command-line entry point.

    cua discover   run the LLM-driven loop on a goal and record a capability
    cua approve    move a capability draft -> approved (the human review gate)
    cua replay     execute a saved capability deterministically, no LLM
    cua catalog    list capabilities as typed tools an agent could invoke
    cua invoke     call a capability by name with typed args (the agent path)
    cua inspect    print a capability's contract and step plan
    cua models     show which LLM models the configured key can reach
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, Optional

from .agent.loop import AgentLoop
from .agent.planner import (PlannerError, available_provider, build_planner,
                            list_models)
from .artifact.recorder import Recorder, load_outcome_library
from .artifact.schema import Capability, resolve_for_tenant
from .artifact.store import CapabilityCatalog, CapabilityStore
from .replay.executor import ReplayExecutor
from .replay.result import SUCCESS
from .safety.policy import Policy, default_policy
from .session import (
    DEFAULT_ARTIFACT_ROOT,
    DEFAULT_CONFIG_ROOT,
    DEFAULT_EVIDENCE_ROOT,
    check_target,
    control,
    load_dotenv,
    run_session,
    tenant_config,
)

_INJECTABLE = ("slow", "interstitial", "session_timeout", "app_error")


# --------------------------------------------------------------------------
# shared argument groups
# --------------------------------------------------------------------------
def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tenant", default="meridian",
                        help="tenant install to run against (see config/tenants.json)")
    parser.add_argument("--base-url", default=None,
                        help="override the tenant's base URL")
    parser.add_argument("--headed", action="store_true",
                        help="show the browser window (required for --operator takeover)")
    parser.add_argument("--slow-mo", type=int, default=0,
                        help="milliseconds to slow each interaction, for watching a demo")
    parser.add_argument("--artifacts", default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--evidence", default=DEFAULT_EVIDENCE_ROOT)
    parser.add_argument("--operator", default="console",
                        choices=["console", "web", "takeover", "script", "none"],
                        help="how a human is brought in when the run gets stuck")
    parser.add_argument("--operator-script", default=None,
                        help="JSON file of operator commands, for --operator script")
    parser.add_argument("--operator-port", type=int, default=5099)
    parser.add_argument("--open-console", action="store_true",
                        help="open the web operator console in a browser automatically")
    parser.add_argument("--inject", default="",
                        help="comma-separated runtime conditions to inject into the "
                             "target app: " + ", ".join(_INJECTABLE))
    parser.add_argument("--reset-target", action="store_true",
                        help="reset the target app's data and injected state first")
    parser.add_argument("--run-id", default=None,
                        help="stable name for this run's evidence directory "
                             "(default: a timestamped id)")


def _params(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    if os.path.exists(raw):
        with open(raw, "r", encoding="utf-8") as fh:
            return json.load(fh)
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise SystemExit("--params must be JSON or a path to a JSON file: {}".format(exc))


def _resolve_target(args) -> Dict[str, Any]:
    cfg = tenant_config(args.tenant)
    base_url = args.base_url or cfg["base_url"]
    cfg = dict(cfg)
    cfg["base_url"] = base_url
    return cfg


def _prepare_target(args, base_url: str) -> None:
    check_target(base_url)
    if args.reset_target:
        control(base_url, "reset")
    injections = [i.strip() for i in (args.inject or "").split(",") if i.strip()]
    unknown = [i for i in injections if i not in _INJECTABLE]
    if unknown:
        raise SystemExit("Unknown --inject value(s): {}. Choose from {}".format(
            ", ".join(unknown), ", ".join(_INJECTABLE)))
    if injections:
        control(base_url, "inject", {name: True for name in injections})
        print("[target] injected runtime condition(s): {}".format(", ".join(injections)))


def _policy_for(args, base_url: str) -> Policy:
    custom = os.path.join(DEFAULT_CONFIG_ROOT, "policy.json")
    if os.path.exists(custom):
        policy = Policy.load(custom)
        if not policy.allowed_origins:
            policy.allowed_origins = default_policy(base_url).allowed_origins
        return policy
    return default_policy(base_url)


def _sensitive_values(params: Dict[str, Any]) -> Dict[str, str]:
    hints = ("pass", "secret", "token", "pin", "credential")
    return {k: str(v) for k, v in params.items()
            if any(h in k.lower() for h in hints)}


# --------------------------------------------------------------------------
# discover
# --------------------------------------------------------------------------
def cmd_discover(args) -> int:
    cfg = _resolve_target(args)
    base_url = cfg["base_url"]
    _prepare_target(args, base_url)

    params = _params(args.params)
    if not params:
        raise SystemExit("--params is required for discovery: the goal's inputs "
                         "become the capability's typed input contract.")

    try:
        planner = build_planner(args.planner, model=args.model, recorded_from=args.from_events)
    except PlannerError as exc:
        raise SystemExit(str(exc))

    if not planner.is_llm and args.planner == "auto":
        print("[planner] No provider API key found in the environment, so discovery will "
              "use the offline '{}' stand-in.\n"
              "          Set GROQ_API_KEY (free: https://console.groq.com/keys) for a real "
              "LLM-driven run.".format(planner.model_id))
    else:
        print("[planner] {}".format(planner.model_id))

    entry_path = args.entry_path or ("/servicing/login" if cfg.get("overrides", {}).get("base_path")
                                     else "/login")
    entry_url = base_url.rstrip("/") + entry_path

    with run_session(run_prefix="discover", base_url=base_url, headed=args.headed,
                     operator=args.operator, operator_script=args.operator_script,
                     operator_port=args.operator_port, open_console=args.open_console,
                     evidence_root=args.evidence, policy=_policy_for(args, base_url),
                     secrets=_sensitive_values(params), slow_mo_ms=args.slow_mo,
                     run_id=args.run_id) as ctx:
        loop = AgentLoop(ctx.surface, planner, ctx.policy, ctx.recorder,
                         escalation=ctx.escalation, lease=ctx.lease,
                         max_steps=args.max_steps)
        result = loop.run(args.goal, {k: str(v) for k, v in params.items()}, entry_url)

        print("\n[discovery] status={} steps={} outputs={}".format(
            result.status, result.steps_taken, list(result.outputs)))
        if result.reason:
            print("[discovery] {}".format(result.reason))

        if result.status != "completed":
            print("\nNo capability recorded: discovery did not complete.")
            print("Evidence: {}".format(ctx.recorder.dir))
            return 2

        store = CapabilityStore(args.artifacts)
        capability_id = args.capability_id
        version = args.version or store.next_version(capability_id)

        library = load_outcome_library(
            args.outcomes or os.path.join(
                DEFAULT_CONFIG_ROOT, "outcomes.{}.json".format(cfg.get("vendor_product", "unknown")))
        )
        overrides = _collect_overrides(args.tenant)

        recorder = Recorder(
            params={k: str(v) for k, v in params.items()},
            tenant_id=args.tenant,
            vendor_product=cfg.get("vendor_product", "unknown"),
            product_version=cfg.get("product_version", ""),
            base_url=base_url,
        )
        capability = recorder.record(
            result,
            capability_id=capability_id,
            name=args.name or capability_id.replace("_", " ").title(),
            description=args.description or args.goal,
            goal=args.goal,
            entry_path=entry_path,
            run_id=ctx.recorder.run_id,
            model_id=planner.model_id,
            version=version,
            outcome_library=library,
            tenant_overrides=overrides,
        )
        path = store.save(capability, overwrite=args.overwrite)
        ctx.recorder.write_json("recorded_capability.json", capability.to_dict())
        ctx.recorder.log("capability_recorded", capability=capability.id,
                         version=capability.version, path=os.path.basename(path))

        print("\n[artifact] {}".format(path))
        print(_describe(capability))
        print("\nNext:  cua approve --id {} --version {}".format(capability.id, capability.version))
        print("Evidence: {}".format(ctx.recorder.dir))
        return 0


def _collect_overrides(recorded_tenant: str) -> Dict[str, Any]:
    """Attach every *other* tenant's overrides to the artifact.

    The capability is recorded on one install but is meant to be invocable on
    any install of the same product, so it ships with the deltas for the others
    rather than needing a re-recording per institution.
    """
    from .session import load_tenants

    out: Dict[str, Any] = {}
    for tenant_id, cfg in load_tenants().items():
        if tenant_id == recorded_tenant:
            continue
        if cfg.get("overrides"):
            out[tenant_id] = cfg["overrides"]
    return out


# --------------------------------------------------------------------------
# approve / inspect / catalog
# --------------------------------------------------------------------------
def cmd_approve(args) -> int:
    store = CapabilityStore(args.artifacts)
    version = args.version or max(store.versions(args.id) or [0]) or None
    capability = store.load(args.id, version)
    if capability.has_irreversible_step() and not args.yes:
        print(_describe(capability))
        print("\nThis capability contains irreversible step(s). Approving it permits "
              "unattended replay of those steps.")
        answer = input("Type the capability id to confirm approval: ").strip()
        if answer != capability.id:
            print("Not approved.")
            return 1
    updated = store.set_status(capability.id, capability.version,
                               "draft" if args.revoke else "approved")
    print("{} v{} is now {}.".format(updated.id, updated.version, updated.status))
    return 0


def cmd_inspect(args) -> int:
    store = CapabilityStore(args.artifacts)
    capability = store.load(args.id, args.version)
    if args.tenant:
        capability = resolve_for_tenant(capability, args.tenant)
        print("(resolved for tenant {!r})\n".format(args.tenant))
    if args.json:
        print(json.dumps(capability.to_dict(), indent=2))
        return 0
    print(_describe(capability, verbose=True))
    return 0


def cmd_models(args) -> int:
    """Which models can this key reach, and which one would be used."""
    provider = args.provider or available_provider()
    if provider is None:
        print("No provider API key found in the environment.\n"
              "Set GROQ_API_KEY (free: https://console.groq.com/keys) in your shell "
              "or in a .env at the repo root.\n"
              "Discovery will otherwise use the offline 'sandbox' stand-in; replay "
              "never needs a model.")
        return 1
    print("provider: {}".format(provider))
    try:
        print("default : {}".format(build_planner(provider).model_id))
        print("\navailable to this key:")
        for name in list_models(provider):
            print("  {}".format(name))
    except PlannerError as exc:
        print("\n{}".format(exc))
        return 1
    return 0


def cmd_catalog(args) -> int:
    store = CapabilityStore(args.artifacts)
    catalog = CapabilityCatalog(store, approved_only=not args.include_drafts)
    tools = catalog.tools()
    if args.json:
        print(json.dumps(tools, indent=2))
        return 0
    if not tools:
        print("No {}capabilities in {}.".format(
            "" if args.include_drafts else "approved ", args.artifacts))
        if not args.include_drafts:
            print("(Drafts exist but are hidden. Run with --include-drafts, or approve one.)")
        return 0
    print("Capabilities an agent can invoke:\n")
    for tool in tools:
        meta = tool["metadata"]
        flag = "  [IRREVERSIBLE]" if meta["irreversible"] else ""
        print("  {}  v{}  ({}){}".format(tool["name"], meta["version"], meta["status"], flag))
        print("    {}".format(tool["description"].split("\n")[0]))
        required = tool["input_schema"].get("required", [])
        for pname, spec in tool["input_schema"]["properties"].items():
            print("      {} {:<16} {}".format(
                "*" if pname in required else " ", pname + ":" + spec["type"],
                spec.get("description", "")))
        for oname, spec in tool["returns"].items():
            print("      -> {:<16} {}".format(oname + ":" + spec["type"], spec.get("description", "")))
        # Business outcomes are part of the contract, not a footnote: a caller
        # that cannot handle `member_not_found` is not ready to use this.
        capability = catalog.get(tool["name"])
        if capability.business_outcomes:
            print("      may return instead of success:")
            for b in capability.business_outcomes:
                print("        ~ {:<22} {}".format(b.name, b.description))
        print()
    return 0


# --------------------------------------------------------------------------
# replay / invoke
# --------------------------------------------------------------------------
def cmd_replay(args) -> int:
    cfg = _resolve_target(args)
    base_url = cfg["base_url"]
    _prepare_target(args, base_url)

    store = CapabilityStore(args.artifacts)
    try:
        capability = store.load(args.id, args.version)
    except FileNotFoundError as exc:
        raise SystemExit(str(exc))

    effective = resolve_for_tenant(capability, args.tenant)
    if effective is not capability:
        print("[tenant] applied {!r} overrides to {} v{}".format(
            args.tenant, capability.id, capability.version))

    params = _params(args.params)
    return _run_replay(args, effective, params, base_url, cfg)


def cmd_invoke(args) -> int:
    """The agent-facing path: call a capability by name with typed args.

    Goes through the catalog rather than the store, so it sees only approved
    capabilities -- the same view a production agent would have.
    """
    cfg = _resolve_target(args)
    base_url = cfg["base_url"]
    _prepare_target(args, base_url)

    store = CapabilityStore(args.artifacts)
    catalog = CapabilityCatalog(store, approved_only=not args.include_drafts)
    try:
        capability = catalog.get(args.name)
    except KeyError as exc:
        raise SystemExit(str(exc))

    effective = resolve_for_tenant(capability, args.tenant)
    args_dict = _params(args.args)
    problems = effective.validate_params(args_dict)
    if problems:
        print(json.dumps({"status": "invalid_arguments", "problems": problems}, indent=2))
        return 1
    return _run_replay(args, effective, args_dict, base_url, cfg, as_json=True)


def _run_replay(args, capability: Capability, params: Dict[str, Any],
                base_url: str, cfg: Dict[str, Any], as_json: bool = False) -> int:
    with run_session(run_prefix="replay", base_url=base_url, headed=args.headed,
                     operator=args.operator, operator_script=args.operator_script,
                     operator_port=args.operator_port, open_console=args.open_console,
                     evidence_root=args.evidence, policy=_policy_for(args, base_url),
                     secrets=_sensitive_values(params), echo=not as_json,
                     slow_mo_ms=args.slow_mo, run_id=args.run_id) as ctx:
        executor = ReplayExecutor(ctx.surface, ctx.policy, ctx.recorder,
                                  escalation=ctx.escalation, lease=ctx.lease)
        result = executor.replay(capability, params, base_url)
        ctx.recorder.write_json("replay_result.json", result.to_dict())

    if as_json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print("\n" + result.summary())

    # Exit codes are part of the contract for anything shelling out to this:
    # 0 success, 3 business outcome (valid answer, not an error), 1 failure.
    if result.status == SUCCESS:
        return 0
    if result.status == "business_outcome":
        return 3
    return 1


# --------------------------------------------------------------------------
# presentation
# --------------------------------------------------------------------------
def _describe(capability: Capability, verbose: bool = False) -> str:
    lines = [
        "{}  v{}  [{}]".format(capability.id, capability.version, capability.status),
        "  {}".format(capability.description),
        "  product   {} {} (surface: {})".format(
            capability.target.vendor_product, capability.target.product_version_range,
            capability.target.surface),
        "  entry     {}".format(capability.target.entry_path),
        "  recorded  by {} on tenant {!r} v{}{}".format(
            capability.provenance.discovered_by or "?",
            capability.provenance.recorded_on_tenant,
            capability.provenance.recorded_on_version,
            "  (HUMAN INTERVENED)" if capability.provenance.human_intervened else ""),
    ]
    lines.append("  inputs")
    for i in capability.inputs:
        lines.append("    {} {:<18} {}{}".format(
            "*" if i.required else " ", i.name + ":" + i.type.value,
            i.description, "  [sensitive]" if i.sensitive else ""))
    lines.append("  outputs")
    for o in capability.outputs:
        lines.append("      {:<18} {}{}".format(
            o.name + ":" + o.type.value, o.description,
            "  [redacted in evidence]" if o.redact_in_evidence else ""))
    lines.append("  business outcomes")
    for b in capability.business_outcomes:
        lines.append("      {:<24} {}".format(b.name, b.description))
    lines.append("  recovery rules")
    for r in capability.recovery_rules:
        lines.append("      {:<24} {}".format(r.name, r.action.get("kind", "")))
    if capability.overrides:
        lines.append("  tenant overrides  {}".format(", ".join(sorted(capability.overrides))))
    lines.append("  steps ({})".format(len(capability.steps)))
    for s in capability.steps:
        risk = "" if s.risk.value == "safe" else "  <{}>".format(s.risk.value)
        detail = s.locator.description if s.locator else (s.literal or "")
        if s.action.value == "extract" and s.extract:
            detail = "{} <- label {!r}".format(s.extract.output_name, s.extract.label)
        lines.append("    {:<28} {:<9} {}{}".format(s.id, s.action.value, detail, risk))
        if verbose:
            for n, strat in enumerate(s.locator.strategies if s.locator else []):
                lines.append("        {} {}".format(
                    "->" if n == 0 else "  ", json.dumps(strat, sort_keys=True)))
            if s.checkpoint:
                lines.append("           verify: {} {!r}".format(
                    s.checkpoint.kind, s.checkpoint.value))
    if capability.final_checkpoint:
        lines.append("  success   {} {!r}".format(
            capability.final_checkpoint.kind, capability.final_checkpoint.value))
    return "\n".join(lines)


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cua",
        description="Discover a UI flow with an LLM once; replay it deterministically forever.")
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="run the LLM-driven loop and record a capability")
    _add_common(d)
    d.add_argument("--goal", required=True)
    d.add_argument("--params", required=True,
                   help="JSON object (or path to one) of the inputs this goal needs")
    d.add_argument("--capability-id", required=True)
    d.add_argument("--name", default=None)
    d.add_argument("--description", default=None)
    d.add_argument("--entry-path", default=None)
    d.add_argument("--planner", default="auto",
                   help="auto | groq | openai | together | openrouter | anthropic | gemini "
                        "| sandbox | recorded")
    d.add_argument("--model", default=None)
    d.add_argument("--from-events", default=None,
                   help="events.jsonl to replay decisions from, for --planner recorded")
    d.add_argument("--max-steps", type=int, default=25)
    d.add_argument("--outcomes", default=None, help="path to the app's outcome library JSON")
    d.add_argument("--version", type=int, default=None)
    d.add_argument("--overwrite", action="store_true")
    d.set_defaults(func=cmd_discover)

    a = sub.add_parser("approve", help="mark a capability approved for unattended replay")
    a.add_argument("--id", required=True)
    a.add_argument("--version", type=int, default=None)
    a.add_argument("--artifacts", default=DEFAULT_ARTIFACT_ROOT)
    a.add_argument("--revoke", action="store_true", help="move it back to draft")
    a.add_argument("--yes", action="store_true", help="skip the interactive confirmation")
    a.set_defaults(func=cmd_approve)

    i = sub.add_parser("inspect", help="print a capability's contract and step plan")
    i.add_argument("--id", required=True)
    i.add_argument("--version", type=int, default=None)
    i.add_argument("--tenant", default=None, help="show the capability resolved for a tenant")
    i.add_argument("--artifacts", default=DEFAULT_ARTIFACT_ROOT)
    i.add_argument("--json", action="store_true")
    i.set_defaults(func=cmd_inspect)

    c = sub.add_parser("catalog", help="list capabilities as tools an agent can invoke")
    c.add_argument("--artifacts", default=DEFAULT_ARTIFACT_ROOT)
    c.add_argument("--json", action="store_true")
    c.add_argument("--include-drafts", action="store_true")
    c.set_defaults(func=cmd_catalog)

    m = sub.add_parser("models", help="show which LLM models the configured key can reach")
    m.add_argument("--provider", default=None,
                   help="groq | openai | together | openrouter (default: auto-detect)")
    m.set_defaults(func=cmd_models)

    r = sub.add_parser("replay", help="execute a saved capability deterministically")
    _add_common(r)
    r.add_argument("--id", required=True)
    r.add_argument("--version", type=int, default=None)
    r.add_argument("--params", default="{}")
    r.set_defaults(func=cmd_replay)

    v = sub.add_parser("invoke", help="the agent path: call a capability by name, JSON in/out")
    _add_common(v)
    v.add_argument("name")
    v.add_argument("--args", default="{}")
    v.add_argument("--include-drafts", action="store_true")
    v.set_defaults(func=cmd_invoke)

    return parser


def main(argv=None) -> int:
    # Before anything reads os.environ for a provider key.
    load_dotenv()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as exc:
        print("\n{}".format(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
