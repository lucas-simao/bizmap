"""Tiny FastAPI worker that reads the same shop tables as the Go API."""

from fastapi import FastAPI

app = FastAPI()


@app.get("/reports/low-stock")
def low_stock():
    """List SKUs that need replenishment for purchasing."""
    conn = connect()
    rows = conn.execute("SELECT sku, qty FROM inventory WHERE qty < 10")
    return list(rows)


def connect():
    # Placeholder: a real worker would use psycopg. BizMap only reads the SQL literal.
    return FakeConn()


class FakeConn:
    def execute(self, sql: str):
        return []
