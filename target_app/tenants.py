"""
Tenant configuration for the MeridianCore stand-in.

The whole point of this file is to model the reality described in the brief:
hundreds of institutions run *the same vendor product*, branded, re-labelled
and routed differently. The underlying screens, field semantics and business
rules are identical -- what differs is cosmetic and superficial-structural:
display labels, button captions, route prefixes, and chrome.

`cua` capabilities are recorded against the vendor product (`meridiancore`,
major version 8) and carry per-tenant overrides for exactly these differences,
rather than being re-recorded per institution. See REPORT.md
"Heterogeneity & multi-tenant".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict


@dataclass(frozen=True)
class Tenant:
    id: str
    display_name: str
    # Vendor product identity -- what a capability is actually recorded against.
    vendor_product: str = "meridiancore"
    product_version: str = "8.2.1"
    # Route prefix. Tenant installs of the same product are often mounted at
    # different base paths behind the institution's reverse proxy.
    prefix: str = ""
    # Display-label overrides. Same field, different caption per institution.
    labels: Dict[str, str] = field(default_factory=dict)
    accent: str = "#1f3864"

    def label(self, key: str) -> str:
        return self.labels.get(key, _DEFAULT_LABELS[key])

    def path(self, suffix: str) -> str:
        return f"{self.prefix}{suffix}"


_DEFAULT_LABELS = {
    "member_number": "Member Number",
    "member_search_submit": "Search",
    "open_subaccount": "Open Sub-Account",
    "account_type": "Account Type",
    "initial_deposit": "Initial Deposit",
    "continue": "Continue",
    "submit_order": "Submit Request",
    "member_record": "Member Record",
    "search_heading": "Member Search",
}


TENANTS: Dict[str, Tenant] = {
    # Tenant A -- the "base" install a capability is discovered against.
    "meridian": Tenant(
        id="meridian",
        display_name="Meridian Credit Union",
        prefix="",
        labels={},
        accent="#1f3864",
    ),
    # Tenant B -- same vendor product, version 8.2.4, rebranded and remounted.
    # Deliberately differs in every dimension a naive recording would hardcode:
    # route prefix, field captions, button captions, and page chrome.
    "summit": Tenant(
        id="summit",
        display_name="Summit Federal Credit Union",
        product_version="8.2.4",
        prefix="/servicing",
        labels={
            "member_number": "Account Holder ID",
            "member_search_submit": "Find",
            "open_subaccount": "New Sub Account",
            "account_type": "Sub Account Type",
            "initial_deposit": "Opening Deposit",
            "continue": "Next",
            "submit_order": "Submit",
            "member_record": "Account Holder Record",
            "search_heading": "Account Holder Lookup",
        },
        accent="#7b2d26",
    ),
}


def get_tenant(tenant_id: str) -> Tenant:
    if tenant_id not in TENANTS:
        raise KeyError(f"Unknown tenant {tenant_id!r}. Known: {sorted(TENANTS)}")
    return TENANTS[tenant_id]
