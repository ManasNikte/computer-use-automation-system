"""Guardrails: allowlist enforcement, risk gating, and redaction."""
from __future__ import annotations

import pytest

from cua.artifact.schema import RiskLevel
from cua.safety.policy import ConfirmationRequired, Policy, PolicyViolation, default_policy
from cua.safety.redaction import MASK, Redactor


# -- allowlist --------------------------------------------------------------
def test_allows_approved_origin_and_path():
    policy = default_policy("http://127.0.0.1:5075")
    policy.check_location("http://127.0.0.1:5075/members/100237")


def test_blocks_a_different_origin():
    policy = default_policy("http://127.0.0.1:5075")
    with pytest.raises(PolicyViolation) as exc:
        policy.check_location("https://evil.example.com/members/100237")
    assert "allowlist" in str(exc.value)


def test_blocks_a_different_port_on_the_same_host():
    """Two tenants on one box differ only by port; that has to be enough."""
    policy = default_policy("http://127.0.0.1:5075")
    with pytest.raises(PolicyViolation):
        policy.check_location("http://127.0.0.1:5076/members/100237")


def test_denied_paths_win_over_allowed_ones():
    """The fault-injection control plane lives on an allowed host and must
    still be unreachable from any capability."""
    policy = default_policy("http://127.0.0.1:5075")
    with pytest.raises(PolicyViolation) as exc:
        policy.check_location("http://127.0.0.1:5075/_control/reset")
    assert "denied" in str(exc.value)


def test_blocks_a_path_outside_the_allowed_globs():
    policy = default_policy("http://127.0.0.1:5075")
    with pytest.raises(PolicyViolation):
        policy.check_location("http://127.0.0.1:5075/admin/wire-transfers")


def test_about_blank_is_permitted_because_that_is_where_a_context_starts():
    default_policy("http://127.0.0.1:5075").check_location("about:blank")


def test_blocks_disallowed_action_types():
    policy = Policy(allowed_actions=["navigate", "click", "extract"])
    policy.check_action("click")
    with pytest.raises(PolicyViolation) as exc:
        policy.check_action("fill")
    assert "not permitted" in str(exc.value)


# -- risk gate --------------------------------------------------------------
def test_safe_and_reversible_actions_pass_both_modes():
    policy = default_policy("http://127.0.0.1:5075")
    for mode in ("discovery", "replay"):
        policy.check_risk(RiskLevel.SAFE, mode=mode)
        policy.check_risk(RiskLevel.REVERSIBLE, mode=mode)


def test_irreversible_action_needs_a_human_during_discovery():
    policy = default_policy("http://127.0.0.1:5075")
    with pytest.raises(ConfirmationRequired):
        policy.check_risk(RiskLevel.IRREVERSIBLE, mode="discovery", context="submit")


def test_irreversible_replay_is_blocked_while_the_capability_is_a_draft():
    policy = default_policy("http://127.0.0.1:5075")
    with pytest.raises(ConfirmationRequired) as exc:
        policy.check_risk(RiskLevel.IRREVERSIBLE, mode="replay", capability_approved=False)
    assert "approved" in str(exc.value)


def test_irreversible_replay_is_allowed_once_the_capability_is_approved():
    policy = default_policy("http://127.0.0.1:5075")
    policy.check_risk(RiskLevel.IRREVERSIBLE, mode="replay", capability_approved=True)


def test_a_policy_can_forbid_irreversible_replay_outright():
    policy = Policy(allow_irreversible_in_replay=False)
    with pytest.raises(PolicyViolation):
        policy.check_risk(RiskLevel.IRREVERSIBLE, mode="replay", capability_approved=True)


def test_policy_survives_a_json_round_trip():
    original = default_policy("http://127.0.0.1:5075")
    assert Policy.from_dict(original.to_dict()).to_dict() == original.to_dict()


# -- redaction --------------------------------------------------------------
def test_masks_registered_secrets_anywhere_they_appear():
    redactor = Redactor()
    redactor.register_secret("sandbox-only-pw")
    assert "sandbox-only-pw" not in redactor.redact("signing in with sandbox-only-pw now")


def test_ignores_secrets_too_short_to_mask_safely():
    """Masking every occurrence of a 2-character string destroys the log and
    protects nothing."""
    redactor = Redactor()
    redactor.register_secret("ab")
    assert redactor.redact("about") == "about"


def test_masks_regulated_data_scraped_off_the_screen():
    """The realistic leak path: a tax ID we never passed in, read off a
    member record and written into page text we logged."""
    redactor = Redactor()
    out = redactor.redact("Name: Dana Whitfield  Tax ID: 521-44-9087  Acct: SV-100237-01")
    assert "521-44-9087" not in out
    assert "SV-100237-01" not in out


def test_masks_values_under_sensitive_keys_regardless_of_shape():
    redactor = Redactor()
    out = redactor.redact_obj({"operator_id": "svc_admin", "passcode": "wildebeest"})
    assert out["passcode"] == MASK
    assert out["operator_id"] == "svc_admin"


def test_redacts_recursively_through_nested_structures():
    redactor = Redactor()
    out = redactor.redact_obj({"steps": [{"note": "tax id 521-44-9087"}]})
    assert "521-44-9087" not in str(out)


def test_redact_params_masks_what_the_capability_declared_sensitive():
    redactor = Redactor()
    out = redactor.redact_params(
        {"member_id": "100237", "passcode": "pw1234"}, sensitive_names=["passcode"])
    assert out["passcode"] == MASK
    assert out["member_id"] == "100237"


def test_non_string_values_pass_through_untouched():
    redactor = Redactor()
    assert redactor.redact_obj({"count": 3, "ok": True}) == {"count": 3, "ok": True}
