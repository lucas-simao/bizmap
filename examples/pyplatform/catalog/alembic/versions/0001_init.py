"""create tenants, users, orders, documents"""

revision = "0001"
down_revision = None

import sqlalchemy as sa
from alembic import op


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String()),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String()),
        sa.Column("email", sa.String()),
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String()),
        sa.Column("user_id", sa.String()),
        sa.Column("status", sa.String(), nullable=False),
    )
    op.create_table(
        "documents",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("order_id", sa.String()),
        sa.Column("body", sa.Text()),
    )


def downgrade() -> None:
    op.drop_table("documents")
    op.drop_table("orders")
    op.drop_table("users")
    op.drop_table("tenants")
