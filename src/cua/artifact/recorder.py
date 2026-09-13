"""
Recorder: turn a successful discovery trace into a reusable capability.

This is where a one-off model run becomes a durable asset, and it is doing
three jobs that are easy to underestimate.

**1. Locator synthesis.** The model chose a control by role and name. That
alone is not a good production locator -- most importantly because in a
results grid, `role=link name="Open"` matches *every* row. The recorder has
access to addressing hints the model never saw (frame, form-field name,
containing-row text, CSS path) and emits a ranked strategy list, choosing the
ranking from evidence rather than a fixed template:

  - if the control lived inside a table row whose key cell carried a value we
    can parameterize, the row-anchored strategy goes **first**, because
    role+name alone would be *wrong*, not merely fragile;
  - role+name goes first otherwise, but only when the platform agrees that
    name is the accessible name (see `name_source` in web_probe.js). A name we
    inferred from an adjacent table cell reads well to a human but will not
    resolve via role+name, so for those controls the form-field attribute
    leads instead.

**2. Checkpoint inference.** For each step, the recorder compares the surface
before and after and derives a statement of what should be true if the step
worked: the path changed, or an input value we supplied appeared on screen, or
the screen title changed. Steps with no observable effect (typing into a
field) correctly get no checkpoint rather than a fabricated one.

**3. Parameterization.** Concrete values are substituted back into `{param}`
placeholders everywhere they appear -- in URLs, in row anchors, in
checkpoints. This is what makes the artifact work for *any* member rather
than the one it was recorded against, and it is the same mechanism that makes
cross-tenant reuse possible.

What the recorder deliberately does **not** invent: business outcomes and
recovery rules. A happy-path run never sees "no such member", so pretending to
have discovered it would be a lie. Those come from a per-application library
(`config/outcomes.*.json`) -- which matches how this works in practice, since
"record not found" is a property of the *application*, shared by every
capability recorded against it, and is part of a contract that needs human
review anyway.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from ..agent.loop import DiscoveryResult, TraceStep
from .schema import (
    ActionType,
    BusinessOutcome,
    Capability,
    Condition,
    ExtractSpec,
    InputParam,
    Locator,
    OutputField,
    ParamType,
    Provenance,
    RecoveryRule,
    RiskLevel,
    Step,
    TargetBinding,
)

# Names that mean a value must never be written down.
_SENSITIVE_HINTS = ("pass", "secret", "token", "pin", "credential", "ssn", "tax")
# Outputs that are regulated data: returned to the caller, kept out of evidence.
_REDACT_OUTPUT_HINTS = ("account_number", "tax", "ssn", "balance", "card")

# Name sources the browser itself will agree constitute the accessible name.
# A name we recovered from an adjacent table cell is NOT one of these.
_PLATFORM_NAMES = {
    "aria-label", "aria-labelledby", "label-for", "label-ancestor",
    "value", "text", "placeholder", "title",
}


class Recorder:
    def __init__(self, params: Dict[str, str], tenant_id: str = "",
                 vendor_product: str = "", product_version: str = "",
                 base_url: str = ""):
        self.params = dict(params or {})
        self.tenant_id = tenant_id
        self.vendor_product = vendor_product
        self.product_version = product_version
        self.base_url = base_url.rstrip("/")
        # Longest first: substituting "100237" before "1002" avoids a partial
        # replacement producing a corrupt placeholder.
        self._subs: List[Tuple[str, str]] = sorted(
            ((str(v), name) for name, v in self.params.items() if str(v)),
            key=lambda pair: len(pair[0]), reverse=True,
        )

    # -- public ------------------------------------------------------------
    def record(self, result: DiscoveryResult, *, capability_id: str, name: str,
               description: str, goal: str, entry_path: str, run_id: str,
               model_id: str, version: int = 1,
               outcome_library: Optional[Dict[str, Any]] = None,
               tenant_overrides: Optional[Dict[str, Any]] = None) -> Capability:
        steps: List[Step] = []
        used_params: List[str] = []

        for i, tstep in enumerate(result.trace):
            step = self._step(tstep, index=i, is_last=(i == len(result.trace) - 1))
            if step is None:
                continue
            # Collect every parameter the step actually depends on, from every
            # place a placeholder can end up. Missing one is not cosmetic: an
            # undeclared param makes `validate_params` reject the caller's
            # argument as "unknown input", and an *omitted* argument leaves the
            # literal "{member_id}" to be typed into the field -- which the app
            # answers with "no such member", and replay then reports as a
            # perfectly confident, completely wrong business outcome.
            if step.param:
                used_params.append(step.param)
            if step.literal:
                used_params.extend(re.findall(r"\{(\w+)\}", step.literal))
            for strat in (step.locator.strategies if step.locator else []):
                for key in ("row", "name", "value"):
                    used_params.extend(re.findall(r"\{(\w+)\}", strat.get(key, "") or ""))
            if step.checkpoint:
                used_params.extend(re.findall(r"\{(\w+)\}", step.checkpoint.value))
            if step.extract and step.extract.label:
                used_params.extend(re.findall(r"\{(\w+)\}", step.extract.label))
            steps.append(step)

        inputs = self._inputs(used_params)
        outputs = self._outputs(steps)
        library = outcome_library or {}

        capability = Capability(
            id=capability_id,
            name=name,
            version=version,
            # Generalise the description the same way the steps are
            # generalised. A goal is written about one concrete record ("look
            # up member 100237"); the capability it becomes works for any of
            # them, and a calling agent reading "member 100237" in the tool
            # description would reasonably conclude otherwise.
            description=self._parameterize(description) or description,
            target=TargetBinding(
                vendor_product=self.vendor_product or "unknown",
                surface="web",
                product_version_range=library.get("version_range", "*"),
                entry_path=entry_path,
                app_name=library.get("app_name", ""),
            ),
            inputs=inputs,
            outputs=outputs,
            steps=steps,
            business_outcomes=[BusinessOutcome.from_dict(b)
                               for b in library.get("business_outcomes", [])],
            recovery_rules=[RecoveryRule.from_dict(r)
                            for r in library.get("recovery_rules", [])],
            final_checkpoint=self._final_checkpoint(result, steps, entry_path),
            applies_to=list(library.get("applies_to", ["*"])),
            overrides=dict(tenant_overrides or {}),
            status="draft",
            provenance=Provenance(
                discovered_by=model_id,
                run_id=run_id,
                recorded_on_tenant=self.tenant_id,
                recorded_on_version=self.product_version,
                steps_taken=result.steps_taken,
                human_intervened=result.human_intervened,
            ),
        )
        return capability

    # -- steps -------------------------------------------------------------
    def _step(self, t: TraceStep, index: int, is_last: bool) -> Optional[Step]:
        step_id = self._step_id(t, index)

        if t.action == "extract":
            return Step(
                id=step_id,
                action=ActionType.EXTRACT,
                risk=RiskLevel.SAFE,
                extract=ExtractSpec(
                    output_name=t.output_name,
                    method="labelled_value",
                    label=t.extract_label,
                ),
                checkpoint=Condition(
                    kind="text_present", value=t.extract_label,
                    description="the screen is showing the field we are reading",
                ) if t.extract_label else None,
                notes=t.reasoning,
            )

        if t.action == "navigate":
            return Step(
                id=step_id,
                action=ActionType.NAVIGATE,
                literal=self._parameterize(_path_of(t.literal or t.location_after)),
                risk=RiskLevel.SAFE,
                checkpoint=self._checkpoint(t),
                notes=t.reasoning,
            )

        if t.action not in ("click", "fill", "select"):
            return None

        return Step(
            id=step_id,
            action=ActionType(t.action),
            locator=self._locator(t),
            param=t.param,
            literal=self._parameterize(t.literal) if t.literal else None,
            checkpoint=self._checkpoint(t),
            risk=RiskLevel(t.risk),
            notes=(t.reasoning + (" [recorded from a human operator action]"
                                  if t.by_operator else "")).strip(),
        )

    def _step_id(self, t: TraceStep, index: int) -> str:
        base = re.sub(r"[^a-z0-9]+", "_", (t.name or t.output_name or t.action).lower()).strip("_")
        return "{:02d}_{}_{}".format(index + 1, t.action, base or "step")[:60]

    # -- locators ----------------------------------------------------------
    def _locator(self, t: TraceStep) -> Locator:
        frame = t.hints.get("frame", "")
        role, name = t.role, t.name
        name_source = t.hints.get("name_source", "")
        row_anchor = (t.hints.get("row_anchor") or "").strip()
        name_attr = t.hints.get("name_attr", "")
        tag = t.hints.get("tag", "")
        css = t.hints.get("css", "")

        role_name = {"kind": "role_name", "role": role, "name": name, "frame": frame}
        strategies: List[Dict[str, str]] = []

        # A control inside a data row whose key cell we can parameterize: the
        # row anchor is not an optimisation, it is the only correct addressing.
        # "The link named Open" would silently pick row one for every input.
        row_is_useful = bool(row_anchor) and row_anchor.lower() != name.lower()
        row_param = self._parameterize(row_anchor) if row_is_useful else ""
        if row_is_useful and row_param != row_anchor:
            strategies.append({
                "kind": "row_role_name", "role": role, "name": name,
                "row": row_param, "frame": frame,
            })

        if name_source in _PLATFORM_NAMES:
            strategies.append(role_name)

        if name_attr:
            strategies.append({
                "kind": "attr", "tag": tag, "attr": "name",
                "value": name_attr, "frame": frame,
            })

        # A row anchor with no parameter in it at all is still better than
        # nothing for disambiguation, but it pins us to one record, so it
        # ranks low.
        #
        # The test is "did parameterizing change anything", NOT "does the
        # result start with a placeholder". A key cell rendered as
        # "Member 100237" parameterizes to "Member {member_id}" -- which does
        # not start with "{" -- and emitting the raw variant alongside it would
        # leave a strategy hardcoded to the recorded member. On replay for a
        # different member, if the primary missed, that fallback would resolve
        # against the WRONG record's row and click its link, reported as a
        # drift signal rather than a failure. A confidently wrong action.
        if row_is_useful and row_param == row_anchor:
            strategies.append({
                "kind": "row_role_name", "role": role, "name": name,
                "row": row_anchor, "frame": frame,
            })

        # Generated CSS often embeds the recorded record's id (an href, say),
        # so it gets parameterized like everything else.
        if css:
            strategies.append({"kind": "css", "value": self._parameterize(css), "frame": frame})

        if role in ("button", "link") and name:
            strategies.append({"kind": "text", "value": name, "frame": frame})

        if not strategies:
            strategies.append(role_name)

        return Locator(
            strategies=_dedupe(strategies),
            description="{} {!r}{}".format(
                role, name, " in the row for {}".format(row_param) if row_param else ""),
        )

    # -- checkpoints -------------------------------------------------------
    def _checkpoint(self, t: TraceStep) -> Optional[Condition]:
        """Infer what should be observably true if this step worked.

        Ordered by how strong the signal is. A step that genuinely changes
        nothing observable (typing into a field) gets no checkpoint, which is
        the honest answer -- inventing one would make replay assert something
        that was never true.
        """
        path_before = _path_of(t.location_before)
        path_after = _path_of(t.location_after)

        if path_after and path_after != path_before:
            return Condition(
                kind="url_contains",
                value=self._parameterize(path_after),
                description="navigated to the expected screen",
            )

        # A value we supplied now appears on screen and did not before: the
        # strongest available signal that a search or a submit took effect,
        # and it parameterizes cleanly.
        for value, param in self._subs:
            if len(value) >= 3 and value in t.text_after and value not in t.text_before:
                return Condition(
                    kind="text_present",
                    value="{" + param + "}",
                    description="the value supplied for {} is now displayed".format(param),
                )

        if t.title_after and t.title_after != t.title_before:
            return Condition(
                kind="text_present",
                value=t.title_after,
                description="the expected screen is displayed",
            )

        return None

    def _final_checkpoint(self, result: DiscoveryResult, steps: List[Step],
                          entry_path: str) -> Condition:
        """The success condition for the capability as a whole.

        Prefers a semantic signal over a structural one: if the flow ends by
        reading a labelled field, then "that label is on screen" is a far
        better statement of success than a URL, because it survives routing
        changes and actually asserts we reached a screen showing the answer.
        """
        for step in steps:
            if step.action == ActionType.EXTRACT and step.extract and step.extract.label:
                return Condition(
                    kind="text_present",
                    value=step.extract.label,
                    description="the confirmation screen showing the result is displayed",
                )
        final_path = _path_of(result.final_location)
        if final_path and final_path != entry_path:
            return Condition(
                kind="url_contains",
                value=self._parameterize(final_path),
                description="reached the expected final screen",
            )
        return Condition(
            kind="text_present",
            value=result.final_title or "",
            description="reached the expected final screen",
        )

    # -- contract ----------------------------------------------------------
    def _inputs(self, used: List[str]) -> List[InputParam]:
        """The input contract is what the flow actually consumed.

        Params that were passed but never referenced by any step are dropped:
        advertising an input the capability ignores would be a lie in the
        function signature a calling agent depends on.
        """
        seen: List[str] = []
        for nm in used:
            if nm and nm not in seen:
                seen.append(nm)
        out: List[InputParam] = []
        for nm in seen:
            raw = str(self.params.get(nm, ""))
            sensitive = any(h in nm.lower() for h in _SENSITIVE_HINTS)
            out.append(InputParam(
                name=nm,
                type=ParamType.NUMBER if _looks_numeric(raw) else ParamType.STRING,
                required=True,
                description=nm.replace("_", " "),
                example="" if sensitive else raw,
                sensitive=sensitive,
            ))
        return out

    def _outputs(self, steps: List[Step]) -> List[OutputField]:
        """Declare each output exactly once.

        A model will sometimes re-read a value it already captured -- it is
        cheap and it makes the model more certain. Harmless at runtime, but
        recording it verbatim would declare the same field twice in the
        capability's return contract, which any caller generating a type from
        this would reject.
        """
        out: List[OutputField] = []
        seen: set = set()
        for step in steps:
            if step.action != ActionType.EXTRACT or not step.extract:
                continue
            nm = step.extract.output_name
            if nm in seen:
                continue
            seen.add(nm)
            out.append(OutputField(
                name=nm,
                type=ParamType.STRING,
                description="value read from the {!r} field".format(step.extract.label),
                source_step_id=step.id,
                redact_in_evidence=any(h in nm.lower() for h in _REDACT_OUTPUT_HINTS),
            ))
        return out

    # -- helpers -----------------------------------------------------------
    def _parameterize(self, text: Optional[str]) -> Optional[str]:
        """Replace concrete input values with {param} placeholders.

        Sensitive params are excluded on purpose: a passcode must not appear in
        the artifact even as evidence of where it was substituted, and it never
        shows up in a URL or on screen anyway.
        """
        if not text:
            return text
        out = text
        for value, param in self._subs:
            if any(h in param.lower() for h in _SENSITIVE_HINTS):
                continue
            if len(value) >= 3 and value in out:
                out = out.replace(value, "{" + param + "}")
        return out


def load_outcome_library(path: str) -> Dict[str, Any]:
    if not path or not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _path_of(url: Optional[str]) -> str:
    if not url:
        return ""
    if url.startswith("http"):
        return urlparse(url).path or "/"
    return url


def _looks_numeric(value: str) -> bool:
    if not value:
        return False
    try:
        float(str(value).replace(",", "").replace("$", ""))
        return True
    except ValueError:
        return False


def _dedupe(strategies: List[Dict[str, str]]) -> List[Dict[str, str]]:
    seen = set()
    out = []
    for s in strategies:
        key = json.dumps(s, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out
