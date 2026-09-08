import logging
import uuid
from contextlib import asynccontextmanager
from fastapi import FastAPI, Form, Depends, Response
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from db import init_db, get_db, USSDSession, VendLedger
from operator_client import OperatorClient, detect_network

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ussd-service")

operator_client = OperatorClient()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="USSD Airtime Service", lifespan=lifespan)


@app.post("/ussd", response_class=Response)
async def ussd_handler(
    sessionId: str = Form(...),
    serviceCode: str = Form(...),
    phoneNumber: str = Form(...),
    text: str = Form(""),
    db: Session = Depends(get_db)
):
    text_clean = text.strip()
    parts = [p for p in text_clean.split("*") if p] if text_clean else []

    # Get or create session
    is_new_session = False
    sess = db.query(USSDSession).filter(USSDSession.session_id == sessionId).first()
    if not sess:
        is_new_session = True
        sess = USSDSession(session_id=sessionId, msisdn=phoneNumber, last_text=text_clean if text_clean else "__INITIAL__")
        db.add(sess)
        db.commit()
        db.refresh(sess)

    previous_text = sess.last_text

    # Menu Routing Logic based on text inputs
    if len(parts) == 0:
        sess.last_text = ""
        db.commit()
        return Response(content="CON Welcome to Airtime Vend\n1. Buy Airtime", media_type="text/plain")

    elif len(parts) == 1:
        if parts[0] == "1":
            sess.last_text = text_clean
            db.commit()
            return Response(content="CON Enter amount in NGN:", media_type="text/plain")
        else:
            return Response(content="CON Invalid option.\n1. Buy Airtime", media_type="text/plain")

    elif len(parts) == 2:
        if parts[0] != "1":
            return Response(content="CON Invalid option.\n1. Buy Airtime", media_type="text/plain")

        amount_str = parts[1]
        try:
            amount_ngn = int(amount_str)
            if amount_ngn <= 0:
                return Response(content="CON Invalid amount. Enter positive amount in NGN:", media_type="text/plain")
            if amount_ngn > 500000:
                return Response(content="CON Amount exceeds limit. Enter amount in NGN:", media_type="text/plain")

            sess.last_text = text_clean
            db.commit()
            return Response(content=f"CON Confirm buy {amount_ngn} NGN airtime?\n1. Yes\n2. No", media_type="text/plain")
        except ValueError:
            return Response(content="CON Invalid amount. Enter numeric amount in NGN:", media_type="text/plain")

    elif len(parts) == 3:
        if parts[0] != "1":
            return Response(content="END Invalid menu option.", media_type="text/plain")

        amount_str = parts[1]
        confirm_opt = parts[2]

        try:
            amount_ngn = int(amount_str)
            if amount_ngn <= 0 or amount_ngn > 500000:
                return Response(content="END Invalid amount specified.", media_type="text/plain")
        except ValueError:
            return Response(content="END Invalid amount specified.", media_type="text/plain")

        if confirm_opt != "1":
            return Response(content="END Transaction cancelled.", media_type="text/plain")

        # Check for existing vend (gateway-retry or double-tap)
        existing_vend = db.query(VendLedger).filter(VendLedger.session_id == sessionId).first()
        if existing_vend:
            return Response(content=f"END Airtime purchase of {amount_ngn} NGN already processed.", media_type="text/plain")

        # Check for out-of-order execution (session received main menu previously but never selected amount)
        expected_prev_text = f"1*{amount_str}"
        if not is_new_session and previous_text != expected_prev_text and previous_text != text_clean:
            return Response(content="CON Out of order action. Welcome to Airtime Vend\n1. Buy Airtime", media_type="text/plain")

        sess.last_text = text_clean
        db.commit()

        # Create new vend ledger entry
        client_ref = f"CIP-{sessionId[:16]}-{uuid.uuid4().hex[:6]}"
        amount_minor = amount_ngn * 100
        network = detect_network(phoneNumber)

        vend_entry = VendLedger(
            session_id=sessionId,
            client_ref=client_ref,
            msisdn=phoneNumber,
            network=network,
            amount_minor=amount_minor,
            status="PENDING"
        )

        try:
            db.add(vend_entry)
            db.commit()
            db.refresh(vend_entry)
        except IntegrityError:
            db.rollback()
            return Response(content=f"END Airtime purchase of {amount_ngn} NGN already processed.", media_type="text/plain")

        # Call operator API
        res = await operator_client.vend(
            client_ref=client_ref,
            msisdn=phoneNumber,
            network=network,
            amount_minor=amount_minor
        )

        vend_entry.status = res.get("status", "UNKNOWN")
        vend_entry.operator_ref = res.get("operator_ref")
        vend_entry.reason_code = res.get("reason_code")
        db.commit()

        if vend_entry.status == "SUCCESSFUL":
            return Response(content=f"END Airtime purchase of {amount_ngn} NGN successful.", media_type="text/plain")
        elif vend_entry.status == "FAILED":
            return Response(content=f"END Airtime purchase failed: {vend_entry.reason_code or 'Failed'}.", media_type="text/plain")
        else:
            return Response(content=f"END Airtime purchase submitted with status {vend_entry.status}.", media_type="text/plain")

    else:
        return Response(content="END Invalid input sequence.", media_type="text/plain")


@app.get("/health")
def health():
    return {"status": "ok"}
