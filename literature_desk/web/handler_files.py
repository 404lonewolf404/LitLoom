from __future__ import annotations
from ..services import app_services as _services


class _HandlerFilesMixin:
    def save_attachment_bytes(
        self, paper_id: str, filename: str, content: bytes
    ) -> tuple[dict, bool]:
        return _services().store_pdf_attachment(paper_id, filename, content)

    def add_attachment(self, paper_id: str, data: dict):
        try:
            content = __import__("base64").b64decode(
                data.get("data", ""), validate=True
            )
        except Exception:
            raise ValueError("PDF 文件数据无法读取")
        result, deduplicated = self.save_attachment_bytes(
            paper_id, data.get("filename", "paper.pdf"), content
        )
        return self.send_json(result, 200 if deduplicated else 201)

    def download_pdf(self, paper_id: str, data: dict):
        """Download a paper's public PDF into the managed attachment store."""
        return self.send_json(
            _services().download_public_pdf(paper_id, data.get("url", ""))
        )

    def create_backup(self):
        return self.send_json(_services().create_backup_archive())

    def restore_backup(self, data):
        filename = _services().clean_text(data.get("filename"), 250)
        if _services().Path(filename).name != filename or not filename.lower().endswith(
            ".zip"
        ):
            raise ValueError("备份文件名无效")
        content = __import__("base64").b64decode(data.get("data", ""), validate=True)
        if len(content) > 2 * 1024 * 1024 * 1024:
            raise ValueError("备份包超过 2 GB 限制")
        backup_dir = _services().active_backup_dir()
        temp = backup_dir / ("restore-" + str(_services().uuid.uuid4()) + ".zip")
        backup_dir.mkdir(parents=True, exist_ok=True)
        temp.write_bytes(content)
        try:
            with _services().zipfile.ZipFile(temp, "r") as zf:
                names = zf.namelist()
                if "manifest.json" not in names:
                    raise ValueError("备份缺少 manifest")
                manifest = _services().json.loads(zf.read("manifest.json"))
                database_member = manifest.get("database", {}).get(
                    "path", "literature.sqlite3"
                )
                if database_member not in names:
                    raise ValueError("备份缺少数据库文件")
                db_bytes = zf.read(database_member)
                if _services().hashlib.sha256(db_bytes).hexdigest() != manifest.get(
                    "database", {}
                ).get("sha256"):
                    raise ValueError("数据库校验失败，未恢复")
                # Reject paths that could escape the selected data directories.
                for name in names:
                    p = _services().Path(name)
                    if p.is_absolute() or ".." in p.parts:
                        raise ValueError("备份包含非法路径")
                if _services().using_postgres():
                    if not database_member.endswith(".dump"):
                        raise ValueError(
                            "当前数据库是 PostgreSQL；该 SQLite 备份需先迁移到 PostgreSQL，不能直接覆盖恢复"
                        )
                    staged_dump = backup_dir / (
                        "staged-" + str(_services().uuid.uuid4()) + ".dump"
                    )
                    staged_dump.write_bytes(db_bytes)
                    staged_files = []
                    for info in manifest.get("files", []):
                        rel = info.get("path", "")
                        if rel not in names or not rel.startswith("files/"):
                            continue
                        payload = zf.read(rel)
                        if _services().hashlib.sha256(payload).hexdigest() != info.get(
                            "sha256"
                        ):
                            staged_dump.unlink(missing_ok=True)
                            raise ValueError(f"附件校验失败：{rel}")
                        staged_files.append((_services().Path(rel).name, payload))
                    try:
                        _services().postgres_validate_dump(staged_dump)
                        safety = backup_dir / (
                            "before-restore-"
                            + _services().datetime.now().strftime("%Y%m%d-%H%M%S")
                            + ".dump"
                        )
                        _services().postgres_dump_database(
                            _services().postgres_database_url(), safety
                        )
                        _services().postgres_restore_dump(
                            _services().postgres_database_url(), staged_dump
                        )
                        _services().active_files_dir().mkdir(
                            parents=True, exist_ok=True
                        )
                        for name, payload in staged_files:
                            (_services().active_files_dir() / name).write_bytes(payload)
                        _services().init_db()
                        with _services().db_session() as db:
                            _services().record_audit(
                                db,
                                "backup.restored",
                                "backup",
                                filename,
                                {
                                    "attachments_restored": len(staged_files),
                                    "source_schema_version": manifest.get(
                                        "schema_version"
                                    ),
                                },
                            )
                        return self.send_json(
                            {
                                "restored": True,
                                "papers_checked": manifest["database"]["sha256"],
                                "attachments_restored": len(staged_files),
                                "safety_copy_created": True,
                            }
                        )
                    finally:
                        staged_dump.unlink(missing_ok=True)
                if database_member != "literature.sqlite3":
                    raise ValueError("当前 SQLite 恢复只接受 SQLite 格式备份")
                staged_db = backup_dir / (
                    "staged-" + str(_services().uuid.uuid4()) + ".sqlite3"
                )
                staged_db.write_bytes(db_bytes)
                check = _services().sqlite3.connect(staged_db)
                integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
                check.close()
                if integrity != "ok":
                    staged_db.unlink(missing_ok=True)
                    raise ValueError("SQLite 完整性检查失败")
                staged_files = []
                for info in manifest.get("files", []):
                    rel = info.get("path", "")
                    if rel not in names or not rel.startswith("files/"):
                        continue
                    payload = zf.read(rel)
                    if _services().hashlib.sha256(payload).hexdigest() != info.get(
                        "sha256"
                    ):
                        staged_db.unlink(missing_ok=True)
                        raise ValueError(f"附件校验失败：{rel}")
                    staged_files.append((_services().Path(rel).name, payload))
                with _services().DB_LOCK:
                    selected_db = _services()._active_database_path()
                    if selected_db.exists():
                        safety = (
                            backup_dir
                            / f"before-restore-{_services().datetime.now().strftime('%Y%m%d-%H%M%S')}.sqlite3"
                        )
                        live = _services().connect(selected_db)
                        safe = _services().sqlite3.connect(safety)
                        live.backup(safe)
                        safe.close()
                        live.close()
                    _services().os.replace(staged_db, selected_db)
                    _services().active_files_dir().mkdir(parents=True, exist_ok=True)
                    for name, payload in staged_files:
                        (_services().active_files_dir() / name).write_bytes(payload)
                    # Backups from older releases may predate new local
                    # memory tables; rerun the idempotent schema initializer
                    # after swapping the verified database into place.
                    _services().init_db()
                    with _services().db_session() as db:
                        _services().record_audit(
                            db,
                            "backup.restored",
                            "backup",
                            filename,
                            {
                                "attachments_restored": len(staged_files),
                                "source_schema_version": manifest.get("schema_version"),
                            },
                        )
                return self.send_json(
                    {
                        "restored": True,
                        "papers_checked": manifest.get("database", {}).get("sha256"),
                        "attachments_restored": len(staged_files),
                        "safety_copy_created": True,
                    }
                )
        finally:
            temp.unlink(missing_ok=True)
