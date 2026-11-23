"""Add assigned roles, account status and provider usage metrics."""
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade():
    op.execute("ALTER TABLE users ADD COLUMN roles varchar[] NOT NULL DEFAULT '{}', ADD COLUMN enabled boolean NOT NULL DEFAULT true")
    op.execute("UPDATE users SET roles = ARRAY[role]")
    op.execute("ALTER TABLE sessions ADD COLUMN active_role varchar")
    op.execute("""CREATE TABLE model_usage (
        id varchar PRIMARY KEY, org varchar NOT NULL REFERENCES organizations,
        kind varchar NOT NULL, model varchar NOT NULL, outcome varchar NOT NULL,
        recorded_at timestamptz NOT NULL, latency_ms integer NOT NULL,
        prompt_tokens integer, completion_tokens integer, total_tokens integer
    )""")
    op.create_index("ix_model_usage_org_recorded_at", "model_usage", ["org", "recorded_at"])


def downgrade():
    op.drop_table("model_usage")
    op.execute("ALTER TABLE sessions DROP COLUMN active_role")
    op.execute("ALTER TABLE users DROP COLUMN enabled, DROP COLUMN roles")
