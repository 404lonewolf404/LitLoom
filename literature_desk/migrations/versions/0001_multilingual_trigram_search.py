"""Add indexed multilingual substring search for PostgreSQL."""

from alembic import op

revision = "0001_trigram_search"
down_revision = "0000_sqlite_19"
branch_labels = None
depends_on = None


_INDEXES = (
    ("paper_search_content", "title"),
    ("paper_search_content", "authors"),
    ("paper_search_content", "abstract"),
    ("paper_search_content", "topic"),
    ("paper_search_content", "tags"),
    ("paper_search_content", "notes"),
    ("paper_search_content", "reading_card"),
    ("paper_page_search_content", "text"),
    ("document_pages", "text"),
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for table, column in _INDEXES:
        name = f"{table}_{column}_trgm_idx"
        op.execute(
            f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" USING gin ("{column}" gin_trgm_ops)'
        )


def downgrade() -> None:
    for table, column in reversed(_INDEXES):
        op.execute(f'DROP INDEX IF EXISTS "{table}_{column}_trgm_idx"')
