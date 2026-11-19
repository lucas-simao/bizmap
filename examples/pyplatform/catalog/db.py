"""Repository helpers using SQLAlchemy Core/ORM."""

from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.orm import Session

from .models import Order, documents


def fetch_order(session: Session, order_id: str):
    """Return one order by id."""
    return session.execute(select(Order).where(Order.id == order_id)).first()


def list_open_orders(session: Session):
    """List orders that still need fulfillment."""
    return session.query(Order).filter(Order.status == "open").all()


def create_order(session: Session, order_id: str, tenant_id: str, user_id: str) -> None:
    """Record a new order for a tenant."""
    session.add(Order(id=order_id, tenant_id=tenant_id, user_id=user_id, status="open"))
    session.execute(insert(documents).values(id=order_id + "-doc", order_id=order_id, body=""))


def mark_paid(session: Session, order_id: str) -> None:
    """Mark an order paid after checkout."""
    session.execute(update(Order).where(Order.id == order_id).values(status="paid"))


def cancel_order(session: Session, order_id: str) -> None:
    """Delete an order that never completed checkout."""
    session.execute(delete(Order).where(Order.id == order_id))


def raw_totals(session: Session):
    return session.execute(text("SELECT status, count(*) FROM orders GROUP BY status"))
