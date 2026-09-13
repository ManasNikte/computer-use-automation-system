"""
The capability artifact: a typed, versioned, reviewable description of a flow.

This is the contract between three audiences, and every design decision here
comes from serving all three at once:

  * **A calling AI agent** needs a function signature -- typed inputs, typed
    outputs, and a statement of what non-happy-path answers it might get back.
    It should never need to read a step list to know how to call this.
  * **The replay engine** needs enough to execute without a model: ordered
    steps, ranked ways to address each control, and a way to verify each step
    actually landed.
  * **A human reviewer** -- at a bank, plausibly a compliance reviewer -- needs
    to read it and understand what it touches, what's irreversible, and what
    data leaves the building.

Load-bearing decisions:

1. **Locators are ranked strategy lists, not selectors.** A single selector is
   a single point of failure and tells you nothing when it breaks. A ranked
   list degrades gracefully *and* reports which rank it used, which is the
   drift signal (see `replay.executor`).

2. **Every step carries its own checkpoint and risk level.** Without a
   per-step checkpoint you find out a click silently failed four steps later,
   with a useless error. Without per-step risk the safety layer would have to
   re-derive intent from the step text at enforcement time.

3. **Expected business outcomes are declared in the artifact, not discovered
   at runtime.** "No such member" is part of this capability's *contract* --
   the caller must handle it. Leaving it to the executor to guess is how you
   end up returning a stack trace for a perfectly normal answer.

4. **Recovery rules are declared too.** "Dismiss the maintenance interstitial"
   and "re-authenticate on session expiry" are properties of the application,
   which is what the artifact describes. Hardcoding them in the executor would
   mean every new app needs an executor change.

5. **Nothing sensitive lives here.** Values for parameters marked `sensitive`
   are never recorded, only referenced by name. The artifact holds no model
   transcript, no credentials, and no member data.

6. **Tenant scoping is a first-class field, not a fork.** A capability is
   recorded against a *vendor product*, with per-tenant overrides layered on
   top. See `resolve_for_tenant`.
"""
from __future__ import annotations

import copy
import re
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = "cua.capability/v1"


class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    SELECT = "select"
    EXTRACT = "extract"
    ASSERT = "assert"


class RiskLevel(str, Enum):
    """Risk is about *reversibility*, not about how scary the button looks."""

    SAFE = "safe"                  # read-only: navigate, search, read a balance
    REVERSIBLE = "reversible"      # mutates transient state, undoable (fill a form field)
    IRREVERSIBLE = "irreversible"  # commits a real-world effect (open an account, post a transaction)


class ParamType(str, Enum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"


# --------------------------------------------------------------------------
# Addressing
# --------------------------------------------------------------------------
@dataclass
class Locator:
    """A ranked list of ways to address the same control.

    Ordering is the whole point and it is not arbitrary. The recorder emits
    strategies most-portable-first:

      1. `role_name`      - accessible role + name. The only strategy that has
                            a direct analogue on a native desktop surface, so
                            it ranks first wherever the platform agrees the
                            name is the accessible name.
      2. `row_role_name`  - role + name anchored inside the row containing a
                            given value. Required for result grids, where a
                            dozen rows each hold a control named "Open".
      3. `attr`           - a stable form-field attribute (`name=`). Legacy
                            apps essentially always have these even when they
                            have no ids and no test ids.
      4. `css`            - a generated structural selector. Brittle under
                            layout change; present as a last resort.
      5. `text`           - raw visible text. Weakest; ambiguous by nature.

    Replay records which index resolved. Anything above 0 is a drift warning.
    """

    strategies: List[Dict[str, str]] = field(default_factory=list)
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"strategies": self.strategies, "description": self.description}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Locator":
        return Locator(strategies=list(d.get("strategies", [])),
                       description=d.get("description", ""))


@dataclass
class Condition:
    """A predicate over surface state. Used for checkpoints, for detecting
    business outcomes, and for triggering recovery rules -- deliberately one
    type, because all three are the same question: "is the surface in state
    X right now?"
    """

    kind: str            # url_contains | text_present | text_absent | control_present
    value: str
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Condition":
        return Condition(kind=d["kind"], value=d["value"], description=d.get("description", ""))


# --------------------------------------------------------------------------
# Contract: inputs and outputs
# --------------------------------------------------------------------------
@dataclass
class InputParam:
    name: str
    type: ParamType = ParamType.STRING
    required: bool = True
    description: str = ""
    example: str = ""
    # `sensitive` params (credentials, tax IDs) are never written to logs,
    # evidence, or the artifact -- only referenced by name.
    sensitive: bool = False
    enum: Optional[List[str]] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["type"] = self.type.value
        return d

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "InputParam":
        return InputParam(
            name=d["name"],
            type=ParamType(d.get("type", "string")),
            required=bool(d.get("required", True)),
            description=d.get("description", ""),
            example=d.get("example", ""),
            sensitive=bool(d.get("sensitive", False)),
            enum=d.get("enum"),
        )


@dataclass
class OutputField:
    name: str
    type: ParamType = ParamType.STRING
    description: str = ""
    source_step_id: str = ""
    # Outputs can be regulated data. `redact_in_evidence` returns the value to
    # the caller but keeps it out of logs on disk.
    redact_in_evidence: bool = False

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["type"] = self.type.value
        return d

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "OutputField":
        return OutputField(
            name=d["name"],
            type=ParamType(d.get("type", "string")),
            description=d.get("description", ""),
            source_step_id=d.get("source_step_id", ""),
            redact_in_evidence=bool(d.get("redact_in_evidence", False)),
        )


@dataclass
class BusinessOutcome:
    """A named, expected, non-happy-path answer.

    This is part of the capability's public contract: a caller that invokes
    `open_member_subaccount` must be prepared for `member_not_found`. Modelling
    these as declared outcomes rather than exceptions is the single most
    important correctness decision in this schema.
    """

    name: str
    detect: Condition
    description: str = ""
    # Whether reaching this outcome means the run is over. A validation error
    # is terminal for *this* invocation even though the app is still usable.
    terminal: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "detect": self.detect.to_dict(),
                "description": self.description, "terminal": self.terminal}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "BusinessOutcome":
        return BusinessOutcome(
            name=d["name"],
            detect=Condition.from_dict(d["detect"]),
            description=d.get("description", ""),
            terminal=bool(d.get("terminal", True)),
        )


@dataclass
class RecoveryRule:
    """A declared, bounded response to a known transient condition.

    Bounded is the operative word: each rule names exactly one action and a
    hard attempt cap. There is no open-ended "try to figure it out" branch,
    because that would put a model back in the production decision loop, which
    is the thing this system exists to avoid.

    `action.kind` is one of:
      retry_step        - wait and re-attempt the current step
      click             - click a declared locator (dismiss a known interstitial)
      restart_from_step - re-run from a named earlier step, then resume where
                          we left off (session re-authentication)
    """

    name: str
    detect: Condition
    action: Dict[str, Any]
    max_attempts: int = 2
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "detect": self.detect.to_dict(), "action": self.action,
                "max_attempts": self.max_attempts, "description": self.description}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "RecoveryRule":
        return RecoveryRule(
            name=d["name"],
            detect=Condition.from_dict(d["detect"]),
            action=dict(d.get("action", {})),
            max_attempts=int(d.get("max_attempts", 2)),
            description=d.get("description", ""),
        )


@dataclass
class ExtractSpec:
    """How to pull one output value off the surface.

    Declarative and surface-portable on purpose. `labelled_value` -- "the value
    displayed next to the label X" -- covers the overwhelming majority of
    read-backs in these apps and survives column reordering, unlike an XPath.
    """

    output_name: str
    method: str = "labelled_value"   # labelled_value | regex | text_of
    label: str = ""                  # for labelled_value
    pattern: str = ""                # for regex (group 1 is the value)
    locator: Optional[Locator] = None  # for text_of

    def to_dict(self) -> Dict[str, Any]:
        return {
            "output_name": self.output_name,
            "method": self.method,
            "label": self.label,
            "pattern": self.pattern,
            "locator": self.locator.to_dict() if self.locator else None,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "ExtractSpec":
        return ExtractSpec(
            output_name=d["output_name"],
            method=d.get("method", "labelled_value"),
            label=d.get("label", ""),
            pattern=d.get("pattern", ""),
            locator=Locator.from_dict(d["locator"]) if d.get("locator") else None,
        )


@dataclass
class Step:
    id: str
    action: ActionType
    locator: Optional[Locator] = None
    # Exactly one of these supplies the value. `param` keeps secrets out of
    # the artifact; `literal` is for genuinely static values.
    param: Optional[str] = None
    literal: Optional[str] = None
    checkpoint: Optional[Condition] = None
    risk: RiskLevel = RiskLevel.SAFE
    extract: Optional[ExtractSpec] = None
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action.value,
            "locator": self.locator.to_dict() if self.locator else None,
            "param": self.param,
            "literal": self.literal,
            "checkpoint": self.checkpoint.to_dict() if self.checkpoint else None,
            "risk": self.risk.value,
            "extract": self.extract.to_dict() if self.extract else None,
            "notes": self.notes,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Step":
        return Step(
            id=d["id"],
            action=ActionType(d["action"]),
            locator=Locator.from_dict(d["locator"]) if d.get("locator") else None,
            param=d.get("param"),
            literal=d.get("literal"),
            checkpoint=Condition.from_dict(d["checkpoint"]) if d.get("checkpoint") else None,
            risk=RiskLevel(d.get("risk", "safe")),
            extract=ExtractSpec.from_dict(d["extract"]) if d.get("extract") else None,
            notes=d.get("notes", ""),
        )


@dataclass
class TargetBinding:
    """What this capability is recorded *against*.

    Note what is and isn't here. The vendor product and version range are
    intrinsic to the capability. The base URL is not -- it is per-install, so
    it is supplied at invocation time or via a tenant override. Baking a
    hostname into a capability is what forces a re-recording per tenant.
    """

    vendor_product: str
    surface: str = "web"                 # web | desktop
    product_version_range: str = "*"
    entry_path: str = "/"                # path appended to the per-install base URL
    app_name: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "TargetBinding":
        return TargetBinding(
            vendor_product=d["vendor_product"],
            surface=d.get("surface", "web"),
            product_version_range=d.get("product_version_range", "*"),
            entry_path=d.get("entry_path", "/"),
            app_name=d.get("app_name", ""),
        )


@dataclass
class Provenance:
    """How this artifact came to exist. Kept separate from the flow itself so
    the capability is reviewable without wading through discovery metadata --
    and so that re-discovering a capability doesn't churn the contract."""

    discovered_at: float = field(default_factory=time.time)
    discovered_by: str = ""          # model identifier, or "operator" for hand-edited
    run_id: str = ""
    recorded_on_tenant: str = ""
    recorded_on_version: str = ""
    steps_taken: int = 0
    # Set when a human took over mid-discovery; the reviewer should know.
    human_intervened: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Provenance":
        return Provenance(**{k: d.get(k, v) for k, v in asdict(Provenance()).items()})


@dataclass
class Capability:
    """The artifact. Serialized to JSON; this is the unit of review, approval,
    versioning and invocation."""

    id: str
    name: str
    version: int
    description: str
    target: TargetBinding
    inputs: List[InputParam] = field(default_factory=list)
    outputs: List[OutputField] = field(default_factory=list)
    steps: List[Step] = field(default_factory=list)
    business_outcomes: List[BusinessOutcome] = field(default_factory=list)
    recovery_rules: List[RecoveryRule] = field(default_factory=list)
    final_checkpoint: Optional[Condition] = None
    # Which installs this capability claims to work on. "*" means every tenant
    # running the vendor product within the version range.
    applies_to: List[str] = field(default_factory=lambda: ["*"])
    # Per-tenant deltas layered over the base recording. See resolve_for_tenant.
    overrides: Dict[str, Any] = field(default_factory=dict)
    status: str = "draft"            # draft | approved
    schema_version: str = SCHEMA_VERSION
    provenance: Provenance = field(default_factory=Provenance)

    # -- serialization -----------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "status": self.status,
            "target": self.target.to_dict(),
            "applies_to": self.applies_to,
            "inputs": [i.to_dict() for i in self.inputs],
            "outputs": [o.to_dict() for o in self.outputs],
            "business_outcomes": [b.to_dict() for b in self.business_outcomes],
            "recovery_rules": [r.to_dict() for r in self.recovery_rules],
            "steps": [s.to_dict() for s in self.steps],
            "final_checkpoint": self.final_checkpoint.to_dict() if self.final_checkpoint else None,
            "overrides": self.overrides,
            "provenance": self.provenance.to_dict(),
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Capability":
        return Capability(
            id=d["id"],
            name=d["name"],
            version=int(d["version"]),
            description=d.get("description", ""),
            target=TargetBinding.from_dict(d["target"]),
            inputs=[InputParam.from_dict(x) for x in d.get("inputs", [])],
            outputs=[OutputField.from_dict(x) for x in d.get("outputs", [])],
            steps=[Step.from_dict(x) for x in d.get("steps", [])],
            business_outcomes=[BusinessOutcome.from_dict(x) for x in d.get("business_outcomes", [])],
            recovery_rules=[RecoveryRule.from_dict(x) for x in d.get("recovery_rules", [])],
            final_checkpoint=Condition.from_dict(d["final_checkpoint"]) if d.get("final_checkpoint") else None,
            applies_to=list(d.get("applies_to", ["*"])),
            overrides=dict(d.get("overrides", {})),
            status=d.get("status", "draft"),
            schema_version=d.get("schema_version", SCHEMA_VERSION),
            provenance=Provenance.from_dict(d.get("provenance", {})),
        )

    # -- contract helpers --------------------------------------------------
    def step(self, step_id: str) -> Optional[Step]:
        for s in self.steps:
            if s.id == step_id:
                return s
        return None

    def input(self, name: str) -> Optional[InputParam]:
        for i in self.inputs:
            if i.name == name:
                return i
        return None

    def sensitive_param_names(self) -> List[str]:
        return [i.name for i in self.inputs if i.sensitive]

    def has_irreversible_step(self) -> bool:
        return any(s.risk == RiskLevel.IRREVERSIBLE for s in self.steps)

    def validate_params(self, params: Dict[str, Any]) -> List[str]:
        """Return a list of contract violations. Empty means the call is valid.

        Validating at the boundary means a calling agent gets "you didn't pass
        member_id" rather than a locator timeout forty seconds later.
        """
        problems: List[str] = []
        declared = {i.name for i in self.inputs}
        for i in self.inputs:
            if i.required and (i.name not in params or params[i.name] in (None, "")):
                problems.append("missing required input: {}".format(i.name))
                continue
            if i.name not in params:
                continue
            value = params[i.name]
            if i.type == ParamType.NUMBER:
                try:
                    float(str(value).replace(",", "").replace("$", ""))
                except (TypeError, ValueError):
                    problems.append("input {} must be a number, got {!r}".format(i.name, value))
            if i.enum and str(value) not in i.enum:
                problems.append("input {} must be one of {}, got {!r}".format(i.name, i.enum, value))
        for key in params:
            if key not in declared:
                problems.append("unknown input: {} (not declared by this capability)".format(key))
        return problems

    def to_tool_schema(self) -> Dict[str, Any]:
        """JSON-Schema tool definition, for the agent-facing capability catalog.

        This is why the artifact is typed at all: a calling agent discovers
        capabilities by name and invokes them with typed args, without ever
        seeing a step list. The declared business outcomes are surfaced in the
        description because the caller genuinely has to handle them.
        """
        props: Dict[str, Any] = {}
        required: List[str] = []
        for i in self.inputs:
            spec: Dict[str, Any] = {
                "type": {"string": "string", "number": "number", "boolean": "boolean"}[i.type.value],
                "description": i.description or i.name,
            }
            if i.enum:
                spec["enum"] = i.enum
            if i.example and not i.sensitive:
                spec["examples"] = [i.example]
            props[i.name] = spec
            if i.required:
                required.append(i.name)

        outcome_note = ""
        if self.business_outcomes:
            outcome_note = (
                "\n\nMay return one of these business outcomes instead of success: "
                + ", ".join(
                    "{} ({})".format(b.name, b.description or "no detail") for b in self.business_outcomes
                )
            )

        return {
            "name": self.id,
            "description": (self.description or self.name) + outcome_note,
            "input_schema": {"type": "object", "properties": props, "required": required},
            "returns": {
                o.name: {"type": o.type.value, "description": o.description} for o in self.outputs
            },
            "metadata": {
                "version": self.version,
                "status": self.status,
                "vendor_product": self.target.vendor_product,
                "irreversible": self.has_irreversible_step(),
            },
        }


# --------------------------------------------------------------------------
# Multi-tenant resolution
# --------------------------------------------------------------------------
def resolve_for_tenant(capability: Capability, tenant_id: str) -> Capability:
    """Produce the effective capability for one tenant install.

    The model: a capability is recorded once against a vendor product, and a
    tenant entry supplies only the *deltas*. Three kinds of delta cover
    essentially everything that differs between two institutions running the
    same software:

      `base_path`  - the install is mounted under a different route prefix.
      `labels`     - the same control is captioned differently ("Member
                     Number" vs "Account Holder ID"). Applied as a text
                     substitution across every locator strategy, checkpoint
                     and extraction label, because a rebranded install renames
                     the *same* control everywhere at once.
      `steps`      - per-step patches, for the rare genuine structural
                     difference. Deliberately last-resort: if a tenant needs
                     many step patches, that is the signal that it is really a
                     different product version and should be re-recorded.

    Overrides are applied to a deep copy; the stored artifact is never mutated.
    """
    override = (capability.overrides or {}).get(tenant_id)
    if not override:
        return capability

    eff = Capability.from_dict(copy.deepcopy(capability.to_dict()))

    labels: Dict[str, str] = override.get("labels", {})
    base_path: str = override.get("base_path", "")

    # Longest key first, and each region of the string is rewritten at most
    # once. Naive sequential `str.replace` chains: with
    # {"Member Number": "Account Holder ID", "Account": "Client"} the first
    # rule's *output* contains "Account", so the second rule rewrites it to
    # "Client Holder ID". That made correctness depend on key order inside a
    # JSON object -- something no reviewer would treat as significant. It also
    # made the operation non-idempotent ("Account Type" -> "Sub Account Type"
    # -> "Sub Sub Account Type" on a second pass).
    # The alternation includes the tenant's *own* captions as well as the base
    # ones, longest first. A tenant caption that contains its base caption
    # ("Account Type" -> "Sub Account Type") is then matched as itself and left
    # alone, which makes the whole operation idempotent -- otherwise a second
    # pass yields "Sub Sub Account Type", and a resolved capability could never
    # safely be resolved again.
    _terms = sorted(set(labels.keys()) | set(labels.values()), key=len, reverse=True)
    _label_re = re.compile("|".join(re.escape(t) for t in _terms)) if _terms else None
    _tenant_captions = set(labels.values())

    def _swap(match) -> str:
        term = match.group(0)
        if term in _tenant_captions:
            return term          # already this tenant's wording
        return labels.get(term, term)

    def relabel(text: Optional[str]) -> Optional[str]:
        if not text or _label_re is None:
            return text
        # Never rewrite inside a {param} placeholder: a label key that happens
        # to be a substring of a parameter name would corrupt it into one that
        # `_fill` can never substitute, and the checkpoint would then fail
        # permanently and inexplicably.
        parts = re.split(r"(\{\w+\})", text)
        return "".join(
            part if part.startswith("{") and part.endswith("}")
            else _label_re.sub(_swap, part)
            for part in parts
        )

    def repath(text: Optional[str]) -> Optional[str]:
        if not text or not base_path:
            return text
        # Require a segment boundary. Plain `startswith` treats base_path
        # "/cu" as already-applied to "/customers/login", leaving the path
        # unprefixed -- and the whole tenant then fails at step zero on a URL
        # that doesn't exist on their install.
        if text == base_path or text.startswith(base_path + "/"):
            return text
        return base_path + text

    for step in eff.steps:
        if step.locator:
            for strat in step.locator.strategies:
                for key in ("name", "row", "value"):
                    if key in strat:
                        strat[key] = relabel(strat[key])
            # The human-readable description is what `cua inspect --tenant X`
            # prints. Leaving it un-relabelled would show a reviewer the base
            # install's captions while the executor used the tenant's -- a
            # small inconsistency that would cost someone an afternoon.
            step.locator.description = relabel(step.locator.description)
        if step.action == ActionType.NAVIGATE and step.literal:
            step.literal = repath(step.literal)
        if step.checkpoint:
            if step.checkpoint.kind == "url_contains":
                step.checkpoint.value = repath(step.checkpoint.value)
            else:
                step.checkpoint.value = relabel(step.checkpoint.value)
        if step.extract and step.extract.label:
            step.extract.label = relabel(step.extract.label)

    for bo in eff.business_outcomes:
        if bo.detect.kind != "url_contains":
            bo.detect.value = relabel(bo.detect.value)

    for rule in eff.recovery_rules:
        if rule.detect.kind != "url_contains":
            rule.detect.value = relabel(rule.detect.value)
        loc = rule.action.get("locator")
        if isinstance(loc, dict):
            for strat in loc.get("strategies", []):
                for key in ("name", "row", "value"):
                    if key in strat:
                        strat[key] = relabel(strat[key])

    if eff.final_checkpoint:
        if eff.final_checkpoint.kind == "url_contains":
            eff.final_checkpoint.value = repath(eff.final_checkpoint.value)
        else:
            eff.final_checkpoint.value = relabel(eff.final_checkpoint.value)

    if base_path:
        eff.target.entry_path = repath(eff.target.entry_path)

    # Genuine structural patches, applied last so they win over relabelling.
    for step_id, patch in (override.get("steps") or {}).items():
        target_step = eff.step(step_id)
        if target_step is None:
            continue
        merged = target_step.to_dict()
        merged.update(patch)
        replacement = Step.from_dict(merged)
        eff.steps[eff.steps.index(target_step)] = replacement

    return eff
