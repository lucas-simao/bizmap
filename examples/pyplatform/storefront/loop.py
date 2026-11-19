"""Long-running storefront worker loop."""


def run_forever_loop():
    """Poll the catalog until stopped."""
    while True:
        from .client import list_orders

        list_orders()


if __name__ == "__main__":
    run_forever_loop()
