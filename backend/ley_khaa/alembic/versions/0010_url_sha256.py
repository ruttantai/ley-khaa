"""phase 9 task 6: a second key space for unfetchable sources (item 19)

Revision ID: 0010_url_sha256
Revises: 0009_second_question

Adds image_extractions.url_sha256. When `_bytes_for` never produces image
bytes at all -- a fetch refused, a body over the size cap, an undecodable
payload -- there is nothing to hash for image_sha256, so the row's identity
for that path becomes the hash of the SOURCE string instead. A second drive
of the identical unfetchable URL then finds the row by url_sha256 and does
not dead-letter it again.

Nullable and unique, with no server_default: every ordinary image-bytes row
leaves this NULL (SQLite/Postgres both allow many NULLs under a unique
index), and only a genuine second source-keyed row for the SAME source
collides on it. A shared "" default would defeat that uniqueness, which is
why this one column does not follow the "every new non-null string column
needs server_default=text(\"''\")" rule -- it is deliberately nullable, same
shape as messages.external_id in 0001_baseline.
"""

import sqlalchemy as sa
from alembic import op

revision = "0010_url_sha256"
down_revision = "0009_second_question"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "image_extractions",
        sa.Column("url_sha256", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_image_extractions_url_sha256", "image_extractions", ["url_sha256"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_image_extractions_url_sha256", table_name="image_extractions")
    op.drop_column("image_extractions", "url_sha256")
