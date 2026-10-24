"""Workspace isolation, sessions, and team authentication."""

from __future__ import annotations
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import sqlite3

from .services import app_services as _services


def tenant_isolation_enabled() -> bool:
    """Return whether authenticated requests use one database per workspace."""
    enabled = _services().team_auth_enabled() and _services().os.environ.get(
        "LITLOOM_TENANT_MODE", "shared"
    ).strip().casefold() in {"isolated", "tenant", "per_workspace"}
    if enabled and _services().using_postgres():
        raise RuntimeError(
            "PostgreSQL backend currently supports a shared workspace only; per-workspace isolation is not enabled"
        )
    return enabled


def workspace_database_path(workspace_id: str) -> Path:
    """Return a deterministic, non-user-controlled tenant database path."""
    try:
        safe_id = str(_services().uuid.UUID(str(workspace_id)))
    except (TypeError, ValueError, AttributeError):
        raise ValueError("工作区标识无效")
    return _services().WORKSPACE_DB_DIR / f"workspace-{safe_id}.sqlite3"


def workspace_files_dir(workspace_id: str) -> Path:
    """Keep tenant PDF attachments outside every other workspace's folder."""
    try:
        safe_id = str(_services().uuid.UUID(str(workspace_id)))
    except (TypeError, ValueError, AttributeError):
        raise ValueError("工作区标识无效")
    return _services().WORKSPACE_DB_DIR / f"workspace-{safe_id}" / "files"


def workspace_data_dir(workspace_id: str) -> Path:
    """Return the private data root for one isolated workspace."""
    try:
        safe_id = str(_services().uuid.UUID(str(workspace_id)))
    except (TypeError, ValueError, AttributeError):
        raise ValueError("工作区标识无效")
    return _services().WORKSPACE_DB_DIR / f"workspace-{safe_id}"


def active_files_dir() -> Path:
    actor = _services().AUTH_ACTOR.get()
    if _services().tenant_isolation_enabled() and actor and actor.get("workspace_id"):
        return _services().workspace_files_dir(actor["workspace_id"])
    return _services().FILES_DIR


def active_visuals_dir() -> Path:
    """Keep rendered PDF pages inside the active workspace in tenant mode."""
    actor = _services().AUTH_ACTOR.get()
    if _services().tenant_isolation_enabled() and actor and actor.get("workspace_id"):
        return _services().workspace_data_dir(actor["workspace_id"]) / "visual-pages"
    return _services().VISUALS_DIR


def active_backup_dir() -> Path:
    """Resolve backup archives against the active workspace, never globally."""
    actor = _services().AUTH_ACTOR.get()
    if _services().tenant_isolation_enabled() and actor and actor.get("workspace_id"):
        return _services().workspace_data_dir(actor["workspace_id"]) / "backups"
    return _services().BACKUP_DIR


def isolated_workspace_actors() -> list[dict | None]:
    """Return one active actor per workspace for maintenance and recovery.

    The scheduler has no browser session, so it uses an active owner/admin as
    the durable workspace context.  Domain access remains scoped by the
    context variable and every worker spawned from that context inherits it.
    """
    if not _services().tenant_isolation_enabled():
        return [None]
    with _services().DB_LOCK, _services().control_db_session() as db:
        rows = db.execute(
            """SELECT w.id AS workspace_id,w.name AS workspace_name,
                      u.id AS user_id,u.username,u.display_name,m.role
               FROM workspaces w
               JOIN workspace_memberships m ON m.workspace_id=w.id
               JOIN users u ON u.id=m.user_id
               WHERE u.active=1
               ORDER BY w.created_at,w.id,
                        CASE m.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END,
                        u.id"""
        ).fetchall()
    actors = []
    seen = set()
    for row in rows:
        workspace_id = _services().clean_text(row["workspace_id"], 80)
        if not workspace_id or workspace_id in seen:
            continue
        seen.add(workspace_id)
        actors.append(
            {
                "user_id": row["user_id"],
                "username": row["username"],
                "display_name": row["display_name"],
                "workspace_id": workspace_id,
                "workspace_name": row["workspace_name"],
                "role": row["role"],
                "csrf_hash": "",
                "scheduler": True,
            }
        )
    return actors


def initialize_workspace_databases() -> None:
    """Initialize and reconcile every tenant DB before HTTP traffic starts."""
    # The control DB must be initialized first because it owns memberships.
    _services().init_db()
    _services().STARTUP_INTERRUPTED_BY_WORKSPACE = {
        "": {
            "sync": list(_services().STARTUP_INTERRUPTED_SYNC_IDS),
            "agent": list(_services().STARTUP_INTERRUPTED_AGENT_IDS),
        }
    }
    if not _services().tenant_isolation_enabled():
        return
    sync_ids, agent_ids = list(_services().STARTUP_INTERRUPTED_SYNC_IDS), list(
        _services().STARTUP_INTERRUPTED_AGENT_IDS
    )
    for actor in _services().isolated_workspace_actors():
        if not actor:
            continue
        token = _services().AUTH_ACTOR.set(actor)
        try:
            _services().init_db()
            _services().STARTUP_INTERRUPTED_BY_WORKSPACE[actor["workspace_id"]] = {
                "sync": list(_services().STARTUP_INTERRUPTED_SYNC_IDS),
                "agent": list(_services().STARTUP_INTERRUPTED_AGENT_IDS),
            }
            sync_ids.extend(_services().STARTUP_INTERRUPTED_SYNC_IDS)
            agent_ids.extend(_services().STARTUP_INTERRUPTED_AGENT_IDS)
        finally:
            _services().AUTH_ACTOR.reset(token)
    _services().STARTUP_INTERRUPTED_SYNC_IDS = list(dict.fromkeys(sync_ids))
    _services().STARTUP_INTERRUPTED_AGENT_IDS = list(dict.fromkeys(agent_ids))


def resume_workspace_background_jobs() -> None:
    """Resume durable PDF, metadata, index, search, AI and Agent jobs per tenant."""
    actors = _services().isolated_workspace_actors()
    for actor in actors:
        token = _services().AUTH_ACTOR.set(actor)
        try:
            key = actor.get("workspace_id", "") if actor else ""
            scoped = _services().STARTUP_INTERRUPTED_BY_WORKSPACE.get(
                key, {"sync": [], "agent": []}
            )
            _services().STARTUP_INTERRUPTED_SYNC_IDS = list(scoped.get("sync", []))
            _services().STARTUP_INTERRUPTED_AGENT_IDS = list(scoped.get("agent", []))
            _services().active_files_dir().mkdir(parents=True, exist_ok=True)
            _services().resume_pdf_ingest_jobs()
            _services().resume_metadata_draft_jobs()
            _services().resume_semantic_index_jobs()
            _services().resume_interrupted_sync_jobs()
            _services().resume_interrupted_ai_jobs()
            _services().resume_interrupted_agent_runs()
        finally:
            _services().AUTH_ACTOR.reset(token)


def _workspace_schema_sql(source: sqlite3.Connection) -> list[str]:
    rows = source.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 WHEN 'trigger' THEN 2 ELSE 3 END,name"
    ).fetchall()
    control_tables = {
        "workspaces",
        "users",
        "workspace_memberships",
        "auth_sessions",
        "auth_login_attempts",
    }
    statements = []
    for row in rows:
        if row[0] == "table" and row[1] in control_tables:
            continue
        statements.append(row[2])
    return statements


def ensure_workspace_database(
    workspace_id: str, *, clone_current: bool = False
) -> Path:
    """Create an isolated tenant database from the current schema/data once."""
    path = _services().workspace_database_path(workspace_id)
    _services().WORKSPACE_DB_DIR.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        return path
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    source = _services().sqlite3.connect(_services().DB_PATH, timeout=30)
    target = None
    try:
        target = _services().sqlite3.connect(temporary, timeout=30)
        if clone_current:
            source.backup(target)
            target.execute("PRAGMA foreign_keys=OFF")
            # Authentication is kept in the control database only. A cloned
            # tenant keeps the existing paper history but cannot expose users
            # or sessions through an accidental domain query.
            for table in (
                "auth_sessions",
                "workspace_memberships",
                "users",
                "workspaces",
                "auth_login_attempts",
            ):
                target.execute(f"DELETE FROM {table}")
        else:
            target.execute("PRAGMA foreign_keys=OFF")
            for statement in _services()._workspace_schema_sql(source):
                try:
                    target.execute(statement)
                except _services().sqlite3.OperationalError:
                    # FTS shadow/index objects can be recreated by the regular
                    # maintenance path; one unsupported optional object must
                    # not prevent the tenant database from starting.
                    continue
            target.execute(
                "INSERT OR REPLACE INTO app_meta(key,value) VALUES('schema_version',?)",
                (str(_services().DB_SCHEMA_VERSION),),
            )
            target.execute("PRAGMA user_version=" + str(_services().DB_SCHEMA_VERSION))
        target.commit()
    finally:
        if target is not None:
            target.close()
        source.close()
    temporary.replace(path)
    return path


def _active_database_path() -> Path:
    actor = _services().AUTH_ACTOR.get()
    if _services().tenant_isolation_enabled() and actor and actor.get("workspace_id"):
        return _services().ensure_workspace_database(
            actor["workspace_id"], clone_current=False
        )
    return _services().DB_PATH


def postgres_database_url() -> str:
    return _services().os.environ.get("LITLOOM_DATABASE_URL", "").strip()


def using_postgres() -> bool:
    return postgres_database_url().startswith(("postgresql://", "postgresql+psycopg://"))


def sqlite_test_backend_enabled() -> bool:
    """Allow SQLite only when a test process opts in explicitly."""
    return _services().os.environ.get("LITLOOM_TESTING", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def database_table_exists(db, table_name: str) -> bool:
    if using_postgres():
        return _services().postgres_table_exists(db, table_name)
    return bool(
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
        ).fetchone()
    )


def database_integrity_check(db) -> str:
    if using_postgres():
        return _services().postgres_integrity_check(db)
    return str(db.execute("PRAGMA integrity_check").fetchone()[0])


def connect(path: Path | None = None):
    database_url = _services().postgres_database_url()
    if database_url:
        return _services().postgres_connect(database_url)
    if not _services().sqlite_test_backend_enabled():
        raise RuntimeError(
            "LitLoom 运行必须使用 PostgreSQL；请配置 LITLOOM_DATABASE_URL。"
        )
    _services().DATA_DIR.mkdir(parents=True, exist_ok=True)
    selected = _services().Path(path or _services()._active_database_path())
    selected.parent.mkdir(parents=True, exist_ok=True)
    db = _services().sqlite3.connect(selected, timeout=30)
    db.row_factory = _services().sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    return db


@contextmanager
def db_session():
    db = _services().connect()
    changed = False
    try:
        yield db
        db.commit()
        changed = db.total_changes > 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
        if changed:
            _services().invalidate_derived_cache()


@contextmanager
def control_db_session():
    """Open the control-plane database even while a tenant actor is active."""
    db = _services().connect(_services().DB_PATH)
    changed = False
    try:
        yield db
        db.commit()
        changed = db.total_changes > 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
        if changed:
            _services().invalidate_derived_cache()


def record_audit(
    db: sqlite3.Connection,
    event_type: str,
    entity_type: str = "",
    entity_id: str = "",
    payload: dict | None = None,
) -> None:
    """Persist a small, secret-free event for debugging and product metrics."""
    safe_payload = dict(payload or {})
    actor = _services().AUTH_ACTOR.get()
    if actor and actor.get("user_id"):
        safe_payload.setdefault(
            "actor_user_id", _services().clean_text(actor["user_id"], 120)
        )
        safe_payload.setdefault(
            "workspace_id", _services().clean_text(actor.get("workspace_id", ""), 120)
        )
    db.execute(
        "INSERT INTO audit_events(event_type,entity_type,entity_id,payload_json,created_at) VALUES(?,?,?,?,?)",
        (
            _services().clean_text(event_type, 120),
            _services().clean_text(entity_type, 120),
            _services().clean_text(entity_id, 200),
            _services().json.dumps(safe_payload, ensure_ascii=False)[:12000],
            _services().now_iso(),
        ),
    )


def team_auth_enabled() -> bool:
    return _services().os.environ.get("LITLOOM_AUTH_MODE", "").strip().casefold() in {
        "team",
        "single_workspace",
    }


def local_auto_login_enabled() -> bool:
    """Enable passwordless sessions only for explicit local development use."""
    return _services().os.environ.get(
        "LITLOOM_LOCAL_AUTO_LOGIN", ""
    ).strip().casefold() in {"1", "true", "yes", "on"}


def create_local_auto_login_session() -> tuple[str, str, dict] | None:
    """Create a normal session for a persistent local convenience account.

    The account has a randomly generated, undisclosed password. Access is
    granted only by the HTTP layer after verifying the direct peer is loopback.
    This keeps browser use convenient without introducing a reusable default
    credential.
    """
    if not _services().team_auth_enabled() or not _services().local_auto_login_enabled():
        return None
    username = _services().validate_username(
        _services().os.environ.get("LITLOOM_LOCAL_AUTO_LOGIN_USERNAME", "local.default")
    )
    display_name = _services().clean_text(
        _services().os.environ.get("LITLOOM_LOCAL_AUTO_LOGIN_DISPLAY_NAME", "本机默认用户"),
        120,
    ) or "本机默认用户"
    with _services().DB_LOCK, _services().control_db_session() as db:
        workspace = (
            _services()._canonical_auth_workspace(db)
            if _services()._postgres_shared_workspace_mode()
            else _services()._auth_workspace(db)
        )
        user = db.execute(
            "SELECT id,active FROM users WHERE username=? COLLATE NOCASE",
            (username,),
        ).fetchone()
        if not user:
            user_id, now = str(_services().uuid.uuid4()), _services().now_iso()
            db.execute(
                "INSERT INTO users(id,username,display_name,password_hash,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (
                    user_id,
                    username,
                    display_name,
                    _services().hash_password(_services().new_token(48)),
                    now,
                    now,
                ),
            )
            db.execute(
                "INSERT INTO workspace_memberships(workspace_id,user_id,role,created_at) VALUES(?,?,?,?)",
                (workspace["id"], user_id, "researcher", now),
            )
            _services().record_audit(
                db,
                "auth.local_auto_user_created",
                "user",
                user_id,
                {"workspace_id": workspace["id"]},
            )
        else:
            user_id = user["id"]
            if not user["active"]:
                return None
            membership = db.execute(
                "SELECT role FROM workspace_memberships WHERE workspace_id=? AND user_id=?",
                (workspace["id"], user_id),
            ).fetchone()
            if not membership:
                return None
            # The configured local entry is a convenience account for opening
            # the workspace, so it must retain the ordinary edit permission
            # even when an older database created it as viewer/reviewer.
            # Never grant it an administrative role automatically.
            if membership["role"] in {"viewer", "reviewer"}:
                db.execute(
                    "UPDATE workspace_memberships SET role=? WHERE workspace_id=? AND user_id=?",
                    ("researcher", workspace["id"], user_id),
                )
                _services().record_audit(
                    db,
                    "auth.local_auto_user_role_repaired",
                    "user",
                    user_id,
                    {"workspace_id": workspace["id"], "role": "researcher"},
                )
            elif membership["role"] not in {"owner", "admin", "researcher"}:
                return None
        return _services().create_auth_session(db, user_id, workspace["id"])


def _auth_workspace(db: sqlite3.Connection) -> sqlite3.Row:
    row = db.execute(
        "SELECT id,name FROM workspaces ORDER BY created_at,id LIMIT 1"
    ).fetchone()
    if row:
        return row
    workspace_id = str(_services().uuid.uuid4())
    db.execute(
        "INSERT INTO workspaces(id,name,created_at) VALUES(?,?,?)",
        (workspace_id, "LitLoom 团队空间", _services().now_iso()),
    )
    return db.execute(
        "SELECT id,name FROM workspaces WHERE id=?", (workspace_id,)
    ).fetchone()


def _postgres_shared_workspace_mode() -> bool:
    """Whether PostgreSQL is running with one shared workspace.

    PostgreSQL currently has no row-level tenant policy in this application.
    Keeping this check local to the control-plane helpers prevents a caller
    from selecting an older or otherwise manually-created workspace and
    accidentally treating it as an isolated tenant.
    """
    mode = _services().os.environ.get("LITLOOM_TENANT_MODE", "shared").strip().casefold()
    return _services().using_postgres() and mode not in {
        "isolated",
        "tenant",
        "per_workspace",
    }


def _canonical_auth_workspace(db: sqlite3.Connection) -> sqlite3.Row:
    """Return the only supported workspace in PostgreSQL shared mode."""
    return _services()._auth_workspace(db)


def _bootstrap_token_path() -> Path:
    return _services().DATA_DIR / "bootstrap-admin-token.txt"


def _mcp_service_token() -> str:
    """Read an optional local MCP service token without persisting or logging it."""
    configured_file = _services().os.environ.get("LITLOOM_MCP_TOKEN_FILE", "").strip()
    candidates = (
        [_services().Path(configured_file).expanduser()] if configured_file else []
    )
    for path in candidates:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            continue
        if value and not value.startswith("#"):
            return (
                value.split("=", 1)[1].strip().strip("'\"")
                if value.upper().startswith("LITLOOM_MCP_TOKEN=") and "=" in value
                else value
            )
    return _services().os.environ.get("LITLOOM_MCP_TOKEN", "").strip()


def _mcp_service_actor(token: str) -> dict | None:
    """Resolve a configured MCP token to one active member of this workspace."""
    if not _services().team_auth_enabled() or not _services().constant_time_token_match(
        token, _services()._mcp_service_token()
    ):
        return None
    username = _services().os.environ.get("LITLOOM_MCP_USERNAME", "").strip().casefold()
    workspace_id = _services().os.environ.get("LITLOOM_MCP_WORKSPACE_ID", "").strip()
    if not username:
        return None
    with _services().DB_LOCK, _services().control_db_session() as db:
        if _services()._postgres_shared_workspace_mode():
            canonical = _services()._canonical_auth_workspace(db)
            if workspace_id and workspace_id != canonical["id"]:
                return None
            workspace_id = canonical["id"]
        workspace_clause = " AND w.id=?" if workspace_id else ""
        workspace_args = (username, workspace_id) if workspace_id else (username,)
        row = db.execute(
            f"""SELECT u.id AS user_id,u.username,u.display_name,u.active,
                      w.id AS workspace_id,w.name AS workspace_name,m.role
               FROM users u JOIN workspace_memberships m ON m.user_id=u.id
               JOIN workspaces w ON w.id=m.workspace_id
               WHERE u.username=?{workspace_clause}
               ORDER BY w.created_at,w.id LIMIT 1""",
            workspace_args,
        ).fetchone()
    if not row or not row["active"]:
        return None
    # A service token is deliberately not a browser session and therefore has
    # no usable CSRF hash. The request gate uses the private marker below to
    # skip only the browser CSRF check; role checks still run normally.
    return {
        "user_id": row["user_id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "workspace_id": row["workspace_id"],
        "workspace_name": row["workspace_name"],
        "role": row["role"],
        "csrf_hash": "",
        "mcp_service": True,
    }


def _ensure_auth_bootstrap_token(db: sqlite3.Connection) -> None:
    if (
        not _services().team_auth_enabled()
        or _services()._auth_owner_exists(db)
    ):
        return
    token_row = db.execute(
        "SELECT value FROM app_meta WHERE key='auth_bootstrap_token_sha256'"
    ).fetchone()
    if token_row and token_row[0]:
        return
    token = _services().new_token(_services().BOOTSTRAP_TOKEN_BYTES)
    path = _services()._bootstrap_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = _services().os.O_WRONLY | _services().os.O_CREAT | _services().os.O_TRUNC
    try:
        fd = _services().os.open(path, flags, 0o600)
        with _services().os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
    except OSError as exc:
        raise RuntimeError(f"无法安全创建管理员初始化令牌：{path}") from exc
    db.execute(
        "INSERT INTO app_meta(key,value) VALUES('auth_bootstrap_token_sha256',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (_services().token_digest(token),),
    )


def _auth_owner_exists(db: sqlite3.Connection) -> bool:
    """Whether the canonical workspace has an active owner account."""
    workspace = _services()._auth_workspace(db)
    return bool(
        db.execute(
            """SELECT 1 FROM workspace_memberships m
               JOIN users u ON u.id=m.user_id
               WHERE m.workspace_id=? AND m.role='owner' AND u.active=1 LIMIT 1""",
            (workspace["id"],),
        ).fetchone()
    )


def create_auth_session(
    db: sqlite3.Connection, user_id: str, workspace_id: str = ""
) -> tuple[str, str, dict]:
    if _services()._postgres_shared_workspace_mode():
        canonical = _services()._canonical_auth_workspace(db)
        if workspace_id and workspace_id != canonical["id"]:
            raise PermissionError(
                "当前 PostgreSQL 运行在共享工作区模式，不允许使用其他工作区"
            )
        workspace = canonical
    else:
        workspace = None
    if workspace is None and workspace_id:
        workspace = db.execute(
            "SELECT id,name FROM workspaces WHERE id=?", (workspace_id,)
        ).fetchone()
    workspace = workspace or _services()._auth_workspace(db)
    membership = db.execute(
        "SELECT role FROM workspace_memberships WHERE workspace_id=? AND user_id=?",
        (workspace["id"], user_id),
    ).fetchone()
    if not membership:
        raise PermissionError("此账号尚未加入团队工作区")
    session_token, csrf_token = _services().new_token(), _services().new_token(24)
    now = _services().datetime.now(_services().timezone.utc).replace(microsecond=0)
    db.execute(
        "INSERT INTO auth_sessions(token_hash,user_id,workspace_id,csrf_hash,created_at,last_seen_at,expires_at) VALUES(?,?,?,?,?,?,?)",
        (
            _services().token_digest(session_token),
            user_id,
            workspace["id"],
            _services().token_digest(csrf_token),
            now.isoformat(),
            now.isoformat(),
            (
                now + _services().timedelta(days=_services().AUTH_SESSION_TTL_DAYS)
            ).isoformat(),
        ),
    )
    # Keep the query portable across SQLite and PostgreSQL. SQLite accepts
    # ``LIMIT -1 OFFSET`` but PostgreSQL rejects it; slicing the small session
    # list in Python is also clearer and avoids backend-specific SQL.
    all_sessions = db.execute(
        "SELECT token_hash FROM auth_sessions WHERE user_id=? ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()
    stale = all_sessions[10:]
    if stale:
        db.executemany(
            "DELETE FROM auth_sessions WHERE token_hash=?",
            [(row["token_hash"],) for row in stale],
        )
    user = db.execute(
        "SELECT id,username,display_name FROM users WHERE id=?", (user_id,)
    ).fetchone()
    return (
        session_token,
        csrf_token,
        {
            "user_id": user["id"],
            "username": user["username"],
            "display_name": user["display_name"],
            "workspace_id": workspace["id"],
            "workspace_name": workspace["name"],
            "role": membership["role"],
        },
    )


def resolve_auth_session(session_token: str) -> dict | None:
    if not session_token:
        return None
    now = _services().datetime.now(_services().timezone.utc).replace(microsecond=0)
    with _services().DB_LOCK, _services().control_db_session() as db:
        row = db.execute(
            """SELECT s.*,u.username,u.display_name,u.active,w.name AS workspace_name,m.role
               FROM auth_sessions s JOIN users u ON u.id=s.user_id
               JOIN workspaces w ON w.id=s.workspace_id
               JOIN workspace_memberships m ON m.workspace_id=s.workspace_id AND m.user_id=s.user_id
               WHERE s.token_hash=?""",
            (_services().token_digest(session_token),),
        ).fetchone()
        if not row:
            return None
        if _services()._postgres_shared_workspace_mode():
            canonical = _services()._canonical_auth_workspace(db)
            if row["workspace_id"] != canonical["id"]:
                # A session created before the shared-mode guard, or inserted
                # manually, must never expose another workspace's data.
                db.execute(
                    "DELETE FROM auth_sessions WHERE token_hash=?", (row["token_hash"],)
                )
                return None
        created = _services().datetime.fromisoformat(row["created_at"])
        expires = _services().datetime.fromisoformat(row["expires_at"])
        if (
            not row["active"]
            or expires <= now
            or created
            + _services().timedelta(days=_services().AUTH_SESSION_MAX_AGE_DAYS)
            <= now
        ):
            db.execute(
                "DELETE FROM auth_sessions WHERE token_hash=?", (row["token_hash"],)
            )
            return None
        last_seen = _services().datetime.fromisoformat(row["last_seen_at"])
        if (now - last_seen).total_seconds() >= 60:
            db.execute(
                "UPDATE auth_sessions SET last_seen_at=?,expires_at=? WHERE token_hash=?",
                (
                    now.isoformat(),
                    (
                        now
                        + _services().timedelta(days=_services().AUTH_SESSION_TTL_DAYS)
                    ).isoformat(),
                    row["token_hash"],
                ),
            )
        return {
            "user_id": row["user_id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "workspace_id": row["workspace_id"],
            "workspace_name": row["workspace_name"],
            "role": row["role"],
            "csrf_hash": row["csrf_hash"],
        }


def auth_bootstrap_required() -> bool:
    if not _services().team_auth_enabled():
        return False
    with _services().DB_LOCK, _services().control_db_session() as db:
        return not _services()._auth_owner_exists(db)


def bootstrap_team_owner(data: dict) -> tuple[str, str, dict]:
    if not _services().team_auth_enabled():
        raise LookupError("团队认证尚未启用")
    username = _services().validate_username(data.get("username", ""))
    password = _services().validate_password(data.get("password", ""))
    bootstrap_token = str(data.get("bootstrap_token") or "").strip()
    with _services().DB_LOCK, _services().control_db_session() as db:
        if _services()._auth_owner_exists(db):
            raise LookupError("管理员已经初始化，请改用登录")
        token_row = db.execute(
            "SELECT value FROM app_meta WHERE key='auth_bootstrap_token_sha256'"
        ).fetchone()
        supplied = _services().token_digest(bootstrap_token)
        if not token_row or not _services().constant_time_token_match(
            supplied, token_row[0]
        ):
            raise PermissionError("初始化令牌不正确")
        workspace = _services()._auth_workspace(db)
        now, user_id = _services().now_iso(), str(_services().uuid.uuid4())
        db.execute(
            "INSERT INTO users(id,username,display_name,password_hash,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (
                user_id,
                username,
                _services().clean_text(data.get("display_name") or username, 120),
                _services().hash_password(password),
                now,
                now,
            ),
        )
        db.execute(
            "INSERT INTO workspace_memberships(workspace_id,user_id,role,created_at) VALUES(?,?,?,?)",
            (workspace["id"], user_id, "owner", now),
        )
        db.execute("DELETE FROM app_meta WHERE key='auth_bootstrap_token_sha256'")
        try:
            _services()._bootstrap_token_path().unlink(missing_ok=True)
        except OSError:
            pass
        _services().record_audit(
            db,
            "auth.owner_bootstrapped",
            "user",
            user_id,
            {"workspace_id": workspace["id"]},
        )
        return _services().create_auth_session(db, user_id)


def _auth_login_identity(username: str, client_ip: str) -> str:
    return (
        _services()
        .hashlib.sha256(
            f"{client_ip.casefold()}\0{username.casefold()}".encode("utf-8")
        )
        .hexdigest()
    )


def _auth_login_locked(
    db: sqlite3.Connection, identity_hash: str, now: datetime
) -> bool:
    row = db.execute(
        "SELECT locked_until FROM auth_login_attempts WHERE identity_hash=?",
        (identity_hash,),
    ).fetchone()
    return bool(
        row
        and row["locked_until"]
        and _services().datetime.fromisoformat(row["locked_until"]) > now
    )


def _record_auth_failure(
    db: sqlite3.Connection, identity_hash: str, now: datetime
) -> None:
    row = db.execute(
        "SELECT * FROM auth_login_attempts WHERE identity_hash=?", (identity_hash,)
    ).fetchone()
    window_started = (
        _services().datetime.fromisoformat(row["window_started_at"]) if row else now
    )
    failures = int(row["failure_count"] or 0) if row else 0
    if (now - window_started).total_seconds() >= _services().AUTH_LOGIN_WINDOW_SECONDS:
        window_started, failures = now, 0
    failures += 1
    locked_until = (
        (
            now + _services().timedelta(seconds=_services().AUTH_LOGIN_WINDOW_SECONDS)
        ).isoformat()
        if failures >= _services().AUTH_LOGIN_MAX_FAILURES
        else ""
    )
    db.execute(
        """INSERT INTO auth_login_attempts(identity_hash,window_started_at,failure_count,locked_until) VALUES(?,?,?,?)
           ON CONFLICT(identity_hash) DO UPDATE SET window_started_at=excluded.window_started_at,failure_count=excluded.failure_count,locked_until=excluded.locked_until""",
        (identity_hash, window_started.isoformat(), failures, locked_until),
    )


def authenticate_user(
    username: str, password: str, client_ip: str = "", workspace_id: str = ""
) -> tuple[str, str, dict]:
    username = _services().validate_username(username)
    now = _services().datetime.now(_services().timezone.utc).replace(microsecond=0)
    identity_hash = _services()._auth_login_identity(username, client_ip)
    with _services().DB_LOCK, _services().control_db_session() as db:
        if _services()._auth_login_locked(db, identity_hash, now):
            raise TimeoutError("登录尝试过多，请 10 分钟后重试")
        row = db.execute(
            "SELECT id,password_hash,active FROM users WHERE username=? COLLATE NOCASE",
            (username,),
        ).fetchone()
        valid = _services().verify_password(
            password,
            (
                row["password_hash"]
                if row
                else "pbkdf2_sha256$310000$00000000000000000000000000000000$0000000000000000000000000000000000000000000000000000000000000000"
            ),
        )
        if not row or not valid or not row["active"]:
            _services()._record_auth_failure(db, identity_hash, now)
            _services().record_audit(
                db,
                "auth.login_failed",
                "user",
                "",
                {
                    "username": username,
                    "client_ip_hash": _services()
                    .hashlib.sha256(str(client_ip).encode())
                    .hexdigest(),
                },
            )
            raise PermissionError("用户名或密码不正确")
        db.execute(
            "DELETE FROM auth_login_attempts WHERE identity_hash=?", (identity_hash,)
        )
        selected_workspace = _services().clean_text(workspace_id, 80)
        if _services()._postgres_shared_workspace_mode():
            canonical = _services()._canonical_auth_workspace(db)
            if selected_workspace and selected_workspace != canonical["id"]:
                raise PermissionError(
                    "当前 PostgreSQL 运行在共享工作区模式，不允许选择其他工作区"
                )
            selected_workspace = canonical["id"]
        if selected_workspace:
            membership = db.execute(
                "SELECT 1 FROM workspace_memberships WHERE workspace_id=? AND user_id=?",
                (selected_workspace, row["id"]),
            ).fetchone()
            if not membership:
                raise PermissionError("该账号未加入所选工作区")
        else:
            selected_workspace = _services()._auth_workspace(db)["id"]
        _services().record_audit(
            db, "auth.login", "user", row["id"], {"workspace_id": selected_workspace}
        )
        return _services().create_auth_session(db, row["id"], selected_workspace)


def create_workspace_user(data: dict) -> dict:
    username = _services().validate_username(data.get("username", ""))
    password = _services().validate_password(data.get("password", ""))
    role = data.get("role", "researcher")
    if role not in {"admin", "researcher", "reviewer", "viewer"}:
        raise ValueError(
            "可分配角色为 admin、researcher、reviewer 或 viewer；owner 不能由此接口创建"
        )
    now, user_id = _services().now_iso(), str(_services().uuid.uuid4())
    with _services().DB_LOCK, _services().control_db_session() as db:
        workspace = _services()._auth_workspace(db)
        db.execute(
            "INSERT INTO users(id,username,display_name,password_hash,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (
                user_id,
                username,
                _services().clean_text(data.get("display_name") or username, 120),
                _services().hash_password(password),
                now,
                now,
            ),
        )
        db.execute(
            "INSERT INTO workspace_memberships(workspace_id,user_id,role,created_at) VALUES(?,?,?,?)",
            (workspace["id"], user_id, role, now),
        )
        _services().record_audit(
            db,
            "auth.user_created",
            "user",
            user_id,
            {"role": role, "workspace_id": workspace["id"]},
        )
        return _services().auth_user_snapshot(db, user_id, workspace["id"])


def list_user_workspaces(user_id: str) -> list[dict]:
    with _services().DB_LOCK, _services().control_db_session() as db:
        workspace_filter = ""
        args = [user_id]
        if _services()._postgres_shared_workspace_mode():
            canonical = _services()._canonical_auth_workspace(db)
            workspace_filter = " AND w.id=?"
            args.append(canonical["id"])
        rows = db.execute(
            f"SELECT w.id,w.name,w.created_at,m.role FROM workspaces w JOIN workspace_memberships m ON m.workspace_id=w.id WHERE m.user_id=?{workspace_filter} ORDER BY w.created_at,w.id",
            tuple(args),
        ).fetchall()
        return [dict(row) for row in rows]


def create_workspace(data: dict, actor: dict) -> dict:
    if actor.get("role") not in {"owner", "admin"}:
        raise PermissionError("只有工作区管理员可以创建工作区")
    if _services().using_postgres() and not _services().tenant_isolation_enabled():
        raise PermissionError(
            "当前 PostgreSQL 运行在共享工作区模式，暂不支持创建第二个工作区；"
            "请先完成 PostgreSQL 租户隔离后再启用多工作区"
        )
    name = _services().clean_text(data.get("name", ""), 160)
    if len(name) < 2:
        raise ValueError("工作区名称至少需要 2 个字符")
    workspace_id, now = str(_services().uuid.uuid4()), _services().now_iso()
    with _services().DB_LOCK, _services().control_db_session() as db:
        db.execute(
            "INSERT INTO workspaces(id,name,created_at) VALUES(?,?,?)",
            (workspace_id, name, now),
        )
        db.execute(
            "INSERT INTO workspace_memberships(workspace_id,user_id,role,created_at) VALUES(?,?,?,?)",
            (workspace_id, actor["user_id"], "owner", now),
        )
        _services().record_audit(
            db,
            "auth.workspace_created",
            "workspace",
            workspace_id,
            {"name": name, "created_by": actor.get("user_id", "")},
        )
    if _services().tenant_isolation_enabled():
        _services().ensure_workspace_database(workspace_id, clone_current=False)
    return {
        "id": workspace_id,
        "name": name,
        "created_at": now,
        "role": "owner",
        "isolated": _services().tenant_isolation_enabled(),
    }


def switch_workspace(user_id: str, workspace_id: str) -> tuple[str, str, dict]:
    workspace_id = _services().clean_text(workspace_id, 80)
    if not workspace_id:
        raise ValueError("需要 workspace_id")
    if _services().using_postgres() and not _services().tenant_isolation_enabled():
        raise PermissionError(
            "当前 PostgreSQL 运行在共享工作区模式，不允许切换到未隔离的其他工作区"
        )
    with _services().DB_LOCK, _services().control_db_session() as db:
        membership = db.execute(
            "SELECT role FROM workspace_memberships WHERE workspace_id=? AND user_id=?",
            (workspace_id, user_id),
        ).fetchone()
        if not membership:
            raise PermissionError("该账号未加入所选工作区")
        return _services().create_auth_session(db, user_id, workspace_id)


def auth_user_snapshot(db: sqlite3.Connection, user_id: str, workspace_id: str) -> dict:
    row = db.execute(
        """SELECT u.id,u.username,u.display_name,u.active,u.created_at,m.role
           FROM users u JOIN workspace_memberships m ON m.user_id=u.id
           WHERE u.id=? AND m.workspace_id=?""",
        (user_id, workspace_id),
    ).fetchone()
    return dict(row) if row else {}


def list_workspace_users(workspace_id: str) -> list[dict]:
    with _services().DB_LOCK, _services().control_db_session() as db:
        rows = db.execute(
            """SELECT u.id,u.username,u.display_name,u.active,u.created_at,m.role
               FROM users u JOIN workspace_memberships m ON m.user_id=u.id
               WHERE m.workspace_id=? ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END,u.username""",
            (workspace_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def update_workspace_user(user_id: str, actor: dict, data: dict) -> dict:
    if not isinstance(data, dict) or not data or set(data) - {"role", "active"}:
        raise ValueError("成员更新只支持 role 和 active")
    if user_id == actor.get("user_id") and data.get("active") is False:
        raise ValueError("不能停用当前登录账号")
    workspace_id = actor.get("workspace_id", "")
    with _services().DB_LOCK, _services().control_db_session() as db:
        membership = db.execute(
            "SELECT role FROM workspace_memberships WHERE workspace_id=? AND user_id=?",
            (workspace_id, user_id),
        ).fetchone()
        if not membership:
            raise LookupError("团队成员不存在")
        if membership["role"] == "owner":
            raise ValueError("团队所有者不可在此操作中降权或停用")
        if (
            user_id == actor.get("user_id")
            and membership["role"] == "admin"
            and data.get("role") not in {None, "admin"}
        ):
            admin_count = db.execute(
                "SELECT COUNT(*) FROM workspace_memberships m JOIN users u ON u.id=m.user_id WHERE m.workspace_id=? AND m.role IN ('owner','admin') AND u.active=1",
                (workspace_id,),
            ).fetchone()[0]
            if admin_count <= 1:
                raise ValueError("工作区至少需要一名可用管理员")
        updates = {}
        if "role" in data:
            if data["role"] not in {"admin", "researcher", "reviewer", "viewer"}:
                raise ValueError("未知的团队角色")
            updates["role"] = data["role"]
        if "active" in data:
            if not isinstance(data["active"], bool):
                raise ValueError("active 必须为布尔值")
            updates["active"] = int(data["active"])
        if "role" in updates:
            db.execute(
                "UPDATE workspace_memberships SET role=? WHERE workspace_id=? AND user_id=?",
                (updates["role"], workspace_id, user_id),
            )
        if "active" in updates:
            db.execute(
                "UPDATE users SET active=?,updated_at=? WHERE id=?",
                (updates["active"], _services().now_iso(), user_id),
            )
            if not updates["active"]:
                db.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
        _services().record_audit(
            db,
            "auth.user_updated",
            "user",
            user_id,
            {
                "fields": sorted(updates),
                "role": updates.get("role", membership["role"]),
            },
        )
        return _services().auth_user_snapshot(db, user_id, workspace_id)


def change_user_password(
    user_id: str, current_password: str, new_password: str
) -> tuple[str, str, dict]:
    new_hash = _services().hash_password(new_password)
    with _services().DB_LOCK, _services().control_db_session() as db:
        row = db.execute(
            "SELECT username,password_hash FROM users WHERE id=? AND active=1",
            (user_id,),
        ).fetchone()
        if not row or not _services().verify_password(
            current_password, row["password_hash"]
        ):
            raise PermissionError("当前密码不正确")
        db.execute(
            "UPDATE users SET password_hash=?,updated_at=? WHERE id=?",
            (new_hash, _services().now_iso(), user_id),
        )
        db.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
        _services().record_audit(db, "auth.password_changed", "user", user_id, {})
        return _services().create_auth_session(db, user_id)


def sha256_file(path: Path) -> str:
    digest = _services().hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "tenant_isolation_enabled",
    "workspace_database_path",
    "workspace_files_dir",
    "workspace_data_dir",
    "active_files_dir",
    "active_visuals_dir",
    "active_backup_dir",
    "isolated_workspace_actors",
    "initialize_workspace_databases",
    "resume_workspace_background_jobs",
    "_workspace_schema_sql",
    "ensure_workspace_database",
    "_active_database_path",
    "postgres_database_url",
    "using_postgres",
    "sqlite_test_backend_enabled",
    "database_table_exists",
    "database_integrity_check",
    "connect",
    "db_session",
    "control_db_session",
    "record_audit",
    "team_auth_enabled",
    "local_auto_login_enabled",
    "create_local_auto_login_session",
    "_postgres_shared_workspace_mode",
    "_canonical_auth_workspace",
    "_auth_workspace",
    "_bootstrap_token_path",
    "_mcp_service_token",
    "_mcp_service_actor",
    "_ensure_auth_bootstrap_token",
    "_auth_owner_exists",
    "create_auth_session",
    "resolve_auth_session",
    "auth_bootstrap_required",
    "bootstrap_team_owner",
    "_auth_login_identity",
    "_auth_login_locked",
    "_record_auth_failure",
    "authenticate_user",
    "create_workspace_user",
    "list_user_workspaces",
    "create_workspace",
    "switch_workspace",
    "auth_user_snapshot",
    "list_workspace_users",
    "update_workspace_user",
    "change_user_password",
    "sha256_file",
]
