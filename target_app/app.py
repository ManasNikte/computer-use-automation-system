"""
MeridianCore Servicing -- a stand-in for the kind of back-office application
this system is built to drive.

This is deliberately *not* a clean modern web app. It is built to reproduce
the properties the brief calls out about real bank/credit-union back offices:

  * **Frameset chrome.** The nav and the working area are separate frames.
    Automation that assumes a single document breaks immediately.
  * **Table-based layout, no test IDs, no semantic classes.** Element
    identity has to be derived from accessible role + name, or from
    structural anchoring ("the button in the row whose first cell reads
    <member number>"), because there is nothing else to hold on to.
  * **Real runtime error states, not layout drift.** The UI is stable; what
    varies at runtime is the *outcome*: record not found, permission denied,
    validation error, session expiry, a maintenance interstitial, a slow
    load, an app error. These are injectable so replay's error taxonomy is
    exercisable and reproducible.
  * **Multi-tenant.** The same code serves two branded "installs" of the same
    vendor product (see tenants.py).

Everything in it is synthetic. No real credentials, no real PII.

Run:
    python target_app/app.py --port 5075 --tenant meridian
    python target_app/app.py --port 5076 --tenant summit
"""
from __future__ import annotations

import argparse
import time

from flask import (
    Flask,
    Response,
    redirect,
    render_template,
    request,
    session,
)

from data import ACCOUNT_TYPES, BRANCH_DEPOSIT_LIMIT, OPERATORS, Store
from tenants import get_tenant

app = Flask(__name__)
app.secret_key = "sandbox-only-not-a-secret"  # local demo app; no real sessions

STORE = Store()

# --------------------------------------------------------------------------
# Injectable runtime conditions.
#
# These are the whole reason this app exists rather than pointing at a public
# demo site: they let a replay run deterministically hit each branch of the
# error taxonomy. Set via POST /_control/inject (see cua.cli --inject).
# --------------------------------------------------------------------------
INJECT = {
    "slow": False,            # add latency -> recoverable (wait/retry)
    "interstitial": False,    # maintenance notice -> recoverable (dismiss)
    "session_timeout": False, # expire the session once -> recoverable (re-auth)
    "app_error": False,       # 500 on the next detail view -> hard failure
}
_ONESHOT = {"session_timeout": False, "app_error": False}


def tenant():
    return app.config["TENANT"]


def ctx(**kw):
    t = tenant()
    base = dict(t=t, tenant=t, account_types=ACCOUNT_TYPES,
                operator=session.get("operator_name", ""))
    base.update(kw)
    return base


def _maybe_slow() -> None:
    if INJECT["slow"]:
        time.sleep(3.0)


def _session_expired() -> bool:
    """Fire the session-timeout injection exactly once, so that a replay can
    demonstrate detecting it, re-authenticating, and carrying on."""
    if INJECT["session_timeout"] and not _ONESHOT["session_timeout"]:
        _ONESHOT["session_timeout"] = True
        session.clear()
        return True
    return "operator" not in session


def require_login():
    if _session_expired():
        return redirect(tenant().path("/login") + "?reason=expired")
    return None


# --------------------------------------------------------------------------
# Control plane (not part of the "application" -- the test harness seam)
# --------------------------------------------------------------------------
@app.post("/_control/inject")
def control_inject():
    payload = request.get_json(silent=True) or {}
    for k in INJECT:
        if k in payload:
            INJECT[k] = bool(payload[k])
    for k in _ONESHOT:
        _ONESHOT[k] = False
    return {"ok": True, "inject": INJECT}


@app.post("/_control/reset")
def control_reset():
    STORE.reset()
    for k in INJECT:
        INJECT[k] = False
    for k in _ONESHOT:
        _ONESHOT[k] = False
    session.clear()
    return {"ok": True}


@app.get("/_control/health")
def control_health():
    return {"ok": True, "tenant": tenant().id, "product_version": tenant().product_version}


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
def _route(suffix: str) -> str:
    return tenant().path(suffix)


@app.get("/login")
@app.get("/servicing/login")
def login_form():
    reason = request.args.get("reason", "")
    banner = ""
    if reason == "expired":
        # Exact wording matters: replay detects this string to classify the
        # condition as recoverable (re-authenticate) rather than a failure.
        banner = "Your session has expired. Please sign in again."
    return render_template("login.html", **ctx(banner=banner, error=""))


@app.post("/login")
@app.post("/servicing/login")
def login_submit():
    username = (request.form.get("operator_id") or "").strip()
    password = request.form.get("passcode") or ""
    op = OPERATORS.get(username)
    if op is None or op.password != password:
        return render_template(
            "login.html",
            **ctx(banner="", error="Sign-in failed. The operator ID or passcode is not recognized."),
        )
    session["operator"] = op.username
    session["operator_name"] = op.display_name
    session["entitlements"] = op.entitlements
    return redirect(_route("/"))


@app.get("/logout")
@app.get("/servicing/logout")
def logout():
    session.clear()
    return redirect(_route("/login"))


# --------------------------------------------------------------------------
# Frameset chrome
# --------------------------------------------------------------------------
@app.get("/")
@app.get("/servicing/")
@app.get("/servicing")
def home():
    guard = require_login()
    if guard:
        return guard
    return render_template("frameset.html", **ctx())


@app.get("/nav")
@app.get("/servicing/nav")
def nav():
    return render_template("nav.html", **ctx())


# --------------------------------------------------------------------------
# Member search -> detail -> open sub-account -> review -> confirmation
# --------------------------------------------------------------------------
@app.get("/members/search")
@app.get("/servicing/members/search")
def member_search_form():
    guard = require_login()
    if guard:
        return guard
    _maybe_slow()
    return render_template(
        "search.html",
        **ctx(results=None, message="", interstitial=INJECT["interstitial"]),
    )


@app.post("/members/search")
@app.post("/servicing/members/search")
def member_search_submit():
    guard = require_login()
    if guard:
        return guard
    _maybe_slow()
    number = (request.form.get("member_no") or "").strip()
    member = STORE.get_member(number)
    if member is None:
        # A legitimate answer the caller needs -- NOT an error.
        return render_template(
            "search.html",
            **ctx(results=[], message="No member matching that number was found.",
                  interstitial=False, query=number),
        )
    return render_template(
        "search.html",
        **ctx(results=[member], message="", interstitial=False, query=number),
    )


@app.get("/members/<number>")
@app.get("/servicing/members/<number>")
def member_detail(number: str):
    guard = require_login()
    if guard:
        return guard
    _maybe_slow()

    if INJECT["app_error"] and not _ONESHOT["app_error"]:
        _ONESHOT["app_error"] = True
        return Response(
            render_template("error500.html", **ctx()),
            status=500,
        )

    member = STORE.get_member(number)
    if member is None:
        return render_template(
            "search.html",
            **ctx(results=[], message="No member matching that number was found.",
                  interstitial=False, query=number),
        )
    restricted_notice = ""
    if member.status == "Restricted":
        restricted_notice = (
            "This member profile is restricted. Servicing actions are unavailable."
        )
    # Note what is NOT checked here: the operator's entitlement. The button is
    # rendered for everyone and the entitlement is enforced server-side when
    # it is used. That is deliberate and it mirrors how these systems actually
    # behave -- the permission model lives in the back end, and the UI happily
    # offers actions the signed-in operator turns out not to be allowed to
    # take. It is also the more interesting case for this project: the refusal
    # arrives as a screen mid-flow, which is exactly the kind of runtime
    # condition replay has to classify as a business outcome rather than
    # breakage.
    return render_template(
        "member.html",
        **ctx(member=member, restricted_notice=restricted_notice,
              can_open=(member.status == "Active")),
    )


@app.get("/members/<number>/subaccount")
@app.get("/servicing/members/<number>/subaccount")
def subaccount_form(number: str):
    guard = require_login()
    if guard:
        return guard
    member = STORE.get_member(number)
    if member is None:
        return redirect(_route("/members/search"))
    if "subaccount.open" not in session.get("entitlements", []):
        # Permission denial: again, a business outcome the caller must be
        # told about, distinct from "the automation broke".
        return render_template(
            "member.html",
            **ctx(member=member, restricted_notice="",
                  can_open=False,
                  denied="You are not authorized to open sub-accounts. "
                         "Contact a branch supervisor."),
        )
    return render_template(
        "subaccount_form.html",
        **ctx(member=member, error="", values={}),
    )


@app.post("/members/<number>/subaccount")
@app.post("/servicing/members/<number>/subaccount")
def subaccount_review(number: str):
    guard = require_login()
    if guard:
        return guard
    member = STORE.get_member(number)
    if member is None:
        return redirect(_route("/members/search"))

    kind = (request.form.get("acct_type") or "").strip()
    raw_amount = (request.form.get("deposit_amt") or "").strip()
    values = {"acct_type": kind, "deposit_amt": raw_amount}

    def invalid(msg: str):
        return render_template("subaccount_form.html", **ctx(member=member, error=msg, values=values))

    try:
        amount = float(raw_amount.replace(",", "").replace("$", ""))
    except ValueError:
        return invalid("Initial deposit must be a numeric amount.")
    if amount < 0:
        return invalid("Initial deposit must be a numeric amount.")
    if amount > BRANCH_DEPOSIT_LIMIT:
        # Validation error surfaced inline -- the classic "expected business
        # outcome that looks like a stuck page" case.
        return invalid(
            f"Initial deposit exceeds the branch limit of "
            f"${BRANCH_DEPOSIT_LIMIT:,.2f}. Supervisor approval is required."
        )
    if kind not in ACCOUNT_TYPES:
        return invalid("Select an account type.")

    return render_template(
        "subaccount_review.html",
        **ctx(member=member, kind=kind, amount=amount),
    )


@app.post("/members/<number>/subaccount/confirm")
@app.post("/servicing/members/<number>/subaccount/confirm")
def subaccount_confirm(number: str):
    guard = require_login()
    if guard:
        return guard
    member = STORE.get_member(number)
    if member is None:
        return redirect(_route("/members/search"))
    if "subaccount.open" not in session.get("entitlements", []):
        return render_template(
            "member.html",
            **ctx(member=member, restricted_notice="", can_open=False,
                  denied="You are not authorized to open sub-accounts. "
                         "Contact a branch supervisor."),
        )
    kind = request.form.get("acct_type") or "Savings"
    amount = float(request.form.get("deposit_amt") or 0)
    account = STORE.open_subaccount(member, kind, amount)
    return render_template(
        "subaccount_complete.html",
        **ctx(member=member, account=account),
    )


@app.get("/maintenance/ack")
@app.get("/servicing/maintenance/ack")
def maintenance_ack():
    INJECT["interstitial"] = False
    return redirect(_route("/members/search"))


@app.template_filter("money")
def money(v) -> str:
    return f"${float(v):,.2f}"


def main() -> None:
    parser = argparse.ArgumentParser(description="MeridianCore Servicing stand-in")
    parser.add_argument("--port", type=int, default=5075)
    parser.add_argument("--tenant", default="meridian", choices=["meridian", "summit"])
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    app.config["TENANT"] = get_tenant(args.tenant)
    print(f"MeridianCore Servicing [{args.tenant}] -> http://{args.host}:{args.port}"
          f"{app.config['TENANT'].prefix}/login")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
