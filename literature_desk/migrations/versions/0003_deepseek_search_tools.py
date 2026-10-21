"""Add optional DeepSeek Search & Fetch connector settings."""

from alembic import op

revision = "0003_deepseek_search_tools"
down_revision = "0002_segment_annotations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE model_config ADD COLUMN IF NOT EXISTS deepseek_search_enabled INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE model_config ADD COLUMN IF NOT EXISTS deepseek_search_api_url TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE model_config ADD COLUMN IF NOT EXISTS deepseek_fetch_api_url TEXT NOT NULL DEFAULT ''")


def downgrade() -> None:
    # Keep downgrade safe for deployments that have already used the optional
    # connector; removing settings would silently disable a user's workflow.
    pass
