"""
Synthetic member/account data for the stand-in servicing app.

Everything here is fabricated. No real names, no real account numbers, no
real PII. The shapes mimic what a core banking servicing screen exposes so
that the redaction layer (`cua.safety.redaction`) has realistic material to
work against -- member numbers, SSN-shaped tax IDs, and balances.
"""
from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Account:
    number: str
    kind: str          # "Savings" | "Checking" | "Certificate"
    balance: float
    status: str = "Open"


@dataclass
class Member:
    number: str
    name: str
    tax_id: str        # SSN-shaped -- deliberately present so redaction is testable
    status: str        # "Active" | "Restricted" | "Closed"
    branch: str
    accounts: List[Account] = field(default_factory=list)

    def account_of_kind(self, kind: str) -> Optional[Account]:
        for a in self.accounts:
            if a.kind.lower() == kind.lower():
                return a
        return None


@dataclass
class Operator:
    username: str
    password: str
    display_name: str
    # Entitlements gate what the operator may do. `teller` cannot open
    # sub-accounts -- that surfaces as a *business outcome* on replay, not a
    # crash, which is precisely the distinction the brief cares about.
    entitlements: List[str] = field(default_factory=list)


OPERATORS: Dict[str, Operator] = {
    "svc_admin": Operator(
        username="svc_admin",
        password="sandbox-only-pw",
        display_name="S. Admin",
        entitlements=["member.read", "subaccount.open"],
    ),
    "teller01": Operator(
        username="teller01",
        password="sandbox-only-pw",
        display_name="T. Eller",
        entitlements=["member.read"],
    ),
}


def _seed_members() -> Dict[str, Member]:
    return {
        m.number: m
        for m in [
            Member(
                number="100237",
                name="Dana Whitfield",
                tax_id="521-44-9087",
                status="Active",
                branch="Cambridge Main",
                accounts=[
                    Account("SV-100237-01", "Savings", 8421.55),
                    Account("CK-100237-01", "Checking", 1290.04),
                ],
            ),
            Member(
                number="100412",
                name="Marcus Ollivant",
                tax_id="410-82-3315",
                status="Active",
                branch="Somerville",
                accounts=[
                    Account("SV-100412-01", "Savings", 342.10),
                ],
            ),
            Member(
                number="100999",
                name="Priya Raghunathan",
                tax_id="330-77-1204",
                # Restricted profiles are a legitimate business outcome:
                # the caller needs to know, it is not an automation failure.
                status="Restricted",
                branch="Downtown",
                accounts=[
                    Account("SV-100999-01", "Savings", 0.00, status="Frozen"),
                ],
            ),
        ]
    }


class Store:
    """In-memory data store. Reset between runs via `reset()` so that replay
    evidence is reproducible and sub-account creation is not cumulative."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.members: Dict[str, Member] = _seed_members()
        self._seq = itertools.count(1)

    def reset(self) -> None:
        with self._lock:
            self.members = _seed_members()
            self._seq = itertools.count(1)

    def get_member(self, number: str) -> Optional[Member]:
        return self.members.get((number or "").strip())

    def open_subaccount(self, member: Member, kind: str, deposit: float) -> Account:
        with self._lock:
            n = next(self._seq)
            prefix = {"savings": "SV", "checking": "CK", "certificate": "CD"}.get(kind.lower(), "XX")
            account = Account(
                number=f"{prefix}-{member.number}-{n + 1:02d}",
                kind=kind.title(),
                balance=deposit,
            )
            member.accounts.append(account)
            return account


# Branch-level policy: deposits above this need a supervisor, which the app
# surfaces as an inline validation error.
BRANCH_DEPOSIT_LIMIT = 10_000.00

ACCOUNT_TYPES = ["Savings", "Checking", "Certificate"]
