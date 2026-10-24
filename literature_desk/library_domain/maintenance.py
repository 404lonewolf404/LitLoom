"""Database health, backup policies, and archive maintenance."""

from __future__ import annotations

import sqlite3

from ..services import app_services as _services

def maintenance_status() -> dict:
    with _services().DB_LOCK, _services().db_session() as db:
        counts = {
            "papers": db.execute("SELECT COUNT(*) FROM papers").fetchone()[0],
            "search_documents": db.execute(
                "SELECT COUNT(*) FROM paper_search_content"
            ).fetchone()[0],
            "page_search_documents": db.execute(
                "SELECT COUNT(*) FROM paper_page_search_content"
            ).fetchone()[0],
            "revisions": db.execute("SELECT COUNT(*) FROM paper_revisions").fetchone()[
                0
            ],
            "attachments": db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0],
            "pages": db.execute("SELECT COUNT(*) FROM document_pages").fetchone()[0],
            "sync_jobs": db.execute("SELECT COUNT(*) FROM sync_jobs").fetchone()[0],
        }
        job_status = {
            row["status"]: row["n"]
            for row in db.execute(
                "SELECT status,COUNT(*) AS n FROM sync_jobs GROUP BY status"
            )
        }
        fts5 = _services().database_table_exists(db, "paper_search") and _services().database_table_exists(db, "paper_page_search")
        schema = db.execute(
            "SELECT value FROM app_meta WHERE key='schema_version'"
        ).fetchone()
        orphan_attachments = db.execute(
            "SELECT COUNT(*) FROM attachments a LEFT JOIN papers p ON p.id=a.paper_id WHERE p.id IS NULL"
        ).fetchone()[0]
        orphan_pages = db.execute(
            "SELECT COUNT(*) FROM document_pages d LEFT JOIN papers p ON p.id=d.paper_id WHERE p.id IS NULL"
        ).fetchone()[0]
        index_ok = counts["papers"] == counts["search_documents"]
        page_index_ok = counts["pages"] == counts["page_search_documents"]
        database_bytes = (
            db.execute("SELECT pg_database_size(current_database())").fetchone()[0]
            if _services().using_postgres()
            else (
                _services().DB_PATH.stat().st_size
                if _services().DB_PATH.exists()
                else 0
            )
        )
    backup_dir = _services().active_backup_dir()
    backups = (
        sorted(
            [
                *backup_dir.glob("researchdesk-backup-*.zip"),
                *backup_dir.glob("researchdesk-auto-*.zip"),
            ],
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        if backup_dir.exists()
        else []
    )
    policy = _services().get_backup_policy()
    return {
        "database": {
            "path": (
                _services().postgres_database_label(
                    _services().postgres_database_url()
                )
                if _services().using_postgres()
                else str(_services().DB_PATH)
            ),
            "schema_version": schema[0] if schema else "unknown",
            "bytes": database_bytes,
            "wal_bytes": (
                _services().Path(str(_services().DB_PATH) + "-wal").stat().st_size
                if not _services().using_postgres()
                and _services().Path(str(_services().DB_PATH) + "-wal").exists()
                else 0
            ),
        },
        "counts": counts,
        "jobs": job_status,
        "index": {
            "fts5": fts5,
            "search_backend": (
                "PostgreSQL pg_trgm"
                if _services().using_postgres()
                else "SQLite FTS5" if fts5 else "字段降级"
            ),
            "documents_match_papers": index_ok,
            "page_documents_match_pages": page_index_ok,
        },
        "orphans": {"attachments": orphan_attachments, "pages": orphan_pages},
        "backups": {
            "automatic_enabled": policy["enabled"],
            "interval_hours": policy["interval_hours"],
            "retention": policy["retention"],
            "count": len(backups),
            "latest": backups[0].name if backups else "",
            "last_success_at": policy["last_success_at"],
            "next_run_at": policy["next_run_at"],
            "last_error": policy["last_error"],
        },
        "checked_at": _services().now_iso(),
    }


def get_backup_policy() -> dict:
    defaults = {
        "enabled": _services().AUTO_BACKUP_INTERVAL_HOURS > 0,
        "interval_hours": max(1, int(_services().AUTO_BACKUP_INTERVAL_HOURS or 24)),
        "retention": _services().BACKUP_RETENTION,
        "last_success_at": "",
        "next_run_at": "",
        "last_error": "",
    }
    with _services().DB_LOCK, _services().db_session() as db:
        row = db.execute(
            "SELECT value FROM app_meta WHERE key='backup_policy'"
        ).fetchone()
    if not row:
        return defaults
    try:
        stored = _services().json.loads(row[0])
    except (_services().json.JSONDecodeError, TypeError):
        return defaults
    if not isinstance(stored, dict):
        return defaults
    result = defaults | {
        key: stored[key]
        for key in (
            "enabled",
            "interval_hours",
            "retention",
            "last_success_at",
            "next_run_at",
            "last_error",
        )
        if key in stored
    }
    result["enabled"] = result["enabled"] is True
    try:
        result["interval_hours"] = min(8760, max(1, int(result["interval_hours"])))
        result["retention"] = min(365, max(1, int(result["retention"])))
    except (TypeError, ValueError):
        return defaults
    for key in ("last_success_at", "next_run_at", "last_error"):
        result[key] = _services().clean_text(result[key], 1000)
    return result


def save_backup_policy(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("备份设置必须是 JSON 对象")
    current = _services().get_backup_policy()
    enabled_value = data.get("enabled", current["enabled"])
    if not isinstance(enabled_value, bool):
        raise ValueError("自动备份开关必须是布尔值")
    try:
        interval = int(data.get("interval_hours", current["interval_hours"]))
        retention = int(data.get("retention", current["retention"]))
    except (TypeError, ValueError):
        raise ValueError("备份间隔和保留数量必须为整数")
    if (
        isinstance(data.get("interval_hours"), float)
        and not data["interval_hours"].is_integer()
    ):
        raise ValueError("备份间隔必须为整数")
    if isinstance(data.get("retention"), float) and not data["retention"].is_integer():
        raise ValueError("保留数量必须为整数")
    if not 1 <= interval <= 8760:
        raise ValueError("自动备份间隔需在 1 到 8760 小时之间")
    if not 1 <= retention <= 365:
        raise ValueError("自动备份保留数量需在 1 到 365 份之间")
    enabled = enabled_value
    updated = current | {
        "enabled": enabled,
        "interval_hours": interval,
        "retention": retention,
        "last_error": "",
    }
    if enabled:
        updated["next_run_at"] = _services().now_iso()
    with _services().DB_LOCK, _services().db_session() as db:
        db.execute(
            "INSERT INTO app_meta(key,value) VALUES('backup_policy',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_services().json.dumps(updated, ensure_ascii=False),),
        )
        _services().record_audit(
            db,
            "backup.policy_updated",
            "backup",
            "automatic",
            {"enabled": enabled, "interval_hours": interval, "retention": retention},
        )
    return updated


def prune_backup_archives(retention: int | None = None) -> int:
    """Keep the configured number of automatic archives; preserve manual exports."""
    backup_dir = _services().active_backup_dir()
    if not backup_dir.exists():
        return 0
    retention = min(
        365,
        max(
            1,
            int(
                retention
                if retention is not None
                else _services().get_backup_policy()["retention"]
            ),
        ),
    )
    archives = sorted(
        backup_dir.glob("researchdesk-auto-*.zip"),
        key=lambda item: item.name,
        reverse=True,
    )
    removed = 0
    for archive in archives[retention:]:
        try:
            archive.unlink()
            removed += 1
        except OSError:
            # A user may be downloading an archive or have it locked on Windows.
            continue
    return removed


def create_backup_archive(automatic: bool = False) -> dict:
    """Create a consistent database/files archive for manual or scheduled backups."""
    with _services().BACKUP_LOCK:
        backup_dir = _services().active_backup_dir()
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = (
            _services().datetime.now().strftime("%Y%m%d-%H%M%S")
            + "-"
            + _services().uuid.uuid4().hex[:8]
        )
        prefix = "researchdesk-auto" if automatic else "researchdesk-backup"
        destination = backup_dir / f"{prefix}-{stamp}.zip"
        temporary_archive = backup_dir / f".{prefix}-{stamp}.tmp"
        database_filename = (
            "litloom-postgresql.dump"
            if _services().using_postgres()
            else "literature.sqlite3"
        )
        copy_path = backup_dir / (
            f"snapshot-{stamp}.dump"
            if _services().using_postgres()
            else f"snapshot-{stamp}.sqlite3"
        )
        try:
            if _services().using_postgres():
                _services().postgres_dump_database(
                    _services().postgres_database_url(), copy_path
                )
            else:
                with _services().DB_LOCK:
                    live = _services().connect()
                    try:
                        target = _services().sqlite3.connect(copy_path)
                        try:
                            live.backup(target)
                        finally:
                            target.close()
                    finally:
                        live.close()
            files = []
            for file in (
                sorted(_services().active_files_dir().glob("*.pdf"))
                if _services().active_files_dir().exists()
                else []
            ):
                files.append(
                    {
                        "path": "files/" + file.name,
                        "sha256": _services().sha256_file(file),
                        "bytes": file.stat().st_size,
                    }
                )
            manifest = {
                "product": "LitLoom",
                "app_version": _services().APP_VERSION,
                "schema_version": _services().DB_SCHEMA_VERSION,
                "created_at": _services().now_iso(),
                "automatic": bool(automatic),
                "database": {
                    "path": database_filename,
                    "sha256": _services().sha256_file(copy_path),
                },
                "files": files,
            }
            with _services().zipfile.ZipFile(
                temporary_archive, "w", _services().zipfile.ZIP_DEFLATED
            ) as zf:
                zf.write(copy_path, database_filename)
                if _services().active_files_dir().exists():
                    for file in _services().active_files_dir().glob("*.pdf"):
                        zf.write(file, "files/" + file.name)
                zf.writestr(
                    "manifest.json",
                    _services().json.dumps(manifest, ensure_ascii=False, indent=2),
                )
            _services().os.replace(temporary_archive, destination)
            with _services().DB_LOCK, _services().db_session() as db:
                _services().record_audit(
                    db,
                    "backup.created",
                    "backup",
                    destination.name,
                    {
                        "automatic": bool(automatic),
                        "attachment_count": len(files),
                        "sha256": manifest["database"]["sha256"],
                    },
                )
            _services().prune_backup_archives(
                _services().get_backup_policy()["retention"]
            )
            return {
                "path": str(destination),
                "filename": destination.name,
                "download_url": "/api/backups/" + destination.name,
                "papers": manifest["database"]["sha256"],
                "attachment_count": len(files),
                "manifest": manifest,
            }
        finally:
            copy_path.unlink(missing_ok=True)
            temporary_archive.unlink(missing_ok=True)


def scheduled_backup_tick() -> None:
    if not _services().AUTO_BACKUP_RUN_LOCK.acquire(blocking=False):
        return
    try:
        policy = _services().get_backup_policy()
        if not policy["enabled"]:
            return
        now = _services().datetime.now(_services().timezone.utc).replace(microsecond=0)
        try:
            next_run = (
                _services().datetime.fromisoformat(policy["next_run_at"])
                if policy["next_run_at"]
                else None
            )
        except ValueError:
            next_run = None
        if next_run and next_run > now:
            return
        try:
            _services().create_backup_archive(automatic=True)
            policy["last_success_at"] = _services().now_iso()
            policy["last_error"] = ""
            policy["next_run_at"] = (
                (
                    _services().datetime.now(_services().timezone.utc)
                    + _services().timedelta(hours=policy["interval_hours"])
                )
                .replace(microsecond=0)
                .isoformat()
            )
        except Exception as exc:
            policy["last_error"] = _services().clean_text(str(exc), 1000)
            retry_hours = min(policy["interval_hours"], 6)
            policy["next_run_at"] = (
                (
                    _services().datetime.now(_services().timezone.utc)
                    + _services().timedelta(hours=retry_hours)
                )
                .replace(microsecond=0)
                .isoformat()
            )
        with _services().DB_LOCK, _services().db_session() as db:
            db.execute(
                "INSERT INTO app_meta(key,value) VALUES('backup_policy',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_services().json.dumps(policy, ensure_ascii=False),),
            )
    finally:
        _services().AUTO_BACKUP_RUN_LOCK.release()


__all__ = ['maintenance_status', 'get_backup_policy', 'save_backup_policy', 'prune_backup_archives', 'create_backup_archive', 'scheduled_backup_tick']
