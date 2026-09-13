"""Recorder: locator ranking, checkpoint inference, and parameterization.

These are the decisions that determine whether a recorded capability works for
any record or only for the one it happened to be recorded against, so they get
tested directly rather than only through an end-to-end run.
"""
from __future__ import annotations

from cua.agent.loop import DiscoveryResult, TraceStep
from cua.artifact.recorder import Recorder
from cua.artifact.schema import RiskLevel

PARAMS = {
    "operator_id": "svc_admin",
    "passcode": "sandbox-only-pw",
    "member_id": "100237",
    "account_type": "Savings",
    "initial_deposit": "50",
}


def recorder() -> Recorder:
    return Recorder(params=PARAMS, tenant_id="meridian", vendor_product="meridiancore",
                    product_version="8.2.1", base_url="http://127.0.0.1:5075")


def record(trace, final_location="", final_title="", outputs=None):
    result = DiscoveryResult(
        status="completed", trace=trace, outputs=outputs or {},
        final_location=final_location, final_title=final_title, steps_taken=len(trace),
    )
    return recorder().record(
        result, capability_id="cap", name="Cap", description="test",
        goal="test goal", entry_path="/login", run_id="run", model_id="test-model",
    )


def field_step(**kw) -> TraceStep:
    base = dict(index=0, action="click", role="button", name="Go", hints={},
                risk=RiskLevel.REVERSIBLE.value, location_before="http://h/a",
                location_after="http://h/a", title_before="A", title_after="A",
                text_before="", text_after="")
    base.update(kw)
    return TraceStep(**base)


# -- locator ranking --------------------------------------------------------
def test_row_anchored_strategy_ranks_first_when_the_anchor_is_parameterizable():
    """In a results grid every row holds a control named "Open". Addressing it
    by role+name alone is not fragile, it is *wrong* -- it picks row one no
    matter which record was searched for. So the row anchor must lead."""
    cap = record([field_step(
        role="link", name="Open",
        hints={"frame": "name=mainframe", "name_source": "text", "tag": "a",
               "row_anchor": "100237", "href": "/members/100237",
               "css": 'a[href="/members/100237"]'},
    )])
    strategies = cap.steps[0].locator.strategies
    assert strategies[0]["kind"] == "row_role_name"
    assert strategies[0]["row"] == "{member_id}"
    assert strategies[1]["kind"] == "role_name"


def test_role_name_ranks_first_for_an_ordinary_control():
    cap = record([field_step(
        role="textbox", name="Member Number",
        hints={"frame": "name=mainframe", "name_source": "aria-label", "tag": "input",
               "name_attr": "member_no", "css": 'input[name="member_no"]'},
    )])
    strategies = cap.steps[0].locator.strategies
    assert strategies[0]["kind"] == "role_name"
    assert strategies[0]["name"] == "Member Number"
    assert [s["kind"] for s in strategies[1:]] == ["attr", "css"]


def test_role_name_is_skipped_when_the_platform_would_not_agree_on_the_name():
    """A name we recovered from an adjacent table cell reads well to a human
    but is not the browser's accessible name, so a role+name locator would
    never resolve. Emitting it as the primary strategy would guarantee a
    fallback on every single replay."""
    cap = record([field_step(
        role="textbox", name="Branch Code",
        hints={"frame": "", "name_source": "adjacent-cell", "tag": "input",
               "name_attr": "branch_cd", "css": 'input[name="branch_cd"]'},
    )])
    kinds = [s["kind"] for s in cap.steps[0].locator.strategies]
    assert "role_name" not in kinds
    assert kinds[0] == "attr"


def test_every_strategy_carries_the_frame_it_was_recorded_in():
    cap = record([field_step(
        role="button", name="Search",
        hints={"frame": "name=mainframe", "name_source": "value", "tag": "input",
               "css": 'input[value="Search"]'},
    )])
    assert all(s["frame"] == "name=mainframe" for s in cap.steps[0].locator.strategies)


def test_strategies_are_deduplicated():
    cap = record([field_step(
        role="button", name="Search",
        hints={"frame": "", "name_source": "value", "tag": "input",
               "row_anchor": "Search", "css": 'input[value="Search"]'},
    )])
    serialized = [tuple(sorted(s.items())) for s in cap.steps[0].locator.strategies]
    assert len(serialized) == len(set(serialized))


# -- checkpoint inference ---------------------------------------------------
def test_infers_a_url_checkpoint_when_the_path_changes():
    cap = record([field_step(location_before="http://h/members/search",
                             location_after="http://h/members/100237")])
    cp = cap.steps[0].checkpoint
    assert cp.kind == "url_contains"
    assert cp.value == "/members/{member_id}"


def test_infers_a_text_checkpoint_from_a_value_that_newly_appeared():
    """A search POSTs to the same path it came from, so the URL says nothing.
    What it does do is put the number we typed on screen."""
    cap = record([field_step(
        location_before="http://h/members/search", location_after="http://h/members/search",
        text_before="Member Search", text_after="Member Search 100237 Dana Whitfield",
    )])
    cp = cap.steps[0].checkpoint
    assert cp.kind == "text_present"
    assert cp.value == "{member_id}"


def test_falls_back_to_the_screen_title():
    cap = record([field_step(
        location_before="http://h/x", location_after="http://h/x",
        title_before="Open Sub-Account", title_after="Review Sub-Account Request",
    )])
    cp = cap.steps[0].checkpoint
    assert cp.kind == "text_present"
    assert cp.value == "Review Sub-Account Request"


def test_emits_no_checkpoint_when_a_step_has_no_observable_effect():
    """Typing into a field changes nothing a checkpoint could assert.
    Fabricating one would make replay verify something that was never true."""
    cap = record([field_step(action="fill", role="textbox", name="Passcode",
                             param="passcode",
                             hints={"name_source": "aria-label", "tag": "input"})])
    assert cap.steps[0].checkpoint is None


def test_prefers_a_semantic_final_checkpoint_over_a_url():
    cap = record(
        [field_step(location_before="http://h/a", location_after="http://h/b"),
         TraceStep(index=1, action="extract", output_name="new_account_number",
                   extract_label="New Account Number")],
        final_location="http://h/members/100237/subaccount/confirm",
    )
    assert cap.final_checkpoint.kind == "text_present"
    assert cap.final_checkpoint.value == "New Account Number"


# -- parameterization -------------------------------------------------------
def test_never_writes_a_sensitive_value_into_the_artifact():
    cap = record([field_step(action="fill", role="textbox", name="Passcode",
                             param="passcode",
                             hints={"name_source": "aria-label", "tag": "input"})])
    assert "sandbox-only-pw" not in str(cap.to_dict())
    assert cap.steps[0].param == "passcode"
    assert cap.steps[0].literal is None


def test_marks_credential_inputs_sensitive_and_omits_their_example():
    cap = record([field_step(action="fill", role="textbox", name="Passcode", param="passcode",
                             hints={"name_source": "aria-label", "tag": "input"})])
    passcode = cap.input("passcode")
    assert passcode.sensitive is True
    assert passcode.example == ""


def test_types_numeric_inputs_from_the_value_supplied():
    cap = record([field_step(action="fill", role="textbox", name="Initial Deposit",
                             param="initial_deposit",
                             hints={"name_source": "aria-label", "tag": "input"})])
    assert cap.input("initial_deposit").type.value == "number"


def test_drops_params_the_flow_never_used():
    """Advertising an input the capability ignores is a lie in the function
    signature a calling agent depends on."""
    cap = record([field_step(action="fill", role="textbox", name="Member Number",
                             param="member_id",
                             hints={"name_source": "aria-label", "tag": "input"})])
    assert [i.name for i in cap.inputs] == ["member_id"]


def test_generalises_the_description_away_from_the_recorded_record():
    result = DiscoveryResult(status="completed", trace=[field_step()], steps_taken=1)
    cap = recorder().record(
        result, capability_id="cap", name="Cap",
        description="Open a Savings sub-account for member 100237",
        goal="g", entry_path="/login", run_id="r", model_id="m")
    assert "100237" not in cap.description
    assert "{member_id}" in cap.description


def test_marks_regulated_outputs_for_redaction_in_evidence():
    cap = record([TraceStep(index=0, action="extract", output_name="new_account_number",
                            extract_label="New Account Number")])
    assert cap.outputs[0].redact_in_evidence is True


def test_flags_the_artifact_when_a_human_took_over_during_discovery():
    """A reviewer must not be shown a human-assisted recording as if a model
    had found the whole flow unaided."""
    result = DiscoveryResult(status="completed", trace=[field_step(by_operator=True)],
                             human_intervened=True, steps_taken=1)
    cap = recorder().record(result, capability_id="c", name="C", description="d",
                            goal="g", entry_path="/login", run_id="r", model_id="m")
    assert cap.provenance.human_intervened is True
    assert "human operator" in cap.steps[0].notes


def test_records_which_model_discovered_the_capability():
    cap = record([field_step()])
    assert cap.provenance.discovered_by == "test-model"
    assert cap.status == "draft"


def test_attaches_the_applications_declared_outcomes_and_recovery_rules():
    """The recorder must not invent these -- a happy-path run never observes
    'no such member' -- so they come from the per-application library."""
    result = DiscoveryResult(status="completed", trace=[field_step()], steps_taken=1)
    cap = recorder().record(
        result, capability_id="c", name="C", description="d", goal="g",
        entry_path="/login", run_id="r", model_id="m",
        outcome_library={
            "version_range": ">=8.0 <9.0",
            "business_outcomes": [{"name": "member_not_found",
                                   "detect": {"kind": "text_present", "value": "No member"},
                                   "description": "nope"}],
            "recovery_rules": [{"name": "retry", "detect": {"kind": "on_timeout", "value": ""},
                                "action": {"kind": "retry_step"}}],
        })
    assert [b.name for b in cap.business_outcomes] == ["member_not_found"]
    assert [r.name for r in cap.recovery_rules] == ["retry"]
    assert cap.target.product_version_range == ">=8.0 <9.0"
