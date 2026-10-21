"""Persist page anchored explanations for the side-by-side reader."""

from alembic import op

revision = "0002_segment_annotations"
down_revision = "0001_trigram_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_segment_annotations (
            id TEXT PRIMARY KEY,
            paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            segment_key TEXT NOT NULL,
            attachment_id TEXT NOT NULL DEFAULT '',
            page_number INTEGER NOT NULL,
            paragraph_index INTEGER NOT NULL DEFAULT 0,
            source_text TEXT NOT NULL DEFAULT '',
            analysis_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'completed',
            model TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(paper_id, segment_key)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS paper_segment_annotations_paper_idx "
        "ON paper_segment_annotations(paper_id,page_number,paragraph_index)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS paper_segment_annotations")
