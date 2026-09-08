#!/usr/bin/env python3
"""
Mock USSD Gateway Driver
Cloud Interactive Media Group - Engineering Exercise

This is the USSD gateway. It does not run as a service: it drives YOUR service
by POSTing session callbacks at it, exactly as an operator gateway or aggregator
would, and prints what came back.

    python driver.py --base-url http://localhost:8000 --scenario happy
    python driver.py --base-url http://localhost:8000 --scenario all

It behaves like a real gateway, which means it behaves badly. Read the scenarios.

--------------------------------------------------------------------------
REQUEST SHAPE (what we POST to {base}/ussd, form-encoded)

    sessionId    opaque, stable for the life of one session
    serviceCode  the short code dialled, e.g. *384*7000#
    phoneNumber  MSISDN in E.164, e.g. +2348031234567
    text         the accumulated user input, '*'-delimited.
                 First callback of a session sends text=''
                 then '1', then '1*500', then '1*500*1', and so on.

RESPONSE SHAPE (plain text, what your service returns)

    CON <message>   keep the session open and show <message>
    END <message>   terminate the session and show <message>

Anything else, a non-200, or a response slower than 8 seconds is treated by the
gateway as a failed session and the subscriber sees a network error.
--------------------------------------------------------------------------
"""

import argparse
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

SERVICE_CODE = "*384*7000#"
MSISDN = "+2348031234567"
TIMEOUT = 12


def post(base, session_id, text, msisdn=MSISDN):
    data = urllib.parse.urlencode({
        "sessionId": session_id,
        "serviceCode": SERVICE_CODE,
        "phoneNumber": msisdn,
        "text": text,
    }).encode()
    req = urllib.request.Request(
        f"{base}/ussd", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read().decode("utf-8", "replace").strip(), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace").strip(), time.time() - t0
    except Exception as e:
        return 0, f"<transport error: {e}>", time.time() - t0


def show(label, status, body, dt):
    kind = "CON" if body.startswith("CON") else "END" if body.startswith("END") else "??"
    print(f"    {label:<32} [{status}] {kind} {dt*1000:6.0f}ms  {body[:88]}")


def sid():
    return "ATUid_" + uuid.uuid4().hex[:16]


def sc_happy(base):
    """A subscriber walks the menu and buys airtime. Nothing goes wrong."""
    s = sid()
    for label, text in [("dial", ""), ("select airtime", "1"),
                        ("enter amount 500", "1*500"), ("confirm", "1*500*1")]:
        show(label, *post(base, s, text))
    print("    EXPECT: CON responses through the menu, then one END confirming the vend.")


def sc_gateway_retry(base):
    """The gateway did not see our response in time and re-sends the SAME
    callback. This is routine. It must not vend twice."""
    s = sid()
    for label, text in [("dial", ""), ("select airtime", "1"), ("amount 500", "1*500")]:
        show(label, *post(base, s, text))
    print("    -- gateway re-sends the confirm callback three times --")
    for i in range(3):
        show(f"confirm (delivery {i+1})", *post(base, s, "1*500*1"))
    print("    EXPECT: exactly ONE vend on the ledger for this session.")


def sc_double_tap(base):
    """Subscriber mashes the confirm key. Two identical callbacks arrive
    concurrently on the same session."""
    s = sid()
    for text in ["", "1", "1*500"]:
        post(base, s, text)
    print("    -- two concurrent confirm callbacks on one session --")
    with ThreadPoolExecutor(max_workers=2) as ex:
        for i, res in enumerate(ex.map(lambda _: post(base, s, "1*500*1"), range(2))):
            show(f"concurrent confirm {i+1}", *res)
    print("    EXPECT: exactly ONE vend. A race here debits a real subscriber twice.")


def sc_abandoned(base):
    """Subscriber dials, starts, and walks away. The session must expire and
    must NOT leave a half-built transaction behind."""
    s = sid()
    for label, text in [("dial", ""), ("select airtime", "1")]:
        show(label, *post(base, s, text))
    print("    -- subscriber abandons; no further callbacks ever arrive --")
    print("    EXPECT: no ledger entry, and session state that expires rather than leaking.")


def sc_resume(base):
    """The gateway drops the session; the subscriber redials with a NEW session
    id from the same MSISDN. Fresh menu, no leaked state."""
    s1 = sid()
    for text in ["", "1", "1*500"]:
        post(base, s1, text)
    print("    -- session dropped mid-flow; subscriber redials --")
    show("redial (new sessionId)", *post(base, sid(), ""))
    print("    EXPECT: a fresh main menu, NOT the amount-confirmation step.")


def sc_out_of_order(base):
    """A confirm callback arrives for a session that never selected an amount.
    Gateways do this. Your state machine must reject it, not crash."""
    s = sid()
    show("dial", *post(base, s, ""))
    show("confirm out of order", *post(base, s, "1*500*1"))
    print("    EXPECT: a clean rejection or re-prompt. Never a 500, never a vend.")


def sc_bad_input(base):
    """Garbage in the menu path."""
    s = sid()
    post(base, s, "")
    for label, text in [("invalid menu option", "9"), ("non-numeric amount", "1*abc"),
                        ("negative amount", "1*-500"), ("absurd amount", "1*99999999")]:
        show(label, *post(base, s, text))
    print("    EXPECT: CON re-prompts or a clean END. No 500s.")


def sc_slow(base):
    """The operator is slow today. Your USSD response must still land inside the
    gateway window: the vend cannot block the session response."""
    s = sid()
    for text in ["", "1", "1*500"]:
        post(base, s, text)
    st, b, dt = post(base, s, "1*500*1")
    show("confirm (timed)", st, b, dt)
    if dt > 8:
        print(f"    FAIL: {dt:.1f}s exceeds the 8s gateway window. The subscriber saw an error.")
    else:
        print(f"    PASS: responded in {dt:.1f}s, inside the 8s gateway window.")


SCENARIOS = {"happy": sc_happy, "gateway-retry": sc_gateway_retry,
             "double-tap": sc_double_tap, "abandoned": sc_abandoned,
             "resume": sc_resume, "out-of-order": sc_out_of_order,
             "bad-input": sc_bad_input, "slow": sc_slow}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--scenario", default="happy",
                    help="one of: " + ", ".join(SCENARIOS) + ", or 'all'")
    a = ap.parse_args()
    base = a.base_url.rstrip("/")
    names = list(SCENARIOS) if a.scenario == "all" else [a.scenario]
    for n in names:
        if n not in SCENARIOS:
            print(f"unknown scenario: {n}", file=sys.stderr); sys.exit(2)
        print(f"\n=== {n}\n    {SCENARIOS[n].__doc__.strip()}")
        SCENARIOS[n](base)
    print()
