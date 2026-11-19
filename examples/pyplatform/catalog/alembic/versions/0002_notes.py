"""add orders.notes and drop a leftover scratch table"""

revision = "0002"
down_revision = "0001"

import sqlalchemy as sa
from alembic import op


def upgrade() -> None:
    op.add_column("orders", sa.Column("notes", sa.Text()))
    op.create_table(
        "scratch",
        sa.Column("id", sa.Integer(), primary_key=True),
    )
    with op.batch_alter_table("orders") as batch:
        batch.add_column(sa.Column("legacy_flag", sa.Integer()))
        batch.drop_column("legacy_flag")
    op.drop_table("scratch")
    op.rename_table("documents", "documents")
    op.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS extra TEXT")
    op.drop_column("orders", "extra")


def downgrade() -> None:
    op.drop_column("orders", "notes")
