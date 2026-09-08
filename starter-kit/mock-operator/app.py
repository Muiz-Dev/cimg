"""
Mock Telecom Operator Airtime Vending API
Cloud Interactive Media Group - Engineering Exercise

This service imitates a real airtime vending partner, including its bad habits.
Behaviour is deterministic: the outcome of a vend is derived from a hash of the
client_ref you send, so the same client_ref always behaves the same way. This
means your tests are reproducible and our review is fair.

Nothing in this service needs to be modified. Treat it as a third party you do
not control.
"""

import hashlib
import os
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone, date
from io import StringIO
import csv

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

API_KEY = os.getenv("OPERATOR_API_KEY", "sk_test_operator_7f3a9c1e")
DB_PATH = os.getenv("OPERATOR_DB", "/data/operator.db")
TIMEOUT_DELAY_SECONDS = float(os.getenv("OPERATOR_TIMEOUT_DELAY", "8"))

app = FastAPI(title="Mock Operator Vending API", version="1.0.0")
_lock = threading.Lock()
_rate_window = {}


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vends (
                operator_ref TEXT PRIMARY KEY,
                client_ref   TEXT NOT NULL,
                msisdn       TEXT NOT NULL,
                network      TEXT NOT NULL,
                amount_minor INTEGER NOT NULL,
                status       TEXT NOT NULL,
                completed_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS attempts (
                client_ref TEXT PRIMARY KEY,
                count      INTEGER NOT NULL
            )
        """)
        conn.commit()


init_db()


def bucket(client_ref: str) -> int:
    h = hashlib.sha256(client_ref.encode("utf-8")).hexdigest()
    return int(h[:8], 16) % 100


def attempt_number(client_ref: str) -> int:
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT count FROM attempts WHERE client_ref = ?", (client_ref,)
        ).fetchone()
        n = (row["count"] if row else 0) + 1
        conn.execute(
            "INSERT INTO attempts (client_ref, count) VALUES (?, ?) "
            "ON CONFLICT(client_ref) DO UPDATE SET count = ?",
            (client_ref, n, n),
        )
        conn.commit()
        return n


def record_vend(client_ref, msisdn, network, amount_minor, status):
    """The operator does NOT deduplicate. Two calls with the same client_ref
    create two separate vends. This is deliberate and matches reality."""
    operator_ref = "OP" + uuid.uuid4().hex[:14].upper()
    with closing(db()) as conn:
        conn.execute(
            "INSERT INTO vends (operator_ref, client_ref, msisdn, network, "
            "amount_minor, status, completed_at) VALUES (?,?,?,?,?,?,?)",
            (operator_ref, client_ref, msisdn, network, amount_minor, status,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    return operator_ref


# --------------------------------------------------------------------------
# auth and rate limiting
# --------------------------------------------------------------------------

def check_key(key):
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")


def rate_limit(scope: str, limit: int, window: int = 60):
    now = time.time()
    with _lock:
        hits = [t for t in _rate_window.get(scope, []) if now - t < window]
        if len(hits) >= limit:
            return False
        hits.append(now)
        _rate_window[scope] = hits
    return True


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------

class VendRequest(BaseModel):
    client_ref: str = Field(..., min_length=6, max_length=64)
    msisdn: str = Field(..., min_length=11, max_length=14)
    network: str
    amount_minor: int = Field(..., gt=0)


# --------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------

@app.post("/v1/vend")
def vend(req: VendRequest, x_api_key: str = Header(None)):
    check_key(x_api_key)

    if req.network.upper() not in {"MTN", "AIRTEL", "GLO", "9MOBILE"}:
        return JSONResponse(status_code=400, content={
            "status": "REJECTED", "reason_code": "UNKNOWN_NETWORK"})

    if not rate_limit("vend", 600):
        return JSONResponse(status_code=429, content={
            "status": "REJECTED", "reason_code": "RATE_LIMITED"},
            headers={"Retry-After": "5"})

    b = bucket(req.client_ref)
    n = attempt_number(req.client_ref)

    # 0-9: the vend commits on our side, then the connection dies.
    if b < 10:
        record_vend(req.client_ref, req.msisdn, req.network,
                    req.amount_minor, "SUCCESSFUL")
        time.sleep(TIMEOUT_DELAY_SECONDS)
        return JSONResponse(status_code=504, content={
            "status": "UNKNOWN", "reason_code": "GATEWAY_TIMEOUT"})

    # 10-14: business failure returned with HTTP 200.
    if b < 15:
        return JSONResponse(status_code=200, content={
            "status": "FAILED",
            "client_ref": req.client_ref,
            "operator_ref": None,
            "reason_code": "INSUFFICIENT_OPERATOR_FLOAT"})

    # 15-17: transient server error on the first attempt only. Nothing commits.
    if b < 18 and n == 1:
        return JSONResponse(status_code=500, content={
            "status": "UNKNOWN", "reason_code": "INTERNAL_ERROR"})

    # 18-19: rate limited on the first attempt only. Nothing commits.
    if b < 20 and n == 1:
        return JSONResponse(status_code=429, content={
            "status": "REJECTED", "reason_code": "RATE_LIMITED"},
            headers={"Retry-After": "3"})

    operator_ref = record_vend(req.client_ref, req.msisdn, req.network,
                               req.amount_minor, "SUCCESSFUL")
    return {
        "status": "SUCCESSFUL",
        "client_ref": req.client_ref,
        "operator_ref": operator_ref,
        "amount_minor": req.amount_minor,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/v1/status")
def status_lookup(client_ref: str, x_api_key: str = Header(None)):
    """Status lookup by client_ref. Rate limited to 10 per minute, and
    sometimes returns UNKNOWN even when a vend exists."""
    check_key(x_api_key)

    if not rate_limit("status", 10):
        return JSONResponse(status_code=429, content={
            "status": "REJECTED", "reason_code": "RATE_LIMITED"},
            headers={"Retry-After": "10"})

    if bucket(client_ref + ":status") < 20:
        return JSONResponse(status_code=200, content={
            "status": "UNKNOWN",
            "client_ref": client_ref,
            "reason_code": "LOOKUP_UNAVAILABLE"})

    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT * FROM vends WHERE client_ref = ? ORDER BY completed_at",
            (client_ref,)).fetchall()

    if not rows:
        return {"status": "NOT_FOUND", "client_ref": client_ref}

    return {
        "status": "SUCCESSFUL",
        "client_ref": client_ref,
        "matches": [dict(r) for r in rows],
    }


@app.get("/v1/settlement/{day}")
def settlement(day: str, x_api_key: str = Header(None)):
    """Daily settlement file. This is the operator's book of record.
    It does not always agree with what the API told you."""
    check_key(x_api_key)
    try:
        target = date.fromisoformat(day)
    except ValueError:
        raise HTTPException(status_code=400, detail="day must be YYYY-MM-DD")

    with closing(db()) as conn:
        rows = conn.execute("SELECT * FROM vends ORDER BY completed_at").fetchall()

    out = StringIO()
    w = csv.writer(out)
    w.writerow(["operator_ref", "client_ref", "msisdn", "network",
                "amount_minor", "status", "completed_at"])

    for r in rows:
        if datetime.fromisoformat(r["completed_at"]).date() != target:
            continue
        b = bucket(r["client_ref"] + ":settle")
        amount = r["amount_minor"]

        # 20-21: operator settles a different amount to the one confirmed.
        if 20 <= b < 22:
            amount = round(amount / 100) * 100 or 100
        # 22: vend confirmed by the API but absent from settlement.
        if b == 22:
            continue

        w.writerow([r["operator_ref"], r["client_ref"], r["msisdn"],
                    r["network"], amount, r["status"], r["completed_at"]])

        # 23: operator settles the same vend twice under different refs.
        if b == 23:
            w.writerow([r["operator_ref"] + "D", r["client_ref"], r["msisdn"],
                        r["network"], amount, r["status"], r["completed_at"]])

    return PlainTextResponse(out.getvalue(), media_type="text/csv")


@app.post("/v1/_admin/reset")
def reset(x_api_key: str = Header(None)):
    check_key(x_api_key)
    with closing(db()) as conn:
        conn.execute("DELETE FROM vends")
        conn.execute("DELETE FROM attempts")
        conn.commit()
    return {"status": "RESET"}


@app.get("/health")
def health():
    return {"status": "ok"}
