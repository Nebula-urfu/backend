"""Initial procurement schema."""
from alembic import op

revision = "0001"
down_revision = None

TABLES = [
    "organizations (id varchar PRIMARY KEY, name varchar NOT NULL)",
    "users (id varchar PRIMARY KEY, org varchar NOT NULL REFERENCES organizations, username varchar NOT NULL UNIQUE, password varchar NOT NULL, role varchar NOT NULL, name varchar NOT NULL, address varchar NOT NULL)",
    "sessions (id varchar PRIMARY KEY, user_id varchar NOT NULL REFERENCES users, expires timestamptz NOT NULL)",
    "login_attempts (id varchar PRIMARY KEY, count integer NOT NULL, expires timestamptz NOT NULL)",
    "contractors (id varchar PRIMARY KEY, org varchar NOT NULL REFERENCES organizations, data jsonb NOT NULL)",
    "requests (id varchar PRIMARY KEY, org varchar NOT NULL REFERENCES organizations, author varchar NOT NULL REFERENCES users, key varchar NOT NULL, fingerprint varchar NOT NULL, data jsonb NOT NULL, UNIQUE(author, key))",
    "files (id varchar PRIMARY KEY, owner varchar NOT NULL REFERENCES users, request_id varchar REFERENCES requests, purpose varchar, version integer, data jsonb NOT NULL)",
    "approvals (id varchar PRIMARY KEY, request_id varchar NOT NULL REFERENCES requests, org varchar NOT NULL REFERENCES organizations, kind varchar NOT NULL, version integer NOT NULL, data jsonb NOT NULL, UNIQUE(request_id, kind, version))",
    "jobs (id varchar PRIMARY KEY, request_id varchar NOT NULL REFERENCES requests, kind varchar NOT NULL, target varchar NOT NULL, generation integer NOT NULL, attempt integer NOT NULL, state varchar NOT NULL)",
]


def upgrade():
    for definition in TABLES:
        op.execute("CREATE TABLE " + definition)
    for table, column in [("requests", "org"), ("files", "request_id"), ("approvals", "request_id"), ("approvals", "org"), ("jobs", "request_id")]:
        op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade():
    for definition in reversed(TABLES):
        op.drop_table(definition.split()[0])
