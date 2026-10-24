"""Store model settings per workspace member."""

from alembic import op


revision = "0005_user_model_configs"
down_revision = "0004_paper_owned_cascade"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_model_configs (
            scope_key TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL DEFAULT '',
            user_id TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL DEFAULT '',
            base_url TEXT NOT NULL DEFAULT '',
            website_url TEXT NOT NULL DEFAULT '',
            api_url TEXT NOT NULL DEFAULT '',
            protocol TEXT NOT NULL DEFAULT 'chat_completions',
            model TEXT NOT NULL DEFAULT 'gpt-4.1-mini',
            fast_model TEXT NOT NULL DEFAULT '',
            analysis_model TEXT NOT NULL DEFAULT '',
            vision_model TEXT NOT NULL DEFAULT '',
            embedding_model TEXT NOT NULL DEFAULT '',
            embedding_api_url TEXT NOT NULL DEFAULT '',
            deepseek_search_enabled INTEGER NOT NULL DEFAULT 0,
            deepseek_search_api_url TEXT NOT NULL DEFAULT '',
            deepseek_fetch_api_url TEXT NOT NULL DEFAULT '',
            temperature DOUBLE PRECISION NOT NULL DEFAULT 0.2,
            enabled INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            CONSTRAINT user_model_configs_scope_unique UNIQUE(workspace_id,user_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS user_model_configs_workspace_idx "
        "ON user_model_configs(workspace_id,user_id)"
    )
    op.execute(
        "INSERT INTO app_meta(key,value) VALUES('schema_version','20') "
        "ON CONFLICT(key) DO UPDATE SET value='20'"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS user_model_configs")
