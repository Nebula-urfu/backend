"""Add organization-specific document templates."""
from alembic import op

revision = "0004"
down_revision = "0003"


def upgrade():
    op.execute("""CREATE TABLE procurement_templates (
        org varchar PRIMARY KEY REFERENCES organizations,
        revision integer NOT NULL,
        data jsonb NOT NULL
    )""")


def downgrade():
    op.drop_table("procurement_templates")
