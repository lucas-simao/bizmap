"""Storefront HTTP client for the catalog service."""

import os

import httpx
import requests
from sqlalchemy import text
from sqlalchemy.orm import Session

CATALOG = "http://catalog.internal"
ORDERS = "/api/orders"


def list_orders():
    """Read the catalog order list over HTTP."""
    return httpx.get(CATALOG + ORDERS)


def create_order():
    """Submit a new order to the catalog service."""
    return httpx.post(f"{CATALOG}{ORDERS}", json={"status": "open"})


def pay_order(order_id: str):
    return requests.post(CATALOG + f"/api/orders/{order_id}/pay")


def local_open_orders(session: Session):
    """Read orders from the shared database."""
    return session.execute(text("SELECT id, status FROM orders WHERE status = 'open'"))


def publish_job():
    from google.cloud import pubsub_v1

    pubsub_v1.PublisherClient().publish("jobs", b"x")
