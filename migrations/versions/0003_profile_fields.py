"""Store resident location and work contact details."""
from alembic import op

revision = "0003"
down_revision = "0002"


def upgrade():
    for field in ("entrance", "floor", "apartment", "phone"):
        op.execute(f"ALTER TABLE users ADD COLUMN {field} varchar NOT NULL DEFAULT ''")


def downgrade():
    for field in ("phone", "apartment", "floor", "entrance"):
        op.drop_column("users", field)
