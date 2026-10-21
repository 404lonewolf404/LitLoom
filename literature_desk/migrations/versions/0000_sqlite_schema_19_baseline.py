"""Record the application schema baseline imported from SQLite version 19."""

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
    if str(version or "") != "19":
        raise RuntimeError(
            "PostgreSQL Alembic baseline requires LitLoom application schema version 19"
        )


def downgrade() -> None:
    pass
