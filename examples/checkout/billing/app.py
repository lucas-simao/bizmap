"""Billing service: invoices and payment-provider webhooks."""

import os
import uuid

import httpx
from fastapi import FastAPI
from sqlalchemy import create_engine, text

app = FastAPI()
engine = create_engine(os.environ["DATABASE_URL"])
API_URL = os.environ["API_URL"]


@app.post("/invoices")
def create_invoice(body: dict):
    """Open an invoice for a new order (called by the api at checkout)."""
    invoice_id = f"inv_{uuid.uuid4().hex}"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO invoices (id, order_id, amount_cents, status) "
                "VALUES (:id, :order_id, :amount, 'open')"
            ),
            {"id": invoice_id, "order_id": body["order_id"], "amount": body["amount_cents"]},
        )
    return {"id": invoice_id}


@app.post("/webhooks/payment")
def payment_webhook(event: dict):
    """The payment provider confirmed a charge: settle the invoice and tell the api."""
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE invoices SET status = 'paid', provider_ref = :ref WHERE id = :id"),
            {"ref": event["charge_id"], "id": event["invoice_id"]},
        )
    order_id = event["order_id"]
    httpx.post(f"{API_URL}/api/orders/{order_id}/paid")
    return {"ok": True}
