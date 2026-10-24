"""Record the application schema baseline imported from SQLite.

The bootstrap schema is created by the running application before Alembic is
invoked. Older deployments created version 19, while a fresh 0.20 bootstrap
already contains the additive version-20 tables. The baseline therefore
accepts either state and lets the remaining idempotent migrations converge the
database to the current head.
"""

from alembic import op
from sqlalchemy import text

revision = "0000_sqlite_19"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    version = connection.execute(
        text("SELECT value FROM app_meta WHERE key='schema_version'")
    ).scalar_one_or_none()
    try:
        current = int(version or 0)
    except (TypeError, ValueError):
        current = 0
    if current < 19:
        raise RuntimeError(
            "PostgreSQL Alembic baseline requires LitLoom application schema version 19 or newer"
        )


def downgrade() -> None:
    pass
