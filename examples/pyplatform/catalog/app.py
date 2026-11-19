"""FastAPI catalog service."""

from fastapi import APIRouter, FastAPI

from .db import cancel_order, create_order, fetch_order, mark_paid
from .paths import API_PREFIX, ORDER_ITEM, ORDERS_PATH, PAY_PATH, V1
from .tasks import enqueue_ingest, refresh_orders

app = FastAPI()
router = APIRouter(prefix=API_PREFIX)


@router.get(ORDERS_PATH)
def list_orders():
    """List orders for the current tenant."""
    return []


@router.post(ORDERS_PATH)
def post_order():
    """Create an order and enqueue downstream ingest."""
    create_order(None, "o1", "t1", "u1")
    enqueue_ingest("o1")
    refresh_orders.delay()
    return {"id": "o1"}


@router.get(ORDER_ITEM)
def get_order():
    """Fetch a single order."""
    return fetch_order(None, "o1")


@app.post(PAY_PATH)
def pay_order():
    """Mark the order paid."""
    mark_paid(None, "o1")
    return {"ok": True}


@app.delete(API_PREFIX + "/orders/{id}")
def remove_order():
    """Cancel an unfinished order."""
    cancel_order(None, "o1")
    return {"ok": True}


# Unresolvable imported const — must keep the placeholder, never a truncated path.
from missing_paths import SECRET  # noqa: E402

@router.get(SECRET + "/hidden")
def hidden():
    return {}


app.include_router(router)
app.include_router(router, prefix=V1)
