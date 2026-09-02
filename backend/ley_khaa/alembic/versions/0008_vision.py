"""phase 7: vision extraction checkpoints

Revision ID: 0008_vision
Revises: 0007_channels
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_vision"
down_revision = "0007_channels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "image_extractions",
        sa.Column("image_sha256", sa.String(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False, server_default=sa.text("'text'")),
        sa.Column("content", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("summary", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("media_type", sa.String(), nullable=False, server_default=sa.text("''")),
        sa.Column("byte_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model", sa.String(), nullable=False, server_default=sa.text("''")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("image_extractions")
