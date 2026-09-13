"""Capability contract: round-tripping, validation, and the tool schema."""
from __future__ import annotations

import pytest

from cua.artifact.schema import (
    ActionType,
    BusinessOutcome,
    Capability,
    Condition,
    ExtractSpec,
    InputParam,
    Locator,
    OutputField,
    ParamType,
    RiskLevel,
    Step,
    TargetBinding,
    resolve_for_tenant,
)


def make_capability() -> Capability:
    return Capability(
        id="open_member_subaccount",
        name="Open Member Sub-Account",
        version=1,
        description="Open a sub-account for {member_id}",
        target=TargetBinding(vendor_product="meridiancore", entry_path="/login"),
        inputs=[
            InputParam("member_id", ParamType.STRING, True, "member number"),
            InputParam("passcode", ParamType.STRING, True, "passcode", sensitive=True),
            InputParam("initial_deposit", ParamType.NUMBER, True, "opening deposit"),
            InputParam("account_type", ParamType.STRING, True, "type",
                       enum=["Savings", "Checking"]),
        ],
        outputs=[OutputField("new_account_number", ParamType.STRING, "the new number",
                             source_step_id="s3", redact_in_evidence=True)],
        steps=[
            Step(
                id="s1", action=ActionType.FILL,
                locator=Locator([{"kind": "role_name", "role": "textbox",
                                  "name": "Member Number", "frame": "name=mainframe"}],
                                description="textbox 'Member Number'"),
                param="member_id",
                checkpoint=Condition("url_contains", "/members/{member_id}"),
                risk=RiskLevel.REVERSIBLE,
            ),
            Step(
                id="s2", action=ActionType.CLICK,
                locator=Locator([{"kind": "role_name", "role": "button",
                                  "name": "Submit Request", "frame": "name=mainframe"}],
                                description="button 'Submit Request'"),
                risk=RiskLevel.IRREVERSIBLE,
            ),
            Step(
                id="s3", action=ActionType.EXTRACT,
                extract=ExtractSpec("new_account_number", "labelled_value",
                                    label="New Account Number"),
            ),
        ],
        business_outcomes=[
            BusinessOutcome("member_not_found",
                            Condition("text_present", "No member matching that number"),
                            "no such member")
        ],
        final_checkpoint=Condition("text_present", "New Account Number"),
        overrides={"summit": {"base_path": "/servicing",
                              "labels": {"Member Number": "Account Holder ID",
                                         "Submit Request": "Submit"}}},
    )


def test_round_trips_through_json_without_loss():
    original = make_capability()
    restored = Capability.from_dict(original.to_dict())
    assert restored.to_dict() == original.to_dict()
    assert restored.steps[1].risk is RiskLevel.IRREVERSIBLE
    assert restored.steps[2].extract.label == "New Account Number"


def test_detects_irreversible_steps():
    assert make_capability().has_irreversible_step() is True


def test_sensitive_params_are_identifiable():
    assert make_capability().sensitive_param_names() == ["passcode"]


@pytest.mark.parametrize("params,expected_fragment", [
    ({}, "missing required input"),
    ({"member_id": "1", "passcode": "p", "initial_deposit": "abc",
      "account_type": "Savings"}, "must be a number"),
    ({"member_id": "1", "passcode": "p", "initial_deposit": "5",
      "account_type": "Brokerage"}, "must be one of"),
    ({"member_id": "1", "passcode": "p", "initial_deposit": "5",
      "account_type": "Savings", "surprise": "x"}, "unknown input"),
])
def test_rejects_calls_that_violate_the_contract(params, expected_fragment):
    problems = make_capability().validate_params(params)
    assert any(expected_fragment in p for p in problems), problems


def test_accepts_a_well_formed_call():
    assert make_capability().validate_params({
        "member_id": "100237", "passcode": "pw",
        "initial_deposit": "50", "account_type": "Savings"}) == []


def test_tool_schema_describes_the_contract_a_caller_needs():
    tool = make_capability().to_tool_schema()
    assert tool["name"] == "open_member_subaccount"
    assert set(tool["input_schema"]["required"]) == {
        "member_id", "passcode", "initial_deposit", "account_type"}
    assert tool["input_schema"]["properties"]["initial_deposit"]["type"] == "number"
    assert tool["input_schema"]["properties"]["account_type"]["enum"] == ["Savings", "Checking"]
    assert tool["returns"]["new_account_number"]["type"] == "string"
    # Irreversibility and declared outcomes must be visible to a caller.
    assert tool["metadata"]["irreversible"] is True
    assert "member_not_found" in tool["description"]


def test_sensitive_params_never_leak_an_example_value():
    cap = make_capability()
    cap.inputs[1].example = "hunter2"
    tool = cap.to_tool_schema()
    assert "examples" not in tool["input_schema"]["properties"]["passcode"]


# -- multi-tenant resolution -----------------------------------------------
def test_tenant_overrides_relabel_locators_and_reroute_paths():
    base = make_capability()
    summit = resolve_for_tenant(base, "summit")

    assert summit.steps[0].locator.strategies[0]["name"] == "Account Holder ID"
    assert summit.steps[1].locator.strategies[0]["name"] == "Submit"
    assert summit.steps[0].checkpoint.value == "/servicing/members/{member_id}"
    assert summit.target.entry_path == "/servicing/login"


def test_tenant_resolution_does_not_mutate_the_stored_artifact():
    base = make_capability()
    resolve_for_tenant(base, "summit")
    assert base.steps[0].locator.strategies[0]["name"] == "Member Number"
    assert base.target.entry_path == "/login"


def test_unknown_tenant_falls_back_to_the_base_recording():
    base = make_capability()
    assert resolve_for_tenant(base, "some-other-cu") is base


def test_parameter_placeholders_survive_relabelling():
    """Relabelling is a text substitution, so it must not eat `{member_id}`."""
    summit = resolve_for_tenant(make_capability(), "summit")
    assert "{member_id}" in summit.steps[0].checkpoint.value
