"""HTTP request handling and API route adaptation."""

from __future__ import annotations
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..services import app_services as _services


class Handler(BaseHTTPRequestHandler):
    server_version = "LitLoom"

    def handle_one_request(self):
        self.request_id = _services().uuid.uuid4().hex[:12]
        self.request_started_perf = _services().time.perf_counter()
        super().handle_one_request()

    def log_message(self, fmt, *args):
        # Local-first app: structured request logs never include bodies or credentials.
        started = getattr(self, "request_started_perf", None)
        duration_ms = (
            round((_services().time.perf_counter() - started) * 1000, 2)
            if started
            else None
        )
        message = fmt % args
        status_match = _services().re.search(r"\"\s+(\d{3})\s+", message)
        status = int(status_match.group(1)) if status_match else 0
        _services().record_runtime_request(
            getattr(self, "command", ""),
            getattr(self, "path", ""),
            status,
            duration_ms or 0,
        )
        print(
            _services().json.dumps(
                {
                    "ts": _services().now_iso(),
                    "level": "info",
                    "request_id": getattr(self, "request_id", ""),
                    "client": self.address_string(),
                    "duration_ms": duration_ms,
                    "message": message,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    def send_json(self, value, status=200, headers=None):
        # Keep the historic ``error`` field for existing clients while adding
        # a stable machine-readable code and request correlation id. This is a
        # transport concern, so routes do not need to repeat it individually.
        if status >= 400 and isinstance(value, dict) and value.get("error"):
            value = {
                **value,
                "error_code": value.get("error_code") or _services().status_error_code(status),
                "retryable": bool(value.get("retryable", status in {408, 429, 502, 503, 504})),
                "request_id": value.get("request_id") or getattr(self, "request_id", ""),
            }
        data = _services().json.dumps(value, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Request-ID", getattr(self, "request_id", ""))
            for key, header_value in (headers or {}).items():
                for item in (
                    header_value
                    if isinstance(header_value, (list, tuple))
                    else (header_value,)
                ):
                    self.send_header(str(key), str(item))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # A browser refresh or navigation can close the socket while a
            # response is being written. The request is already complete from
            # the server's perspective, so avoid a noisy traceback.
            return

    def _handle_unexpected_request_error(self, exc: Exception):
        """Keep an unexpected route failure JSON-shaped and correlated.

        Expected domain failures are handled close to their route. This final
        transport boundary protects clients from a dropped socket when a new
        route forgets a catch, while the server log keeps only the exception
        type and request id for follow-up diagnostics.
        """
        print(
            _services().json.dumps(
                {
                    "ts": _services().now_iso(),
                    "level": "error",
                    "event": "request.unhandled_error",
                    "request_id": getattr(self, "request_id", ""),
                    "method": getattr(self, "command", ""),
                    "path": _services().urlparse(getattr(self, "path", "")).path,
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return self.send_json(
            _services().error_payload(
                "服务器内部错误",
                500,
                request_id=getattr(self, "request_id", ""),
                code="internal_error",
            ),
            500,
        )

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 270 * 1024 * 1024:
            raise ValueError("请求体过大（当前单次上传上限约 270 MB）")
        body = self.rfile.read(length) if length else b"{}"
        return _services().json.loads(body.decode("utf-8"))

    def read_pdf_ingest_request(self):
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            data = self.read_json()
            return data.get("files") or [], data.get("options") or {}
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 190 * 1024 * 1024:
            raise ValueError("批量 PDF 请求为空或超过 190 MB，请分批上传")
        parser = _services().BytesFeedParser(policy=_services().default)
        parser.feed(
            ("Content-Type: " + content_type + "\r\nMIME-Version: 1.0\r\n\r\n").encode(
                "utf-8"
            )
        )
        remaining = length
        while remaining:
            chunk = self.rfile.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError("PDF 上传中断，未收到完整文件")
            parser.feed(chunk)
            remaining -= len(chunk)
        message = parser.close()
        files = []
        options = {"auto_ai": True}
        total_size = 0
        staged_paths = []
        try:
            staging = _services().DATA_DIR / "ingest-staging"
            staging.mkdir(parents=True, exist_ok=True)
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition") or ""
                filename = part.get_filename()
                if filename:
                    if not filename.lower().endswith(".pdf"):
                        continue
                    payload = part.get_payload(decode=True) or b""
                    total_size += len(payload)
                    if (
                        len(files) >= 200
                        or len(payload) > 100 * 1024 * 1024
                        or total_size > 180 * 1024 * 1024
                    ):
                        raise ValueError(
                            "PDF 数量或总大小超过限制（单篇 100 MB，批次 180 MB）"
                        )
                    staged = staging / (
                        "upload-" + str(_services().uuid.uuid4()) + ".pdf"
                    )
                    staged.write_bytes(payload)
                    staged_paths.append(staged)
                    files.append(
                        {"filename": filename, "staged_upload_path": str(staged)}
                    )
                    del payload
                else:
                    value = part.get_content().strip()
                    if name == "auto_ai":
                        options["auto_ai"] = value.casefold() in {
                            "true",
                            "1",
                            "on",
                            "yes",
                        }
                    elif name == "topic":
                        options["topic"] = _services().clean_text(value, 300)
            if not files:
                raise ValueError("请至少选择一个 PDF 文件")
            return files, options
        except Exception:
            for staged in staged_paths:
                staged.unlink(missing_ok=True)
            raise

    def _auth_cookie_values(
        self, session_token: str, csrf_token: str, *, clear: bool = False
    ) -> list[str]:
        if clear:
            return [
                "litloom_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0",
                "litloom_csrf=; Path=/; SameSite=Strict; Max-Age=0",
            ]
        secure = (
            "; Secure"
            if self.headers.get("X-Forwarded-Proto", "").casefold() == "https"
            else ""
        )
        return [
            f"litloom_session={session_token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={_services().AUTH_SESSION_TTL_DAYS * 86400}{secure}",
            f"litloom_csrf={csrf_token}; Path=/; SameSite=Strict; Max-Age={_services().AUTH_SESSION_TTL_DAYS * 86400}{secure}",
        ]

    def _session_cookie(self) -> str:
        cookie = _services().SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        return (
            cookie.get("litloom_session").value if cookie.get("litloom_session") else ""
        )

    def _csrf_cookie(self) -> str:
        cookie = _services().SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        return cookie.get("litloom_csrf").value if cookie.get("litloom_csrf") else ""

    def _begin_auth_request(self) -> bool:
        self.auth_user = None
        self.auth_session_token = ""
        self.auth_via_mcp_token = False
        if not _services().team_auth_enabled():
            return True
        path = _services().urlparse(self.path).path
        method = self.command.upper()
        if (
            method == "GET"
            and (
                path
                in {"/", "/index.html", "/api/health", "/api/ready", "/api/auth/status"}
                or path.startswith("/static/")
            )
            or method == "POST"
            and path in {"/api/auth/login", "/api/auth/bootstrap"}
        ):
            return True
        session_token = self._session_cookie()
        actor = None
        authorization = self.headers.get("Authorization", "")
        if authorization.casefold().startswith("bearer "):
            actor = _services()._mcp_service_actor(authorization[7:].strip())
            self.auth_via_mcp_token = bool(actor)
        if not actor:
            actor = _services().resolve_auth_session(session_token)
        if not actor:
            self.send_json({"error": "请先登录团队工作区"}, 401)
            return False
        self.auth_user = actor
        self.auth_session_token = session_token
        if (
            method not in {"GET", "HEAD", "OPTIONS"}
            and path not in {"/api/auth/login", "/api/auth/bootstrap"}
            and not self.auth_via_mcp_token
        ):
            csrf_cookie = self._csrf_cookie()
            csrf_header = self.headers.get("X-LitLoom-CSRF", "")
            if (
                not csrf_cookie
                or not csrf_header
                or not _services().constant_time_token_match(csrf_cookie, csrf_header)
                or not _services().constant_time_token_match(
                    _services().token_digest(csrf_cookie), actor.get("csrf_hash", "")
                )
            ):
                self.send_json({"error": "安全令牌已失效，请刷新页面后重试"}, 403)
                return False
        role = actor.get("role", "viewer")
        if (
            path == "/api/workspace/members"
            or path.startswith("/api/workspace/members/")
            or (path == "/api/workspaces" and method != "GET")
        ):
            if role not in {"owner", "admin"}:
                self.send_json({"error": "只有工作区管理员可以管理成员"}, 403)
                return False
        if (path == "/api/audit" or path.startswith("/api/backups/")) and role not in {
            "owner",
            "admin",
        }:
            self.send_json({"error": "审计记录和完整备份仅限工作区管理员访问"}, 403)
            return False
        if method not in {"GET", "HEAD", "OPTIONS"}:
            admin_only = path in {
                "/api/settings/model",
                "/api/settings/model/test",
                "/api/settings/backup-policy",
                "/api/backup",
                "/api/backup/restore",
                "/api/workspaces",
            } or path.startswith("/api/maintenance/")
            if admin_only and role not in {"owner", "admin"}:
                self.send_json({"error": "此操作仅限工作区管理员"}, 403)
                return False
            account_action = path in {
                "/api/auth/logout",
                "/api/auth/password",
                "/api/auth/switch-workspace",
            }
            if role == "viewer" and not account_action:
                self.send_json({"error": "当前账号只有查看权限"}, 403)
                return False
            if (
                role == "reviewer"
                and not account_action
                and not (
                    _services().re.fullmatch(r"/api/runs/[^/]+/candidates/[^/]+", path)
                    or _services().re.fullmatch(r"/api/papers/[^/]+/feedback", path)
                    or _services().re.fullmatch(r"/api/study-items/[^/]+/review", path)
                )
            ):
                self.send_json({"error": "当前账号仅能提交评审反馈"}, 403)
        return True

    def do_GET(self):
        if not self._begin_auth_request():
            return
        token = _services().AUTH_ACTOR.set(self.auth_user)
        try:
            try:
                return self._handle_get()
            except Exception as exc:
                return self._handle_unexpected_request_error(exc)
        finally:
            _services().AUTH_ACTOR.reset(token)

    def _handle_get(self):
        parsed = _services().urlparse(self.path)
        path = parsed.path
        if path == "/api/auth/status":
            if not _services().team_auth_enabled():
                return self.send_json({"enabled": False, "authenticated": True})
            actor = _services().resolve_auth_session(self._session_cookie())
            bootstrap_required = _services().auth_bootstrap_required()
            user = (
                {
                    key: actor[key]
                    for key in (
                        "user_id",
                        "username",
                        "display_name",
                        "workspace_id",
                        "workspace_name",
                        "role",
                    )
                }
                if actor
                else None
            )
            return self.send_json(
                {
                    "enabled": True,
                    "authenticated": bool(actor),
                    "bootstrap_required": bootstrap_required,
                    "bootstrap_token_path": (
                        str(_services()._bootstrap_token_path())
                        if bootstrap_required
                        else ""
                    ),
                    "user": user,
                }
            )
        if path == "/api/workspace/members":
            return self.send_json(
                {
                    "members": _services().list_workspace_users(
                        self.auth_user["workspace_id"]
                    )
                }
            )
        if path == "/api/workspaces":
            return self.send_json(
                {
                    "workspaces": _services().list_user_workspaces(
                        self.auth_user["user_id"]
                    ),
                    "active_workspace_id": self.auth_user.get("workspace_id", ""),
                }
            )
        if path == "/" or path == "/index.html":
            return self.serve_file(
                _services().STATIC / "index.html", "text/html; charset=utf-8"
            )
        if path.startswith("/static/"):
            target = (_services().STATIC / path.removeprefix("/static/")).resolve()
            if _services().STATIC.resolve() not in target.parents:
                return self.send_error(404)
            return self.serve_file(target)
        if path.startswith("/files/"):
            name = _services().Path(path.removeprefix("/files/")).name
            target = _services().active_files_dir() / name
            return self.serve_file(target, "application/pdf")
        if path.startswith("/visual-pages/"):
            name = _services().Path(path.removeprefix("/visual-pages/")).name
            if not name.lower().endswith((".jpg", ".jpeg")):
                return self.send_error(404)
            target = _services().active_visuals_dir() / name
            return self.serve_file(target, "image/jpeg")
        if path.startswith("/api/backups/"):
            name = _services().Path(path.removeprefix("/api/backups/")).name
            backup_dir = _services().active_backup_dir()
            target = backup_dir / name
            if (
                not name.endswith(".zip")
                or backup_dir.resolve() not in target.resolve().parents
            ):
                return self.send_error(404)
            return self.serve_file(target, "application/zip")
        if path == "/api/state":
            return self.get_state()
        if path == "/api/pdf-ingest":
            return self.send_json(_services().list_pdf_ingest_jobs())
        if path == "/api/metadata-draft-jobs":
            try:
                limit = min(
                    max(
                        int(
                            (_services().parse_qs(parsed.query).get("limit") or ["10"])[
                                0
                            ]
                        ),
                        1,
                    ),
                    50,
                )
            except (TypeError, ValueError):
                limit = 10
            with _services().DB_LOCK, _services().db_session() as db:
                rows = db.execute(
                    "SELECT * FROM metadata_draft_jobs ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return self.send_json(
                {"jobs": [_services().metadata_draft_job_snapshot(row) for row in rows]}
            )
        metadata_job_match = _services().re.fullmatch(
            r"/api/metadata-draft-jobs/([^/]+)", path
        )
        if metadata_job_match:
            job = _services().get_metadata_draft_job(
                _services().unquote(metadata_job_match.group(1))
            )
            return self.send_json(
                job or {"error": "元数据识别任务不存在"}, 200 if job else 404
            )
        ingest_match = _services().re.fullmatch(r"/api/pdf-ingest/([^/]+)", path)
        if ingest_match:
            snapshot = _services().pdf_ingest_job_snapshot(
                _services().unquote(ingest_match.group(1))
            )
            return self.send_json(snapshot, 200 if snapshot else 404)
        semantic_job_match = _services().re.fullmatch(
            r"/api/semantic-index-jobs/([^/]+)", path
        )
        if semantic_job_match:
            job = _services().get_semantic_index_job(
                _services().unquote(semantic_job_match.group(1))
            )
            return self.send_json(
                job or {"error": "语义索引任务不存在"}, 200 if job else 404
            )
        if path in {"/api/health", "/api/ready"}:
            health = _services().health_summary()
            return self.send_json(health, 200 if health["ready"] else 503)
        if path == "/api/audit":
            try:
                limit = min(
                    max(
                        int(
                            (
                                _services().parse_qs(parsed.query).get("limit")
                                or ["100"]
                            )[0]
                        ),
                        1,
                    ),
                    500,
                )
            except (TypeError, ValueError):
                limit = 100
            event_type = _services().clean_text(
                (_services().parse_qs(parsed.query).get("event_type") or [""])[0], 120
            )
            with _services().DB_LOCK, _services().db_session() as db:
                if event_type:
                    rows = [
                        dict(row)
                        for row in db.execute(
                            "SELECT * FROM audit_events WHERE event_type=? ORDER BY id DESC LIMIT ?",
                            (event_type, limit),
                        )
                    ]
                else:
                    rows = [
                        dict(row)
                        for row in db.execute(
                            "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?",
                            (limit,),
                        )
                    ]
            for row in rows:
                try:
                    row["payload"] = _services().json.loads(
                        row.pop("payload_json", "{}")
                    )
                except _services().json.JSONDecodeError:
                    row["payload"] = {}
            return self.send_json(rows)
        if path == "/api/papers":
            return self.get_papers(_services().parse_qs(parsed.query))
        if path == "/api/study-items":
            params = _services().parse_qs(parsed.query)
            try:
                limit = int((params.get("limit") or ["100"])[0])
                offset = int((params.get("offset") or ["0"])[0])
            except (TypeError, ValueError):
                limit, offset = 100, 0
            return self.send_json(
                _services().study_items_page(
                    (params.get("q") or [""])[0],
                    (params.get("mastery") or [""])[0],
                    (params.get("type") or [""])[0],
                    limit,
                    offset,
                    (params.get("due") or [""])[0].lower() in {"1", "true", "yes"},
                )
            )
        if path == "/api/library":
            params = _services().parse_qs(parsed.query)
            try:
                limit = int((params.get("limit") or ["50"])[0])
                offset = int((params.get("offset") or ["0"])[0])
            except (TypeError, ValueError):
                limit, offset = 50, 0
            filters = {
                "favorite": (params.get("favorite") or [""])[0].lower()
                in {"1", "true", "yes"},
                "has_reading_card": (params.get("has_reading_card") or [""])[0].lower()
                in {"1", "true", "yes"},
                "year_from": (params.get("year_from") or [""])[0],
                "year_to": (params.get("year_to") or [""])[0],
                "sort": (params.get("sort") or [""])[0],
            }
            result = _services().search_library(
                (params.get("q") or [""])[0],
                limit,
                offset,
                (params.get("status") or [""])[0],
                (params.get("topic") or [""])[0],
                filters,
            )
            with _services().DB_LOCK, _services().db_session() as db:
                topics = [
                    row[0]
                    for row in db.execute(
                        "SELECT DISTINCT topic FROM papers WHERE topic<>'' ORDER BY topic COLLATE NOCASE"
                    ).fetchall()
                ]
            result["items"] = result.pop("results")
            result["topics"] = topics
            return self.send_json(result)
        if path == "/api/search":
            params = _services().parse_qs(parsed.query)
            try:
                limit = int((params.get("limit") or ["40"])[0])
            except (TypeError, ValueError):
                limit = 40
            try:
                offset = int((params.get("offset") or ["0"])[0])
            except (TypeError, ValueError):
                offset = 0
            return self.send_json(
                _services().search_library(
                    (params.get("q") or [""])[0],
                    limit,
                    offset,
                    (params.get("status") or [""])[0],
                    (params.get("topic") or [""])[0],
                )
            )
        if path == "/api/maintenance/status":
            return self.send_json(_services().maintenance_status())
        if path == "/api/maintenance/orphans":
            status = _services().maintenance_status()
            return self.send_json(status.get("orphans", {}))
        if path == "/api/recommendations":
            try:
                limit = int(
                    (_services().parse_qs(parsed.query).get("limit") or ["12"])[0]
                )
            except (TypeError, ValueError):
                limit = 12
            return self.send_json(_services().research_memory(limit))
        if path == "/api/agent-tools":
            return self.send_json(
                {
                    "tools": [
                        {"name": name, **spec}
                        for name, spec in _services().AGENT_TOOL_SPECS.items()
                    ],
                    "workflows": sorted(_services().AGENT_WORKFLOWS),
                    "autonomy": {
                        "Read": "automatic",
                        "Draft": "automatic",
                        "Apply": "confirmation_required",
                        "External": "confirmation_required",
                    },
                }
            )
        if path == "/api/research-goals":
            return self.send_json(_services().list_research_goals())
        goal_detail_match = _services().re.fullmatch(
            r"/api/research-goals/([^/]+)", path
        )
        if goal_detail_match:
            goal = _services().get_research_goal(goal_detail_match.group(1))
            return self.send_json(
                goal or {"error": "研究目标不存在"}, 200 if goal else 404
            )
        goal_runs_match = _services().re.fullmatch(
            r"/api/research-goals/([^/]+)/runs", path
        )
        if goal_runs_match:
            goal_id = goal_runs_match.group(1)
            if not _services().get_research_goal(goal_id):
                return self.send_json({"error": "研究目标不存在"}, 404)
            with _services().DB_LOCK, _services().db_session() as db:
                runs = [
                    _services().agent_run_snapshot(row["id"], include_steps=False)
                    for row in db.execute(
                        "SELECT id FROM agent_runs WHERE goal_id=? ORDER BY created_at DESC LIMIT 50",
                        (goal_id,),
                    )
                ]
            return self.send_json(runs)
        agent_run_match = _services().re.fullmatch(r"/api/agent-runs/([^/]+)", path)
        if agent_run_match:
            snapshot = _services().agent_run_snapshot(agent_run_match.group(1))
            return self.send_json(
                snapshot or {"error": "Agent 运行不存在"}, 200 if snapshot else 404
            )
        agent_steps_match = _services().re.fullmatch(
            r"/api/agent-runs/([^/]+)/steps", path
        )
        if agent_steps_match:
            run = _services().agent_run_snapshot(agent_steps_match.group(1))
            return self.send_json(
                (
                    {
                        "run_id": agent_steps_match.group(1),
                        "steps": run.get("steps", []),
                    }
                    if run
                    else {"error": "Agent 运行不存在"}
                ),
                200 if run else 404,
            )
        agent_artifacts_match = _services().re.fullmatch(
            r"/api/agent-runs/([^/]+)/artifacts", path
        )
        if agent_artifacts_match:
            run = _services().agent_run_snapshot(agent_artifacts_match.group(1))
            return self.send_json(
                (
                    {
                        "run_id": agent_artifacts_match.group(1),
                        "artifacts": run.get("artifacts", []),
                    }
                    if run
                    else {"error": "Agent 运行不存在"}
                ),
                200 if run else 404,
            )
        goal_claims_match = _services().re.fullmatch(
            r"/api/research-goals/([^/]+)/claims", path
        )
        if goal_claims_match:
            goal_id = goal_claims_match.group(1)
            with _services().DB_LOCK, _services().db_session() as db:
                claims = []
                for row in db.execute(
                    "SELECT * FROM research_claims WHERE goal_id=? ORDER BY updated_at DESC",
                    (goal_id,),
                ):
                    claim = dict(row)
                    claim["evidence"] = [
                        dict(evidence)
                        for evidence in db.execute(
                            "SELECT * FROM evidence_spans WHERE claim_id=? ORDER BY page_number",
                            (row["id"],),
                        )
                    ]
                    claims.append(claim)
            return self.send_json({"goal_id": goal_id, "claims": claims})
        goal_papers_match = _services().re.fullmatch(
            r"/api/research-goals/([^/]+)/papers", path
        )
        if goal_papers_match:
            goal_id = goal_papers_match.group(1)
            with _services().DB_LOCK, _services().db_session() as db:
                rows = [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM research_goal_papers WHERE goal_id=? ORDER BY last_seen_at DESC",
                        (goal_id,),
                    )
                ]
            for row in rows:
                row["metadata"] = _services()._json_value(
                    row.pop("metadata_json", ""), {}
                )
            return self.send_json({"goal_id": goal_id, "papers": rows})
        goal_critiques_match = _services().re.fullmatch(
            r"/api/research-goals/([^/]+)/critiques", path
        )
        if goal_critiques_match:
            goal_id = goal_critiques_match.group(1)
            with _services().DB_LOCK, _services().db_session() as db:
                rows = [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM claim_critiques WHERE goal_id=? ORDER BY created_at DESC",
                        (goal_id,),
                    )
                ]
            for row in rows:
                row["evidence"] = _services()._json_value(
                    row.pop("evidence_json", ""), {}
                )
            return self.send_json({"goal_id": goal_id, "critiques": rows})
        if path == "/api/queries":
            with _services().DB_LOCK, _services().db_session() as db:
                rows = [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM saved_queries ORDER BY active DESC,updated_at DESC"
                    )
                ]
            for item in rows:
                item["sources"] = _services().json.loads(item.pop("sources_json"))
                try:
                    item["search_options"] = _services().json.loads(
                        item.pop("search_options_json", "{}") or "{}"
                    )
                except _services().json.JSONDecodeError:
                    item["search_options"] = {}
                item["active"] = bool(item["active"])
            return self.send_json(rows)
        if path.startswith("/api/queries/") and path.endswith("/matches"):
            query_id = path.split("/")[3]
            with _services().DB_LOCK, _services().db_session() as db:
                rows = [
                    dict(r)
                    for r in db.execute(
                        """SELECT qm.*,p.title,p.year,p.status
                    FROM query_matches qm JOIN papers p ON p.id=qm.paper_id
                    WHERE qm.query_id=? ORDER BY qm.last_seen_at DESC""",
                        (query_id,),
                    )
                ]
            for item in rows:
                item["explanation"] = _services().json.loads(
                    item.pop("explanation_json", "[]")
                )
            return self.send_json(rows)
        if path.startswith("/api/papers/") and path.endswith("/references"):
            paper_id = path.split("/")[3]
            with _services().DB_LOCK, _services().db_session() as db:
                rows = [
                    dict(r)
                    for r in db.execute(
                        "SELECT target_external_id,provider,discovered_at,last_seen_at FROM citation_edges WHERE source_paper_id=? ORDER BY target_external_id",
                        (paper_id,),
                    )
                ]
            return self.send_json({"references": rows, "count": len(rows)})
        if path.startswith("/api/papers/") and path.endswith("/ai-analysis"):
            paper_id = path.split("/")[3]
            try:
                limit = min(
                    max(
                        int(
                            (_services().parse_qs(parsed.query).get("limit") or ["10"])[
                                0
                            ]
                        ),
                        1,
                    ),
                    50,
                )
            except (TypeError, ValueError):
                limit = 10
            with _services().DB_LOCK, _services().db_session() as db:
                if not db.execute(
                    "SELECT 1 FROM papers WHERE id=?", (paper_id,)
                ).fetchone():
                    return self.send_json({"error": "论文不存在"}, 404)
                rows = [
                    dict(row)
                    for row in db.execute(
                        "SELECT id,artifact_type,provider,model,prompt_version,input_sha256,answer,created_at "
                        "FROM ai_artifacts WHERE paper_id=? AND artifact_type IN ('abstract_brief','reading_card_draft','deep_read','prereading_guide') "
                        "ORDER BY created_at DESC LIMIT ?",
                        (paper_id, limit),
                    )
                ]
                # Keep the latest lightweight abstract brief visible even after
                # many full-text analyses have filled the bounded history page.
                if not any(
                    row.get("artifact_type") == "abstract_brief" for row in rows
                ):
                    latest_brief = db.execute(
                        "SELECT id,artifact_type,provider,model,prompt_version,input_sha256,answer,created_at "
                        "FROM ai_artifacts WHERE paper_id=? AND artifact_type='abstract_brief' "
                        "ORDER BY created_at DESC LIMIT 1",
                        (paper_id,),
                    ).fetchone()
                    if latest_brief:
                        rows.append(dict(latest_brief))
                        rows.sort(
                            key=lambda row: row.get("created_at", ""), reverse=True
                        )
                page_rows = [
                    dict(row)
                    for row in db.execute(
                        "SELECT attachment_id,page_number,text FROM document_pages WHERE paper_id=? ORDER BY attachment_id,page_number",
                        (paper_id,),
                    )
                ]
            for row in rows:
                raw_answer = row.pop("answer", "")
                if row["artifact_type"] == "abstract_brief":
                    row["answer"] = raw_answer
                    row["analysis"] = {
                        "answer": raw_answer,
                        "basis": "标题、摘要和元数据；非全文证据",
                        "model": row.get("model", ""),
                    }
                    continue
                try:
                    stored = _services().json.loads(raw_answer)
                    if isinstance(stored, dict) and isinstance(
                        stored.get("analysis"), dict
                    ):
                        analysis = stored["analysis"]
                        meta = (
                            stored.get("meta", {})
                            if isinstance(stored.get("meta"), dict)
                            else {}
                        )
                        row.update(meta)
                    else:
                        analysis, meta = stored, {}
                    if not isinstance(analysis, dict):
                        analysis = {"raw": _services().clean_text(str(analysis), 20000)}
                    if row["artifact_type"] in {"reading_card_draft", "deep_read"}:
                        rendered_pages = {
                            (
                                _services().clean_text(
                                    item.get("attachment_id", ""), 100
                                ),
                                int(item.get("page") or 0),
                            )
                            for item in meta.get("visual_page_refs", [])
                            if isinstance(item, dict)
                            and item.get("attachment_id")
                            and item.get("page")
                        }
                        _services().validate_ai_evidence(
                            analysis, page_rows, rendered_pages
                        )
                        if row["artifact_type"] == "deep_read":
                            row[
                                "unverified_visual_claim_count"
                            ] = _services().restrict_visual_analysis_to_verified_pages(
                                analysis, rendered_pages
                            )
                            row["visual_recheck_attempt_count"] = int(
                                meta.get("visual_recheck_attempt_count")
                                or sum(
                                    1
                                    for item in meta.get("model_calls", [])
                                    if isinstance(item, dict)
                                    and item.get("phase") == "visual_recheck"
                                )
                            )
                        if analysis.get("analysis_status") == "partial":
                            findings = analysis.get("partial_findings")
                            if isinstance(findings, list):
                                analysis["partial_findings"] = [
                                    item
                                    for item in findings
                                    if isinstance(item, dict)
                                    and item.get("evidence_spans")
                                ]
                        validation_error = ""
                        try:
                            _services().validate_fulltext_analysis(
                                (
                                    "reading_card"
                                    if row["artifact_type"] == "reading_card_draft"
                                    else "deep_read"
                                ),
                                analysis,
                            )
                        except ValueError as exc:
                            validation_error = _services().clean_text(str(exc), 300)
                        if validation_error:
                            analysis["analysis_status"] = "legacy_unverified"
                            analysis.setdefault("uncertainties", []).append(
                                "此历史结果未通过当前结构与页码证据校验；请回看原 PDF 或重新生成分析。"
                            )
                            row["analysis_status"] = "legacy_unverified"
                            row["analysis_warning"] = validation_error
                    elif row["artifact_type"] == "prereading_guide":
                        guide = (
                            analysis.get("prereading_guide")
                            if isinstance(analysis.get("prereading_guide"), dict)
                            else analysis
                        )
                        guide = _services().normalize_prereading_guide(guide)
                        evidence_holder = {
                            "evidence": guide.get("evidence", []),
                            "uncertainties": [],
                        }
                        _services().validate_ai_evidence(evidence_holder, page_rows)
                        guide["evidence"] = evidence_holder["evidence"]
                        if evidence_holder["uncertainties"]:
                            guide["uncertainties"] = list(
                                dict.fromkeys(
                                    [
                                        *guide.get("uncertainties", []),
                                        *evidence_holder["uncertainties"],
                                    ]
                                )
                            )
                        if analysis is not stored:
                            analysis["prereading_guide"] = guide
                        else:
                            analysis = {"prereading_guide": guide}
                        if not _services().prereading_guide_has_content(guide):
                            row["analysis_status"] = "legacy_unverified"
                            row["analysis_warning"] = (
                                "此历史读前准备没有可用的先修概念、术语、符号或自测内容。"
                            )
                    row["analysis"] = analysis
                except (TypeError, _services().json.JSONDecodeError):
                    row["analysis"] = {"raw": raw_answer}
                    row["analysis_status"] = "legacy_unverified"
                    row["analysis_warning"] = (
                        "历史分析数据无法解析，未作为有效分析展示。"
                    )
            return self.send_json({"paper_id": paper_id, "analyses": rows})
        agent_overview_match = _services().re.fullmatch(
            r"/api/papers/([^/]+)/agent-overview", path
        )
        if agent_overview_match:
            paper_id = _services().unquote(agent_overview_match.group(1))
            item = _services().paper_detail(paper_id, include_ai_artifacts=False)
            if not item:
                return self.send_json({"error": "论文不存在"}, 404)
            with _services().DB_LOCK, _services().db_session() as db:
                claims = []
                for row in db.execute(
                    "SELECT id,paper_id,claim_text,claim_type,status,confidence,evidence_scope FROM research_claims WHERE paper_id=? ORDER BY updated_at DESC LIMIT 80",
                    (paper_id,),
                ):
                    claim = dict(row)
                    evidence = db.execute(
                        "SELECT id,page_number,section,quote,evidence_type,verification_status,source_sha256 FROM evidence_spans WHERE claim_id=? ORDER BY page_number",
                        (claim["id"],),
                    ).fetchall()
                    claim["evidence"] = [dict(value) for value in evidence]
                    claims.append(claim)
            paper = {**item, "id": paper_id, "local_paper_id": paper_id}
            overview = _services().call_agent_tool(
                "build_paper_analysis_overview",
                {
                    "papers": [paper],
                    "claims": claims,
                    "verification": {},
                    "critique": {},
                },
            )
            return self.send_json(overview)
        if path.startswith("/api/papers/") and len(path.split("/")) == 4:
            paper_id = path.split("/")[3]
            view = (
                _services()
                .clean_text(
                    (_services().parse_qs(parsed.query).get("view") or ["full"])[0], 20
                )
                .lower()
            )
            item = _services().paper_detail(
                paper_id, include_ai_artifacts=view != "summary"
            )
            return self.send_json(item or {"error": "论文不存在"}, 200 if item else 404)
        if path.startswith("/api/papers/") and path.endswith("/reading-card"):
            paper_id = path.split("/")[3]
            with _services().DB_LOCK, _services().db_session() as db:
                if not db.execute(
                    "SELECT 1 FROM papers WHERE id=?", (paper_id,)
                ).fetchone():
                    return self.send_json({"error": "论文不存在"}, 404)
                row = db.execute(
                    "SELECT * FROM reading_cards WHERE paper_id=?", (paper_id,)
                ).fetchone()
            return self.send_json(_services().reading_card_dict(row, paper_id))
        if path.startswith("/api/papers/") and path.endswith("/history"):
            paper_id = path.split("/")[3]
            with _services().DB_LOCK, _services().db_session() as db:
                if not db.execute(
                    "SELECT 1 FROM papers WHERE id=?", (paper_id,)
                ).fetchone():
                    return self.send_json({"error": "论文不存在"}, 404)
            return self.send_json(
                {"paper_id": paper_id, "revisions": _services().paper_history(paper_id)}
            )
        if path.startswith("/api/papers/") and path.endswith("/connections"):
            try:
                return self.send_json(_services().paper_connections(path.split("/")[3]))
            except ValueError as exc:
                return self.send_json({"error": str(exc)}, 404)
        if path.startswith("/api/papers/") and path.endswith("/extraction"):
            paper_id = path.split("/")[3]
            return self.send_json(_services().extraction_summary(paper_id))
        if path.startswith("/api/papers/") and path.endswith("/ai-jobs"):
            paper_id = path.split("/")[3]
            try:
                limit = min(
                    max(
                        int(
                            (_services().parse_qs(parsed.query).get("limit") or ["20"])[
                                0
                            ]
                        ),
                        1,
                    ),
                    100,
                )
            except (TypeError, ValueError):
                limit = 20
            with _services().DB_LOCK, _services().db_session() as db:
                if not db.execute(
                    "SELECT 1 FROM papers WHERE id=?", (paper_id,)
                ).fetchone():
                    return self.send_json({"error": "论文不存在"}, 404)
                rows = db.execute(
                    "SELECT * FROM ai_jobs WHERE paper_id=? ORDER BY created_at DESC LIMIT ?",
                    (paper_id, limit),
                ).fetchall()
            return self.send_json(
                {
                    "paper_id": paper_id,
                    "jobs": [_services().ai_job_snapshot(row) for row in rows],
                }
            )
        if path.startswith("/api/ai-jobs/"):
            job_id = path.split("/")[3]
            job = _services().get_ai_job(job_id, include_result=True)
            return self.send_json(
                job or {"error": "AI 任务不存在"}, 200 if job else 404
            )
        if path == "/api/ai-jobs":
            try:
                limit = min(
                    max(
                        int(
                            (_services().parse_qs(parsed.query).get("limit") or ["30"])[
                                0
                            ]
                        ),
                        1,
                    ),
                    100,
                )
            except (TypeError, ValueError):
                limit = 30
            with _services().DB_LOCK, _services().db_session() as db:
                rows = db.execute(
                    "SELECT j.*,p.title AS paper_title FROM ai_jobs j JOIN papers p ON p.id=j.paper_id ORDER BY j.updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            jobs = []
            for row in rows:
                item = _services().ai_job_snapshot(row)
                item["paper_title"] = row["paper_title"]
                jobs.append(item)
            return self.send_json({"jobs": jobs})
        if path.startswith("/api/papers/") and path.endswith("/visual-pages"):
            paper_id = path.split("/")[3]
            with _services().DB_LOCK, _services().db_session() as db:
                if not db.execute(
                    "SELECT 1 FROM papers WHERE id=?", (paper_id,)
                ).fetchone():
                    return self.send_json({"error": "论文不存在"}, 404)
                rows = [
                    dict(row)
                    for row in db.execute(
                        "SELECT id,stored_name,sha256 FROM attachments WHERE paper_id=? ORDER BY added_at",
                        (paper_id,),
                    )
                ]
            pages = []
            for attachment in rows:
                prefix = _services().clean_text(
                    attachment.get("sha256", ""), 64
                ) or _services().clean_text(attachment.get("id", ""), 100)
                for image in (
                    sorted(_services().active_visuals_dir().glob(prefix + "-*.jpg"))
                    if _services().active_visuals_dir().exists()
                    else []
                ):
                    match = _services().re.search(r"-(\d+)\.jpg$", image.name)
                    if not match:
                        continue
                    pages.append(
                        {
                            "attachment_id": attachment["id"],
                            "page": int(match.group(1)),
                            "sha256": _services()
                            .hashlib.sha256(image.read_bytes())
                            .hexdigest(),
                            "bytes": image.stat().st_size,
                            "url": "/visual-pages/" + image.name,
                        }
                    )
            return self.send_json(
                {"paper_id": paper_id, "pages": pages, "count": len(pages)}
            )
        semantic_status_match = _services().re.fullmatch(
            r"/api/papers/([^/]+)/semantic-index", path
        )
        if semantic_status_match:
            try:
                return self.send_json(
                    _services().get_semantic_index_status(
                        _services().unquote(semantic_status_match.group(1))
                    )
                )
            except LookupError as exc:
                return self.send_json({"error": str(exc)}, 404)
        if path.startswith("/api/papers/") and path.endswith("/pages"):
            paper_id = path.split("/")[3]
            params = _services().parse_qs(parsed.query)
            query = (params.get("q") or [""])[0]
            if not query:
                return self.send_json({"error": "请提供 q 页码检索词"}, 400)
            try:
                limit = int((params.get("limit") or ["20"])[0])
            except (TypeError, ValueError):
                limit = 20
            semantic = (params.get("semantic") or [""])[0].casefold() in {
                "1",
                "true",
                "yes",
                "on",
            }
            try:
                result = _services().search_document_pages_detailed(
                    paper_id, query, limit, semantic
                )
            except LookupError as exc:
                return self.send_json({"error": str(exc)}, 404)
            return self.send_json({"paper_id": paper_id, "query": query, **result})
        if path.startswith("/api/papers/") and path.endswith("/reading-segments"):
            paper_id = path.split("/")[3]
            params = _services().parse_qs(parsed.query)
            raw_page = (params.get("page") or [""])[0]
            try:
                page = int(raw_page) if raw_page else None
            except (TypeError, ValueError):
                page = None
            try:
                limit = int((params.get("limit") or ["2000"])[0])
            except (TypeError, ValueError):
                limit = 2000
            try:
                return self.send_json(
                    _services().reading_segments(paper_id, page, limit)
                )
            except LookupError as exc:
                return self.send_json({"error": str(exc)}, 404)
        # Keep the pre-LitLoom route alive for already-open browser tabs and
        # cached frontends.  Both endpoints expose the same durable run list;
        # removing the alias made an old tab report a misleading connection
        # error while the background service itself was healthy.
        if path in {"/api/runs", "/api/search/runs"}:
            try:
                limit = int(
                    (_services().parse_qs(parsed.query).get("limit") or ["100"])[0]
                )
            except (TypeError, ValueError):
                limit = 100
            with _services().DB_LOCK, _services().db_session() as db:
                return self.send_json(_services().run_list_snapshots(db, limit))
        if path in {"/api/discovery/history", "/api/discover/history"}:
            try:
                params = _services().parse_qs(parsed.query)
                limit = int((params.get("limit") or ["20"])[0])
                offset = int((params.get("offset") or ["0"])[0])
            except (TypeError, ValueError):
                limit, offset = 20, 0
            history = _services().discovery_history(
                limit, offset, (_services().parse_qs(parsed.query).get("q") or [""])[0]
            )
            return self.send_json(
                {"history": history, "limit": limit, "offset": offset}
            )
        run_screen_match = _services().re.fullmatch(
            r"/api/runs/([^/]+)/(?:ai-screen|triage)", path
        )
        if run_screen_match:
            snapshot = _services().run_results_snapshot(run_screen_match.group(1))
            if not snapshot:
                return self.send_json({"error": "检索运行记录不存在"}, 404)
            triage = snapshot.get("triage") or {"status": "not_requested"}
            payload = (
                dict(triage)
                if isinstance(triage, dict)
                else {"status": "not_requested"}
            )
            for key in (
                "search_status",
                "screening_status",
                "screening_progress",
                "workflow_status",
            ):
                if key in snapshot:
                    payload[key] = snapshot[key]
            return self.send_json(payload)
        if path == "/api/sync-jobs":
            try:
                limit = min(
                    max(
                        int(
                            (
                                _services().parse_qs(parsed.query).get("limit")
                                or ["100"]
                            )[0]
                        ),
                        1,
                    ),
                    100,
                )
            except (TypeError, ValueError):
                limit = 100
            with _services().DB_LOCK, _services().db_session() as db:
                rows = db.execute(
                    """SELECT s.*,
                              r.query_name AS run_query_name,
                              r.query_name AS saved_query_name,
                              r.status AS run_status,
                              r.started_at AS run_started_at_raw,
                              r.completed_at AS run_completed_at_raw,
                              r.new_count AS run_new_count,
                              r.duplicate_count AS run_duplicate_count,
                              r.error AS run_error_raw,
                              r.sources_json AS run_sources_json,
                              r.source_details_json AS run_source_details_json,
                              rs.query AS run_query,
                              rs.summary_json AS run_summary_json,
                              (SELECT COUNT(*) FROM discovery_candidates dc WHERE dc.run_id=s.run_id) AS run_candidate_count,
                              (SELECT COUNT(*) FROM discovery_candidates dc WHERE dc.run_id=s.run_id AND dc.imported_paper_id<>'') AS run_imported_count,
                              q.name AS saved_query_name_joined,
                              q.query AS saved_query
                       FROM sync_jobs s
                       LEFT JOIN runs r ON r.id=s.run_id
                       LEFT JOIN run_snapshots rs ON rs.run_id=s.run_id
                       LEFT JOIN saved_queries q ON q.id=s.query_id
                       ORDER BY s.updated_at DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            snapshots = []
            for row in rows:
                item = dict(row)
                # A saved query name is more useful than the run's fallback
                # label when a topic was renamed after an earlier run.
                if item.get("saved_query_name_joined"):
                    item["saved_query_name"] = item["saved_query_name_joined"]
                item.pop("saved_query_name_joined", None)
                snapshots.append(_services().sync_job_snapshot(item))
            return self.send_json(snapshots)
        if path.startswith("/api/runs/") and path.endswith("/results"):
            run_id = path.split("/")[3]
            snapshot = _services().run_results_snapshot(run_id)
            if not snapshot:
                return self.send_json({"error": "检索运行记录不存在"}, 404)
            return self.send_json(
                snapshot, 202 if snapshot["status"] in {"queued", "running"} else 200
            )
        if path.startswith("/api/runs/"):
            detail = _services().run_detail(path.split("/")[3])
            return self.send_json(
                detail or {"error": "运行记录不存在"}, 200 if detail else 404
            )
        if path == "/api/export/json":
            return self.export_json()
        if path == "/api/export/bibtex":
            return self.export_bibtex()
        if path == "/api/analytics":
            return self.send_json(_services().analytics_summary())
        if path == "/api/metrics":
            return self.send_json(_services().runtime_metrics_summary())
        if path == "/api/diagnostics":
            return self.send_json(_services().diagnostics_summary())
        if path == "/api/briefing":
            params = _services().parse_qs(parsed.query)
            try:
                days = int((params.get("days") or ["7"])[0])
            except (TypeError, ValueError):
                days = 7
            try:
                limit = int((params.get("limit") or ["8"])[0])
            except (TypeError, ValueError):
                limit = 8
            return self.send_json(_services().research_briefing(days, limit))
        if path == "/api/evaluation":
            return self.send_json(_services().evaluation_summary())
        if path == "/api/settings/model":
            return self.send_json(_services().model_settings())
        if path == "/api/settings/model/providers":
            return self.send_json({"providers": _services().model_provider_presets()})
        if path == "/api/settings/backup-policy":
            return self.send_json(_services().get_backup_policy())
        if path == "/api/agent/status":
            settings = _services().model_settings()
            return self.send_json({"configured": settings["api_key_set"], **settings})
        return self.send_error(404)

    def serve_file(self, path: Path, content_type=None):
        if not path.is_file():
            return self.send_error(404)
        data = path.read_bytes()
        self.send_response(200)
        self.send_header(
            "Content-Type",
            content_type
            or _services().mimetypes.guess_type(path.name)[0]
            or "application/octet-stream",
        )
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Request-ID", getattr(self, "request_id", ""))
        self.end_headers()
        self.wfile.write(data)

    def get_state(self):
        with _services().DB_LOCK, _services().db_session() as db:
            counts = {
                r["status"]: r["n"]
                for r in db.execute(
                    "SELECT status,COUNT(*) AS n FROM papers GROUP BY status"
                )
            }
            total = db.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
            recent = [
                _services().paper_dict(r)
                for r in db.execute(
                    "SELECT * FROM papers ORDER BY updated_at DESC LIMIT 8"
                )
            ]
            queries = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM saved_queries ORDER BY active DESC,updated_at DESC LIMIT 10"
                )
            ]
            runs = _services().run_list_snapshots(db, 8)
            attachments = db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
        for q in queries:
            q["sources"] = _services().json.loads(q.pop("sources_json"))
            try:
                q["search_options"] = _services().json.loads(
                    q.pop("search_options_json", "{}") or "{}"
                )
            except _services().json.JSONDecodeError:
                q["search_options"] = {}
            q["active"] = bool(q["active"])
        return self.send_json(
            {
                "counts": counts,
                "total": total,
                "recent": recent,
                "queries": queries,
                "runs": runs,
                "attachments": attachments,
                "updated_at": _services().now_iso(),
                "data_dir": str(_services().DATA_DIR),
            }
        )

    def get_papers(self, params):
        q = _services().clean_text((params.get("q") or [""])[0], 500)
        status = _services().clean_text((params.get("status") or [""])[0], 40)
        topic = _services().clean_text((params.get("topic") or [""])[0], 300)
        try:
            limit = min(max(int((params.get("limit") or ["200"])[0]), 1), 500)
        except (TypeError, ValueError):
            limit = 200
        try:
            offset = max(0, int((params.get("offset") or ["0"])[0]))
        except (TypeError, ValueError):
            offset = 0
        filters = {
            "favorite": (params.get("favorite") or [""])[0].lower()
            in {"1", "true", "yes"},
            "has_reading_card": (params.get("has_reading_card") or [""])[0].lower()
            in {"1", "true", "yes"},
            "year_from": (params.get("year_from") or [""])[0],
            "year_to": (params.get("year_to") or [""])[0],
            "sort": (params.get("sort") or [""])[0],
        }
        if q:
            result = _services().search_library(
                q, limit, offset, status, topic, filters
            )
            return self.send_json(
                result["results"],
                headers={
                    "X-Total-Count": result["total"],
                    "X-Has-More": str(result["has_more"]).lower(),
                    "X-Offset": offset,
                    "X-Limit": limit,
                },
            )
        clauses, args = [], []
        if status:
            clauses.append("status=?")
            args.append(status)
        if topic:
            clauses.append("topic=?")
            args.append(topic)
        if filters["favorite"]:
            clauses.append("is_favorite=1")
        if filters["has_reading_card"]:
            clauses.append(
                "EXISTS (SELECT 1 FROM reading_cards rc WHERE rc.paper_id=papers.id)"
            )
        for key, op in (("year_from", ">="), ("year_to", "<=")):
            try:
                year = int(filters[key]) if filters[key] else None
            except (TypeError, ValueError):
                year = None
            if year:
                clauses.append(f"year{op}?")
                args.append(year)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        order_by = {
            "oldest": "updated_at ASC,id ASC",
            "year_desc": "year DESC,updated_at DESC",
            "year_asc": "year ASC,updated_at DESC",
            "title": "title COLLATE NOCASE ASC,id ASC",
        }.get(filters["sort"], "is_favorite DESC,updated_at DESC,id ASC")
        with _services().DB_LOCK, _services().db_session() as db:
            total = db.execute(
                f"SELECT COUNT(*) FROM papers{where}", tuple(args)
            ).fetchone()[0]
            rows = [
                _services().paper_dict(r)
                for r in db.execute(
                    f"SELECT * FROM papers{where} ORDER BY {order_by} LIMIT ? OFFSET ?",
                    (*args, limit, offset),
                )
            ]
            card_rows = {}
            if rows:
                paper_ids = [paper["id"] for paper in rows]
                placeholders = ",".join("?" for _ in paper_ids)
                card_rows = {
                    row["paper_id"]: row
                    for row in db.execute(
                        f"SELECT * FROM reading_cards WHERE paper_id IN ({placeholders})",
                        paper_ids,
                    )
                }
            for paper in rows:
                paper["reading_card"] = _services().reading_card_dict(
                    card_rows.get(paper["id"]), paper["id"]
                )
                paper["sources"] = [
                    dict(r)
                    for r in db.execute(
                        "SELECT source,external_id,fetched_at FROM source_records WHERE paper_id=? ORDER BY fetched_at DESC",
                        (paper["id"],),
                    )
                ]
                paper["attachments"] = [
                    dict(r)
                    for r in db.execute(
                        "SELECT id,original_name,stored_name,sha256,byte_size,added_at FROM attachments WHERE paper_id=?",
                        (paper["id"],),
                    )
                ]
                # Library rows are rendered as a list; the UI loads the full
                # analysis history only after a paper is opened.  Do not copy
                # multi-megabyte model answers into every list response.
                paper["ai_artifacts"] = [
                    dict(r)
                    for r in db.execute(
                        "SELECT id,artifact_type,provider,model,prompt_version,input_sha256,created_at FROM ai_artifacts WHERE paper_id=? ORDER BY created_at DESC LIMIT 5",
                        (paper["id"],),
                    )
                ]
        return self.send_json(
            rows,
            headers={
                "X-Total-Count": total,
                "X-Has-More": str(offset + len(rows) < total).lower(),
                "X-Offset": offset,
                "X-Limit": limit,
            },
        )

    def do_POST(self):
        if not self._begin_auth_request():
            return
        token = _services().AUTH_ACTOR.set(self.auth_user)
        try:
            try:
                return self._handle_post()
            except Exception as exc:
                return self._handle_unexpected_request_error(exc)
        finally:
            _services().AUTH_ACTOR.reset(token)

    def _handle_post(self):
        path = _services().urlparse(self.path).path
        try:
            if path in {"/api/auth/login", "/api/auth/bootstrap"}:
                data = self.read_json()
                try:
                    if path == "/api/auth/bootstrap":
                        session_token, csrf_token, user = (
                            _services().bootstrap_team_owner(data)
                        )
                    else:
                        session_token, csrf_token, user = _services().authenticate_user(
                            data.get("username", ""),
                            data.get("password", ""),
                            self.client_address[0],
                            data.get("workspace_id", ""),
                        )
                except TimeoutError as exc:
                    return self.send_json({"error": str(exc)}, 429)
                except PermissionError as exc:
                    return self.send_json({"error": str(exc)}, 401)
                except LookupError as exc:
                    return self.send_json({"error": str(exc)}, 409)
                except ValueError as exc:
                    return self.send_json({"error": str(exc)}, 400)
                return self.send_json(
                    {"user": user},
                    headers={
                        "Set-Cookie": self._auth_cookie_values(
                            session_token, csrf_token
                        )
                    },
                )
            if path == "/api/auth/logout":
                with _services().DB_LOCK, _services().control_db_session() as db:
                    db.execute(
                        "DELETE FROM auth_sessions WHERE token_hash=?",
                        (_services().token_digest(self.auth_session_token),),
                    )
                    _services().record_audit(
                        db,
                        "auth.logout",
                        "user",
                        self.auth_user["user_id"],
                        {"workspace_id": self.auth_user["workspace_id"]},
                    )
                return self.send_json(
                    {"logged_out": True},
                    headers={
                        "Set-Cookie": self._auth_cookie_values("", "", clear=True)
                    },
                )
            if path == "/api/auth/switch-workspace":
                data = self.read_json()
                session_token, csrf_token, user = _services().switch_workspace(
                    self.auth_user["user_id"], data.get("workspace_id", "")
                )
                return self.send_json(
                    {"user": user},
                    headers={
                        "Set-Cookie": self._auth_cookie_values(
                            session_token, csrf_token
                        )
                    },
                )
            if path == "/api/auth/password":
                data = self.read_json()
                try:
                    session_token, csrf_token, user = _services().change_user_password(
                        self.auth_user["user_id"],
                        data.get("current_password", ""),
                        data.get("new_password", ""),
                    )
                except (PermissionError, ValueError) as exc:
                    return self.send_json({"error": str(exc)}, 400)
                return self.send_json(
                    {"user": user},
                    headers={
                        "Set-Cookie": self._auth_cookie_values(
                            session_token, csrf_token
                        )
                    },
                )
            if path == "/api/workspaces":
                try:
                    return self.send_json(
                        _services().create_workspace(self.read_json(), self.auth_user),
                        201,
                    )
                except (PermissionError, ValueError) as exc:
                    return self.send_json(
                        {"error": str(exc)},
                        403 if isinstance(exc, PermissionError) else 400,
                    )
            if path == "/api/workspace/members":
                try:
                    return self.send_json(
                        {"member": _services().create_workspace_user(self.read_json())},
                        201,
                    )
                except _services().sqlite3.IntegrityError:
                    return self.send_json({"error": "用户名已存在"}, 409)
            if path == "/api/recommendations/import":
                data = self.read_json()
                try:
                    result = _services().save_openalex_recommendation(
                        data.get("external_id", "")
                    )
                except LookupError as exc:
                    return self.send_json({"error": str(exc)}, 404)
                except ValueError as exc:
                    return self.send_json({"error": str(exc)}, 400)
                return self.send_json(result, 201 if result.get("created") else 200)
            if path == "/api/agent-tools/browse-web":
                data = self.read_json()
                try:
                    return self.send_json(
                        _services().call_agent_tool("browse_web", data)
                    )
                except (
                    PermissionError,
                    ValueError,
                    _services().PublicSourceCooldownError,
                ) as exc:
                    return self.send_json({"error": str(exc)}, 400)
            if path == "/api/agent-tools/fetch-web-page":
                data = self.read_json()
                try:
                    return self.send_json(
                        _services().call_agent_tool("fetch_web_page", data)
                    )
                except (
                    PermissionError,
                    ValueError,
                    _services().PublicSourceCooldownError,
                ) as exc:
                    return self.send_json({"error": str(exc)}, 400)
            if path == "/api/settings/model/test":
                try:
                    probe = self.read_json()
                    return self.send_json(
                        _services().test_model_connection(
                            deep=bool(probe.get("deep", True))
                        )
                    )
                except (
                    _services().HTTPError,
                    _services().URLError,
                    TimeoutError,
                    OSError,
                    ValueError,
                ) as exc:
                    return self.send_json(
                        {"error": f"模型连接测试失败：{str(exc)[:500]}"}, 502
                    )
            if path == "/api/pdf-ingest":
                files, options = self.read_pdf_ingest_request()
                try:
                    result = _services().create_pdf_ingest_job(files, options)
                except Exception:
                    for item in files:
                        if item.get("staged_upload_path"):
                            _services().Path(item["staged_upload_path"]).unlink(
                                missing_ok=True
                            )
                    raise
                return self.send_json(result, 202)
            semantic_index_match = _services().re.fullmatch(
                r"/api/papers/([^/]+)/semantic-index", path
            )
            if semantic_index_match:
                data = self.read_json()
                try:
                    result = _services().submit_semantic_index_job(
                        _services().unquote(semantic_index_match.group(1)),
                        bool(data.get("force")),
                        data.get("consent") is True,
                    )
                except LookupError as exc:
                    return self.send_json({"error": str(exc)}, 404)
                return self.send_json(
                    result,
                    (
                        200
                        if result.get("deduplicated") or result.get("up_to_date")
                        else 202
                    ),
                )
            semantic_cancel_match = _services().re.fullmatch(
                r"/api/semantic-index-jobs/([^/]+)/cancel", path
            )
            if semantic_cancel_match:
                try:
                    return self.send_json(
                        _services().cancel_semantic_index_job(
                            _services().unquote(semantic_cancel_match.group(1))
                        )
                    )
                except LookupError as exc:
                    return self.send_json({"error": str(exc)}, 404)
            if path == "/api/papers/metadata-draft":
                data = self.read_json()
                if data.get("background"):
                    result = _services().submit_metadata_draft_job(data)
                    return self.send_json(
                        result, 200 if result.get("deduplicated") else 202
                    )
                return self.send_json(_services().complete_paper_metadata_draft(data))
            metadata_retry_match = _services().re.fullmatch(
                r"/api/metadata-draft-jobs/([^/]+)/retry", path
            )
            if metadata_retry_match:
                try:
                    result = _services().retry_metadata_draft_job(
                        _services().unquote(metadata_retry_match.group(1))
                    )
                except LookupError as exc:
                    return self.send_json({"error": str(exc)}, 404)
                return self.send_json(
                    result, 200 if result.get("deduplicated") else 202
                )
            if path == "/api/pdf-ingest/scan":
                data = self.read_json()
                folder = _services().register_paper_folder(data)
                result = _services().scan_paper_folder(folder["id"])
                return self.send_json(
                    {"folder": folder, "job": result}, 202 if result.get("id") else 200
                )
            ingest_retry = _services().re.fullmatch(
                r"/api/pdf-ingest/([^/]+)/retry", path
            )
            if ingest_retry:
                return self.send_json(
                    _services().retry_pdf_ingest_job(
                        _services().unquote(ingest_retry.group(1))
                    ),
                    202,
                )
            if path == "/api/pdf-folders":
                return self.send_json(
                    _services().register_paper_folder(self.read_json()), 201
                )
            if path == "/api/papers":
                data = self.read_json()
                pid, created = _services().upsert_paper(data, source="manual")
                return self.send_json(
                    {"id": pid, "created": created}, 201 if created else 200
                )
            if path == "/api/papers/batch":
                data = self.read_json()
                return self.send_json(
                    _services().batch_update_papers(
                        data.get("ids", []), data.get("updates", {})
                    )
                )
            if path == "/api/maintenance/rebuild-search-index":
                return self.send_json(_services().rebuild_search_index())
            history_match = _services().re.fullmatch(
                r"/api/papers/([^/]+)/history/(\d+)/restore", path
            )
            if history_match:
                paper_id, revision_id = history_match.group(1), int(
                    history_match.group(2)
                )
                with _services().DB_LOCK, _services().db_session() as db:
                    revision = db.execute(
                        "SELECT * FROM paper_revisions WHERE id=? AND paper_id=?",
                        (revision_id, paper_id),
                    ).fetchone()
                    if not revision:
                        return self.send_json({"error": "历史版本不存在"}, 404)
                    content = _services().json.loads(revision["content_json"])
                    if revision["revision_type"] == "notes":
                        notes = _services().clean_text(content.get("notes", ""), 100000)
                        db.execute(
                            "UPDATE papers SET notes=?,updated_at=? WHERE id=?",
                            (notes, _services().now_iso(), paper_id),
                        )
                        _services().record_paper_revision(
                            db, paper_id, "notes", {"notes": notes}, "restore"
                        )
                        _services().record_audit(
                            db,
                            "paper.notes_restored",
                            "paper",
                            paper_id,
                            {"revision_id": revision_id},
                        )
                    elif revision["revision_type"] == "reading_card":
                        card = _services().save_reading_card(
                            db,
                            paper_id,
                            {
                                field: content.get(field, "")
                                for field in _services().READING_CARD_FIELDS
                            },
                            "restore",
                        )
                        _services().record_audit(
                            db,
                            "reading_card.restored",
                            "paper",
                            paper_id,
                            {"revision_id": revision_id},
                        )
                    else:
                        raise ValueError("无法恢复此类历史版本")
                return self.send_json(
                    {
                        "restored": True,
                        "revision_id": revision_id,
                        "revision_type": revision["revision_type"],
                        "reading_card": (
                            card
                            if revision["revision_type"] == "reading_card"
                            else None
                        ),
                    }
                )
            if path == "/api/discover":
                data = self.read_json()
                request_id = _services().clean_text(data.get("request_id", ""), 80)
                if request_id:
                    try:
                        request_id = str(_services().uuid.UUID(request_id))
                    except ValueError:
                        raise ValueError("检索请求 ID 无效，请重新发起检索")
                    existing = _services().run_results_snapshot(request_id)
                    if existing:
                        return self.send_json(
                            existing,
                            202 if existing["status"] in {"queued", "running"} else 200,
                        )
                plan_override = (
                    data.get("plan") if isinstance(data.get("plan"), dict) else None
                )
                options = dict(data.get("options") or {})
                options.update({"review_only": True, "auto_add": False})
                options["ai_screen"] = options.get(
                    "ai_screen", data.get("ai_screen", True)
                )
                result = _services().launch_query_job(
                    data.get("query", ""),
                    data.get("sources", ["arxiv"]),
                    data.get("limit", 20),
                    name=data.get("name", ""),
                    options=options,
                    request_id=request_id,
                    plan_override=plan_override,
                )
                return self.send_json(result, 202)
            run_screen_match = _services().re.fullmatch(
                r"/api/runs/([^/]+)/(?:ai-screen|triage)", path
            )
            if run_screen_match:
                data = self.read_json()
                return self.send_json(
                    _services().start_discovery_ai_screen(
                        run_screen_match.group(1),
                        retry=data.get("force") is True or data.get("retry") is True,
                    ),
                    202,
                )
            rerun_match = _services().re.fullmatch(r"/api/runs/([^/]+)/rerun", path)
            if rerun_match:
                data = self.read_json()
                return self.send_json(
                    _services().rerun_discovery(
                        rerun_match.group(1), data.get("ai_screen")
                    ),
                    202,
                )
            feedback_match = _services().re.fullmatch(
                r"/api/runs/([^/]+)/candidates/([^/]+)/feedback", path
            )
            if feedback_match:
                data = self.read_json()
                return self.send_json(
                    _services().update_discovery_candidate_feedback(
                        feedback_match.group(1),
                        _services().unquote(feedback_match.group(2)),
                        data.get("decision", ""),
                        data.get("note", ""),
                    )
                )
            if path == "/api/research-goals":
                return self.send_json(
                    _services().create_research_goal(self.read_json()), 201
                )
            goal_run_match = _services().re.fullmatch(
                r"/api/research-goals/([^/]+)/runs", path
            )
            if goal_run_match:
                data = self.read_json()
                return self.send_json(
                    _services().create_agent_run(
                        goal_run_match.group(1), data.get("trigger", "manual")
                    ),
                    201,
                )
            agent_approve_match = _services().re.fullmatch(
                r"/api/agent-runs/([^/]+)/(approve|resume)", path
            )
            if agent_approve_match:
                data = self.read_json()
                return self.send_json(
                    _services().approve_agent_run(
                        agent_approve_match.group(1),
                        data.get("consent_external") is True,
                    ),
                    202,
                )
            agent_pause_match = _services().re.fullmatch(
                r"/api/agent-runs/([^/]+)/pause", path
            )
            if agent_pause_match:
                return self.send_json(
                    _services().pause_agent_run(agent_pause_match.group(1))
                )
            agent_cancel_match = _services().re.fullmatch(
                r"/api/agent-runs/([^/]+)/cancel", path
            )
            if agent_cancel_match:
                return self.send_json(
                    _services().cancel_agent_run(agent_cancel_match.group(1))
                )
            agent_import_match = _services().re.fullmatch(
                r"/api/agent-runs/([^/]+)/import-candidates", path
            )
            if agent_import_match:
                data = self.read_json()
                return self.send_json(
                    _services().import_agent_candidates(
                        agent_import_match.group(1),
                        data.get("candidate_ids") or [],
                        data.get("confirm") is True,
                    )
                )
            agent_download_match = _services().re.fullmatch(
                r"/api/agent-runs/([^/]+)/download-fulltext", path
            )
            if agent_download_match:
                data = self.read_json()
                return self.send_json(
                    _services().download_agent_fulltext(
                        agent_download_match.group(1),
                        data.get("candidate_ids") or [],
                        data.get("confirm") is True,
                    )
                )
            agent_apply_card_match = _services().re.fullmatch(
                r"/api/agent-runs/([^/]+)/apply-reading-card", path
            )
            if agent_apply_card_match:
                data = self.read_json()
                return self.send_json(
                    _services().apply_agent_reading_card(
                        agent_apply_card_match.group(1),
                        data.get("paper_id", ""),
                        data.get("confirm") is True,
                    )
                )
            agent_export_match = _services().re.fullmatch(
                r"/api/agent-runs/([^/]+)/export-report", path
            )
            if agent_export_match:
                data = self.read_json()
                result = _services().export_agent_report(
                    agent_export_match.group(1),
                    data.get("content", ""),
                    data.get("confirm") is True,
                )
                return self.send_download(
                    result["filename"],
                    "text/markdown; charset=utf-8",
                    result["content"].encode("utf-8"),
                )
            candidate_import_match = _services().re.fullmatch(
                r"/api/runs/([^/]+)/import-candidates", path
            )
            if candidate_import_match:
                data = self.read_json()
                return self.send_json(
                    _services().import_discovery_candidates(
                        candidate_import_match.group(1), data.get("candidate_ids") or []
                    )
                )
            if path == "/api/discover/plan":
                data = self.read_json()
                query = _services().clean_text(data.get("query", ""), 1000)
                sources = [
                    s
                    for s in data.get("sources", ["arxiv"])
                    if s in _services().SOURCE_NAMES and s in _services().SOURCE_SEARCH
                ]
                if not query:
                    raise ValueError("请输入研究问题或关键词")
                if not sources:
                    raise ValueError("请至少选择一个学术来源")
                return self.send_json(
                    _services().search_query_plan(
                        query, sources, data.get("options", {})
                    )
                )
            if path == "/api/discover/agent-plan":
                data = self.read_json()
                query = _services().clean_text(data.get("query", ""), 1000)
                sources = [
                    s
                    for s in data.get("sources", ["arxiv"])
                    if s in _services().SOURCE_NAMES and s in _services().SOURCE_SEARCH
                ]
                if not query:
                    raise ValueError("请输入研究问题或关键词")
                if not sources:
                    raise ValueError("请至少选择一个学术来源")
                try:
                    return self.send_json(
                        _services().agent_discovery_plan(
                            query, sources, data.get("options", {})
                        )
                    )
                except (
                    _services().HTTPError,
                    _services().URLError,
                    TimeoutError,
                    OSError,
                    _services().json.JSONDecodeError,
                    ValueError,
                ) as exc:
                    api_key = _services().model_api_key()
                    return self.send_json(
                        {
                            "error": f"模型检索规划失败：{_services().model_error_detail(exc, api_key)}"
                        },
                        502,
                    )
            if path.startswith("/api/papers/") and path.endswith("/extract"):
                return self.send_json(
                    _services().extract_paper_documents(path.split("/")[3])
                )
            if path.startswith("/api/papers/") and path.endswith("/expand"):
                data = self.read_json()
                return self.send_json(
                    _services().expand_openalex_relations(
                        path.split("/")[3], data.get("limit", 24)
                    )
                )
            if path.startswith("/api/papers/") and path.endswith("/feedback"):
                paper_id = path.split("/")[3]
                data = self.read_json()
                decision = data.get("decision")
                if decision not in _services().RECOMMENDATION_DECISIONS:
                    raise ValueError("反馈必须是 relevant、irrelevant 或 later")
                scope = (
                    _services().clean_text(data.get("scope", "global"), 120) or "global"
                )
                note = _services().clean_text(data.get("note", ""), 500)
                with _services().DB_LOCK, _services().db_session() as db:
                    if not db.execute(
                        "SELECT 1 FROM papers WHERE id=?", (paper_id,)
                    ).fetchone():
                        return self.send_json({"error": "论文不存在"}, 404)
                    db.execute(
                        "INSERT INTO learning_feedback(paper_id,scope,decision,note,created_at) VALUES(?,?,?,?,?) ON CONFLICT(paper_id,scope) DO UPDATE SET decision=excluded.decision,note=excluded.note,created_at=excluded.created_at",
                        (paper_id, scope, decision, note, _services().now_iso()),
                    )
                    _services().record_audit(
                        db,
                        "feedback.updated",
                        "paper",
                        paper_id,
                        {"scope": scope, "decision": decision},
                    )
                _services().invalidate_derived_cache()
                return self.send_json(
                    {
                        "updated": True,
                        "paper_id": paper_id,
                        "scope": scope,
                        "decision": decision,
                    }
                )
            if path == "/api/queries":
                return self.save_query(self.read_json())
            if path.startswith("/api/queries/") and path.endswith("/run"):
                qid = path.split("/")[3]
                with _services().DB_LOCK, _services().db_session() as db:
                    row = db.execute(
                        "SELECT * FROM saved_queries WHERE id=?", (qid,)
                    ).fetchone()
                if not row:
                    return self.send_json({"error": "主题不存在"}, 404)
                try:
                    search_options = _services().json.loads(
                        row["search_options_json"] or "{}"
                    )
                except (_services().json.JSONDecodeError, TypeError):
                    search_options = {}
                # A manual click is a user review action, not scheduler consent
                # to import every result.  Scheduled topic runs keep their
                # established persistence path in the scheduler itself.
                search_options.update({"review_only": True, "auto_add": False})
                result = _services().launch_query_job(
                    row["query"],
                    _services().json.loads(row["sources_json"]),
                    25,
                    qid,
                    False,
                    row["name"],
                    options=search_options,
                )
                return self.send_json(result, 202)
            if path == "/api/import":
                return self.import_data(self.read_json())
            if path.startswith("/api/papers/") and path.endswith("/download-pdf"):
                return self.download_pdf(path.split("/")[3], self.read_json())
            if path.startswith("/api/papers/") and path.endswith("/attachment"):
                return self.add_attachment(path.split("/")[3], self.read_json())
            if path.startswith("/api/papers/") and path.endswith("/agent-brief"):
                paper_id, data = path.split("/")[3], self.read_json()
                if data.get("background"):
                    if self._fulltext_ready(data) is None:
                        return
                    result = _services().submit_ai_job(paper_id, "abstract_brief", data)
                    return self.send_json(
                        result, 200 if result.get("deduplicated") else 202
                    )
                return self.agent_brief(paper_id, data)
            if path.startswith("/api/papers/") and path.endswith("/ai-reading-card"):
                paper_id, data = path.split("/")[3], self.read_json()
                if data.get("background"):
                    if self._fulltext_ready(data, paper_id) is None:
                        return
                    result = _services().submit_ai_job(paper_id, "reading_card", data)
                    return self.send_json(
                        result, 200 if result.get("deduplicated") else 202
                    )
                return self.ai_reading_card(paper_id, data)
            if path.startswith("/api/papers/") and path.endswith("/ai-deep-read"):
                paper_id, data = path.split("/")[3], self.read_json()
                if data.get("background"):
                    if self._fulltext_ready(data, paper_id) is None:
                        return
                    result = _services().submit_ai_job(paper_id, "deep_read", data)
                    return self.send_json(
                        result, 200 if result.get("deduplicated") else 202
                    )
                return self.ai_deep_read(paper_id, data)
            if path.startswith("/api/papers/") and path.endswith("/paragraph-analysis"):
                paper_id, data = path.split("/")[3], self.read_json()
                if data.get("background"):
                    if self._fulltext_ready(data, paper_id) is None:
                        return
                    result = _services().submit_ai_job(
                        paper_id, "paragraph_explain", data
                    )
                    return self.send_json(
                        result, 200 if result.get("deduplicated") else 202
                    )
                if self._fulltext_ready(data, paper_id) is None:
                    return
                try:
                    return self.send_json(
                        _services().analyze_paper_segment(paper_id, data)
                    )
                except LookupError as exc:
                    return self.send_json({"error": str(exc)}, 404)
                except ValueError as exc:
                    return self.send_json({"error": str(exc)}, 422)
            if path.startswith("/api/papers/") and path.endswith(
                "/ai-prereading-guide"
            ):
                paper_id, data = path.split("/")[3], self.read_json()
                if self._fulltext_ready(data, paper_id) is None:
                    return
                result = _services().submit_ai_job(paper_id, "prereading_guide", data)
                return self.send_json(
                    result, 200 if result.get("deduplicated") else 202
                )
            if path.startswith("/api/ai-jobs/") and path.endswith("/cancel"):
                job_id = path.split("/")[3]
                with _services().DB_LOCK, _services().db_session() as db:
                    row = db.execute(
                        "SELECT status FROM ai_jobs WHERE id=?", (job_id,)
                    ).fetchone()
                    if not row:
                        return self.send_json({"error": "AI 任务不存在"}, 404)
                    if row["status"] in {
                        "success",
                        "partial",
                        "failed",
                        "cancelled",
                        "interrupted",
                    }:
                        return self.send_json(_services().get_ai_job(job_id))
                    db.execute(
                        "UPDATE ai_jobs SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END,stage=CASE WHEN status='queued' THEN 'cancelled' ELSE stage END,message=CASE WHEN status='queued' THEN '任务已取消' ELSE '等待当前模型请求结束后取消' END,updated_at=?,completed_at=CASE WHEN status='queued' THEN ? ELSE completed_at END WHERE id=?",
                        (_services().now_iso(), _services().now_iso(), job_id),
                    )
                return self.send_json(_services().get_ai_job(job_id))
            if path.startswith("/api/ai-jobs/") and path.endswith("/retry"):
                job_id = path.split("/")[3]
                return self.send_json(_services().retry_ai_job(job_id), 202)
            if path.startswith("/api/papers/") and path.endswith(
                "/apply-ai-reading-card"
            ):
                return self.apply_ai_reading_card(path.split("/")[3], self.read_json())
            if path.startswith("/api/papers/") and path.endswith("/references/sync"):
                try:
                    return self.send_json(
                        _services().sync_openalex_references(path.split("/")[3])
                    )
                except (
                    _services().HTTPError,
                    _services().URLError,
                    TimeoutError,
                    OSError,
                ) as exc:
                    return self.send_json(
                        {"error": f"OpenAlex 引用同步失败：{str(exc)[:400]}"}, 502
                    )
            if path == "/api/backup":
                return self.create_backup()
            if path == "/api/backup/restore":
                return self.restore_backup(self.read_json())
            return self.send_error(404)
        except PermissionError as exc:
            return self.send_json({"error": str(exc)}, 403)
        except ValueError as exc:
            return self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            return self.send_json({"error": f"操作失败：{str(exc)[:700]}"}, 500)

    def do_PUT(self):
        if not self._begin_auth_request():
            return
        token = _services().AUTH_ACTOR.set(self.auth_user)
        try:
            try:
                return self._handle_put()
            except Exception as exc:
                return self._handle_unexpected_request_error(exc)
        finally:
            _services().AUTH_ACTOR.reset(token)

    def _handle_put(self):
        path = _services().urlparse(self.path).path
        try:
            if path.startswith("/api/papers/") and path.endswith("/reading-card"):
                paper_id = path.split("/")[3]
                data = self.read_json()
                unknown = set(data) - set(_services().READING_CARD_FIELDS)
                if unknown:
                    raise ValueError("阅读卡包含未知字段")
                with _services().DB_LOCK, _services().db_session() as db:
                    if not db.execute(
                        "SELECT 1 FROM papers WHERE id=?", (paper_id,)
                    ).fetchone():
                        return self.send_json({"error": "论文不存在"}, 404)
                    card = _services().save_reading_card(db, paper_id, data)
                    _services().record_audit(
                        db,
                        "reading_card.updated",
                        "paper",
                        paper_id,
                        {
                            "completed_fields": card["completed_fields"],
                            "completion_pct": card["completion_pct"],
                        },
                    )
                return self.send_json(card)
        except ValueError as exc:
            return self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            return self.send_json({"error": f"操作失败：{str(exc)[:700]}"}, 500)
        return self.send_error(404)

    def do_PATCH(self):
        if not self._begin_auth_request():
            return
        token = _services().AUTH_ACTOR.set(self.auth_user)
        try:
            try:
                return self._handle_patch()
            except Exception as exc:
                return self._handle_unexpected_request_error(exc)
        finally:
            _services().AUTH_ACTOR.reset(token)

    def _handle_patch(self):
        path = _services().urlparse(self.path).path
        try:
            if path == "/api/auth/password":
                data = self.read_json()
                try:
                    session_token, csrf_token, user = _services().change_user_password(
                        self.auth_user["user_id"],
                        data.get("current_password", ""),
                        data.get("new_password", ""),
                    )
                except (PermissionError, ValueError) as exc:
                    return self.send_json({"error": str(exc)}, 400)
                return self.send_json(
                    {"user": user},
                    headers={
                        "Set-Cookie": self._auth_cookie_values(
                            session_token, csrf_token
                        )
                    },
                )
            member_match = _services().re.fullmatch(
                r"/api/workspace/members/([^/]+)", path
            )
            if member_match:
                try:
                    member = _services().update_workspace_user(
                        _services().unquote(member_match.group(1)),
                        self.auth_user,
                        self.read_json(),
                    )
                except LookupError as exc:
                    return self.send_json({"error": str(exc)}, 404)
                except ValueError as exc:
                    return self.send_json({"error": str(exc)}, 400)
                return self.send_json({"member": member})
            study_item_match = _services().re.fullmatch(
                r"/api/study-items/([^/]+)", path
            )
            if study_item_match:
                try:
                    item = _services().update_study_item(
                        _services().unquote(study_item_match.group(1)), self.read_json()
                    )
                except LookupError as exc:
                    return self.send_json({"error": str(exc)}, 404)
                return self.send_json(item)
            if path == "/api/settings/model":
                return self.send_json(_services().save_model_settings(self.read_json()))
            if path == "/api/settings/backup-policy":
                return self.send_json(_services().save_backup_policy(self.read_json()))
            candidate_feedback_match = _services().re.fullmatch(
                r"/api/runs/([^/]+)/candidates/([^/]+)", path
            )
            if candidate_feedback_match:
                data = self.read_json()
                return self.send_json(
                    _services().update_discovery_candidate_feedback(
                        candidate_feedback_match.group(1),
                        _services().unquote(candidate_feedback_match.group(2)),
                        data.get("decision", ""),
                    )
                )
            goal_patch_match = _services().re.fullmatch(
                r"/api/research-goals/([^/]+)", path
            )
            if goal_patch_match:
                goal_id = goal_patch_match.group(1)
                data = self.read_json()
                allowed = {
                    "name",
                    "objective",
                    "core_question",
                    "status",
                    "current_stage",
                    "sources",
                    "focus",
                    "exclusions",
                    "deliverables",
                    "success_criteria",
                    "stop_conditions",
                    "cadence_hours",
                    "auto_run",
                }
                updates = {key: value for key, value in data.items() if key in allowed}
                if "status" in updates and updates["status"] not in {
                    "draft",
                    "active",
                    "paused",
                    "completed",
                    "archived",
                }:
                    raise ValueError("未知研究目标状态")
                if "sources" in updates:
                    updates["sources_json"] = _services().json.dumps(
                        [
                            source
                            for source in updates.pop("sources")
                            if source in _services().SOURCE_SEARCH
                        ],
                        ensure_ascii=False,
                    )
                for key in (
                    "focus",
                    "exclusions",
                    "deliverables",
                    "success_criteria",
                    "stop_conditions",
                ):
                    if key in updates:
                        value = updates.pop(key)
                        if isinstance(value, str):
                            value = [
                                part.strip()
                                for part in value.split(",")
                                if part.strip()
                            ]
                        if not isinstance(value, list):
                            raise ValueError(f"{key} 必须是列表")
                        updates[key + "_json"] = _services().json.dumps(
                            [
                                _services().clean_text(item, 500)
                                for item in value
                                if _services().clean_text(item, 500)
                            ],
                            ensure_ascii=False,
                        )
                if "cadence_hours" in updates:
                    updates["cadence_hours"] = min(
                        max(int(updates["cadence_hours"]), 24), 8760
                    )
                if "auto_run" in updates:
                    raw_auto_run = updates["auto_run"]
                    updates["auto_run"] = int(
                        raw_auto_run is True
                        or str(raw_auto_run).casefold() in {"1", "true", "yes", "on"}
                    )
                if not updates:
                    raise ValueError("没有可更新的研究目标字段")
                with _services().DB_LOCK, _services().db_session() as db:
                    current = db.execute(
                        "SELECT auto_run,cadence_hours,workflow,next_run_at FROM research_goals WHERE id=?",
                        (goal_id,),
                    ).fetchone()
                    if not current:
                        return self.send_json({"error": "研究目标不存在"}, 404)
                    if updates.get("auto_run"):
                        if current["workflow"] != "follow_up":
                            raise ValueError("定期自动运行仅支持前沿追踪工作流")
                        cadence = updates.get(
                            "cadence_hours", int(current["cadence_hours"] or 168)
                        )
                        if (
                            not current["auto_run"]
                            or "cadence_hours" in updates
                            or not current["next_run_at"]
                        ):
                            updates["next_run_at"] = (
                                (
                                    _services().datetime.now(_services().timezone.utc)
                                    + _services().timedelta(hours=cadence)
                                )
                                .replace(microsecond=0)
                                .isoformat()
                            )
                    elif "auto_run" in updates:
                        updates["next_run_at"] = ""
                    elif "cadence_hours" in updates and current["auto_run"]:
                        updates["next_run_at"] = (
                            (
                                _services().datetime.now(_services().timezone.utc)
                                + _services().timedelta(hours=updates["cadence_hours"])
                            )
                            .replace(microsecond=0)
                            .isoformat()
                        )
                    updates["updated_at"] = _services().now_iso()
                    assignments = ",".join(f"{key}=?" for key in updates)
                    cur = db.execute(
                        f"UPDATE research_goals SET {assignments} WHERE id=?",
                        (*updates.values(), goal_id),
                    )
                if not cur.rowcount:
                    return self.send_json({"error": "研究目标不存在"}, 404)
                return self.send_json(_services().get_research_goal(goal_id))
            if path.startswith("/api/papers/"):
                pid = path.split("/")[3]
                data = self.read_json()
                allowed = {"status", "topic", "tags", "notes", "is_favorite"}
                updates = {k: v for k, v in data.items() if k in allowed}
                if (
                    "status" in updates
                    and updates["status"] not in _services().PAPER_STATUSES
                ):
                    raise ValueError("未知阅读状态")
                if not updates:
                    raise ValueError("没有可更新的字段")
                assignments, values = [], []
                for key, value in updates.items():
                    if key == "tags":
                        key, value = "tags_json", _services().json.dumps(
                            (
                                value
                                if isinstance(value, list)
                                else [
                                    x.strip()
                                    for x in str(value).split(",")
                                    if x.strip()
                                ]
                            ),
                            ensure_ascii=False,
                        )
                    if key == "is_favorite":
                        value = int(bool(value))
                    if key == "notes":
                        value = _services().clean_text(value, 100000)
                    assignments.append(f"{key}=?")
                    values.append(value)
                with _services().DB_LOCK, _services().db_session() as db:
                    cur = db.execute(
                        f"UPDATE papers SET {','.join(assignments)},updated_at=? WHERE id=?",
                        (*values, _services().now_iso(), pid),
                    )
                    if cur.rowcount:
                        if "notes" in updates:
                            _services().record_paper_revision(
                                db,
                                pid,
                                "notes",
                                {
                                    "notes": next(
                                        value
                                        for key, value in zip(updates.keys(), values)
                                        if key == "notes"
                                    )
                                },
                                "user",
                            )
                        _services().record_audit(
                            db,
                            "paper.updated",
                            "paper",
                            pid,
                            {"fields": sorted(updates)},
                        )
                return self.send_json({"updated": cur.rowcount > 0})
            if path.startswith("/api/queries/") and "/matches/" in path:
                parts = path.split("/")
                query_id, paper_id = parts[3], parts[5]
                data = self.read_json()
                decision = data.get("user_decision")
                if decision not in _services().QUERY_DECISIONS:
                    raise ValueError("未知的主题判断")
                viewed_at = _services().now_iso() if data.get("viewed", True) else ""
                with _services().DB_LOCK, _services().db_session() as db:
                    cur = db.execute(
                        "UPDATE query_matches SET user_decision=?,viewed_at=? WHERE query_id=? AND paper_id=?",
                        (decision, viewed_at, query_id, paper_id),
                    )
                if not cur.rowcount:
                    return self.send_json({"error": "主题匹配记录不存在"}, 404)
                with _services().DB_LOCK, _services().db_session() as db:
                    _services().record_audit(
                        db,
                        "query_match.feedback",
                        "query_match",
                        f"{query_id}:{paper_id}",
                        {
                            "decision": decision,
                            "viewed": bool(data.get("viewed", True)),
                        },
                    )
                return self.send_json(
                    {"updated": True, "user_decision": decision, "viewed_at": viewed_at}
                )
            if path.startswith("/api/queries/"):
                qid = path.split("/")[3]
                data = self.read_json()
                allowed = {
                    "name",
                    "query",
                    "sources",
                    "search_options",
                    "cadence_hours",
                    "active",
                }
                updates = {k: v for k, v in data.items() if k in allowed}
                if not updates:
                    raise ValueError("没有可更新的字段")
                assignments, values = [], []
                for key, value in updates.items():
                    if key == "sources":
                        key, value = "sources_json", _services().json.dumps(
                            [
                                s
                                for s in value
                                if s in _services().SOURCE_NAMES
                                and s in _services().SOURCE_SEARCH
                            ]
                        )
                    if key == "search_options":
                        key, value = "search_options_json", _services().json.dumps(
                            _services().normalize_search_options(
                                value if isinstance(value, dict) else {}
                            ),
                            ensure_ascii=False,
                        )
                    if key == "cadence_hours":
                        value = min(max(int(value), 24), 8760)
                    if key == "active":
                        value = int(bool(value))
                    assignments.append(f"{key}=?")
                    values.append(value)
                with _services().DB_LOCK, _services().db_session() as db:
                    db.execute(
                        f"UPDATE saved_queries SET {','.join(assignments)},updated_at=? WHERE id=?",
                        (*values, _services().now_iso(), qid),
                    )
                return self.send_json({"updated": True})
        except ValueError as exc:
            return self.send_json({"error": str(exc)}, 400)
        return self.send_error(404)

    def do_DELETE(self):
        if not self._begin_auth_request():
            return
        token = _services().AUTH_ACTOR.set(self.auth_user)
        try:
            try:
                return self._handle_delete()
            except Exception as exc:
                return self._handle_unexpected_request_error(exc)
        finally:
            _services().AUTH_ACTOR.reset(token)

    def _handle_delete(self):
        try:
            path = _services().urlparse(self.path).path
            study_item_match = _services().re.fullmatch(
                r"/api/study-items/([^/]+)", path
            )
            if study_item_match:
                item_id = _services().unquote(study_item_match.group(1))
                with _services().DB_LOCK, _services().db_session() as db:
                    cursor = db.execute(
                        "DELETE FROM study_items WHERE id=?", (item_id,)
                    )
                    if cursor.rowcount:
                        _services().record_audit(
                            db, "study_item.deleted", "study_item", item_id, {}
                        )
                return self.send_json({"deleted": bool(cursor.rowcount)})
            if path == "/api/papers/batch":
                data = self.read_json()
                ids = data.get("ids", [])
                protect_favorites = bool(data.get("protect_favorites", True))
                return self.send_json(
                    _services().delete_papers(ids, protect_favorites=protect_favorites)
                )
            paper_match = _services().re.fullmatch(r"/api/papers/([^/]+)", path)
            if paper_match:
                paper_id = _services().unquote(paper_match.group(1))
                data = self.read_json()
                return self.send_json(
                    _services().delete_papers(
                        [paper_id],
                        protect_favorites=bool(data.get("protect_favorites", False)),
                    )
                )
            if path.startswith("/api/queries/"):
                qid = path.split("/")[3]
                # Soft stop, preserving run history and query identity.
                with _services().DB_LOCK, _services().db_session() as db:
                    db.execute(
                        "UPDATE saved_queries SET active=0,updated_at=? WHERE id=?",
                        (_services().now_iso(), qid),
                    )
                    _services().record_audit(
                        db, "query.disabled", "saved_query", qid, {}
                    )
                return self.send_json({"stopped": True})
            folder_match = _services().re.fullmatch(r"/api/pdf-folders/([^/]+)", path)
            if folder_match:
                folder_id = _services().unquote(folder_match.group(1))
                with _services().DB_LOCK, _services().db_session() as db:
                    db.execute(
                        "UPDATE paper_folder_sources SET enabled=0,updated_at=? WHERE id=?",
                        (_services().now_iso(), folder_id),
                    )
                return self.send_json({"disabled": True, "id": folder_id})
            return self.send_error(404)
        except ValueError as exc:
            return self.send_json({"error": str(exc)}, 400)
        except LookupError as exc:
            return self.send_json({"error": str(exc)}, 404)
        except (_services().json.JSONDecodeError, UnicodeDecodeError) as exc:
            return self.send_json({"error": f"请求 JSON 无效：{str(exc)}"}, 400)
        except Exception as exc:
            logging.exception("DELETE request failed: %s", self.path)
            return self.send_json({"error": f"删除操作失败：{str(exc)[:300]}"}, 500)

    def save_query(self, data):
        name = _services().clean_text(data.get("name"), 200) or _services().clean_text(
            data.get("query"), 200
        )
        query = _services().clean_text(data.get("query"), 1000)
        sources = [
            s
            for s in data.get("sources", ["arxiv"])
            if s in _services().SOURCE_NAMES and s in _services().SOURCE_SEARCH
        ]
        search_options = _services().normalize_search_options(
            data.get("search_options", {})
        )
        cadence = min(max(int(data.get("cadence_hours", 168)), 24), 8760)
        if not query or not sources:
            raise ValueError("请填写检索词并至少选择一个来源")
        qid = str(_services().uuid.uuid4())
        now = _services().datetime.now(_services().timezone.utc).replace(microsecond=0)
        with _services().DB_LOCK, _services().db_session() as db:
            db.execute(
                "INSERT INTO saved_queries(id,name,query,sources_json,search_options_json,cadence_hours,active,last_run_at,next_run_at,created_at,updated_at) VALUES(?,?,?,?,?, ?,1,'',?,?,?)",
                (
                    qid,
                    name,
                    query,
                    _services().json.dumps(sources),
                    _services().json.dumps(search_options, ensure_ascii=False),
                    cadence,
                    now.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            _services().record_audit(
                db,
                "query.created",
                "saved_query",
                qid,
                {
                    "sources": sources,
                    "search_options": search_options,
                    "cadence_hours": cadence,
                },
            )
        return self.send_json(
            {
                "id": qid,
                "name": name,
                "query": query,
                "sources": sources,
                "search_options": search_options,
                "cadence_hours": cadence,
                "active": True,
                "next_run_at": now.isoformat(),
            },
            201,
        )

    def import_data(self, data):
        fmt = data.get("format", "json")
        content = _services().clean_text(data.get("content"), 25_000_000)
        records = []
        exported_cards = []
        exported_revisions = []
        exported_ai_artifacts = []
        exported_study_items = []
        exported_agent = {}
        exported_discovery = {}
        if fmt == "json":
            obj = _services().json.loads(content)
            records = obj.get("papers", obj) if isinstance(obj, dict) else obj
            if isinstance(obj, dict):
                exported_cards = (
                    obj.get("reading_cards", [])
                    if isinstance(obj.get("reading_cards", []), list)
                    else []
                )
                exported_revisions = (
                    obj.get("paper_revisions", [])
                    if isinstance(obj.get("paper_revisions", []), list)
                    else []
                )
                exported_ai_artifacts = (
                    obj.get("ai_artifacts", [])
                    if isinstance(obj.get("ai_artifacts", []), list)
                    else []
                )
                exported_study_items = (
                    obj.get("study_items", [])
                    if isinstance(obj.get("study_items", []), list)
                    else []
                )
                for key in (
                    "saved_queries",
                    "runs",
                    "run_snapshots",
                    "discovery_candidates",
                    "discovery_triage_jobs",
                    "discovery_candidate_feedback",
                    "discovery_search_memory",
                ):
                    exported_discovery[key] = (
                        obj.get(key, []) if isinstance(obj.get(key, []), list) else []
                    )
                for key in (
                    "research_goals",
                    "agent_runs",
                    "agent_steps",
                    "agent_tool_calls",
                    "agent_artifacts",
                    "research_claims",
                    "evidence_spans",
                    "claim_critiques",
                    "research_goal_papers",
                    "paper_version_links",
                ):
                    exported_agent[key] = (
                        obj.get(key, []) if isinstance(obj.get(key, []), list) else []
                    )
        elif fmt == "bibtex":
            chunks = _services().re.split(r"(?=\s*@\w+\s*\{)", content)
            for chunk in chunks:
                if not chunk.strip().startswith("@"):
                    continue
                entry = _services().re.search(r"@\w+\s*\{\s*([^,]+),", chunk)
                fields = {
                    m.group(1)
                    .lower(): _services()
                    .re.sub(r"\\[{}]", "", m.group(2))
                    .strip()
                    for m in _services().re.finditer(
                        r"(\w+)\s*=\s*[\{\"](.*?)[\}\"]\s*,?\s*(?=\w+\s*=|\})",
                        chunk,
                        _services().re.S,
                    )
                }
                if fields.get("title"):
                    records.append(
                        {
                            "title": fields.get("title"),
                            "authors": fields.get("author", ""),
                            "year": fields.get("year"),
                            "doi": fields.get("doi", ""),
                            "abstract": fields.get("abstract", ""),
                            "venue": fields.get("journal", fields.get("booktitle", "")),
                            "url": fields.get("url", ""),
                        }
                    )
        elif fmt == "ris":
            for chunk in _services().re.split(
                r"(?=^TY  - )", content, flags=_services().re.M
            ):
                if not chunk.strip():
                    continue
                fields = {}
                authors = []
                for line in chunk.splitlines():
                    m = _services().re.match(r"([A-Z0-9]{2})  - (.*)", line)
                    if not m:
                        continue
                    key, val = m.groups()
                    if key in {"AU", "A1"}:
                        authors.append(val)
                    else:
                        fields.setdefault(key, val)
                if fields.get("TI") or fields.get("T1"):
                    records.append(
                        {
                            "title": fields.get("TI", fields.get("T1")),
                            "authors": ", ".join(authors),
                            "year": fields.get("PY", "")[:4],
                            "doi": fields.get("DO", ""),
                            "abstract": fields.get("AB", ""),
                            "venue": fields.get("JO", fields.get("T2", "")),
                            "url": fields.get("UR", ""),
                        }
                    )
        else:
            raise ValueError("当前支持 JSON、BibTeX 和 RIS 导入")
        if not isinstance(records, list):
            raise ValueError("导入数据格式无效")
        created = duplicates = errors = 0
        imported_ids = {}
        for record in records[:10000]:
            try:
                paper_id, is_new = _services().upsert_paper(
                    record,
                    source="import",
                    external_id=record.get("doi", ""),
                    raw={"import_format": fmt},
                )
                imported_ids[str(record.get("id", ""))] = paper_id
                created += int(is_new)
                duplicates += int(not is_new)
            except Exception:
                errors += 1
        with _services().DB_LOCK, _services().db_session() as db:
            discovery_counts = {
                "saved_queries": 0,
                "runs": 0,
                "candidates": 0,
                "triage_jobs": 0,
                "feedback": 0,
                "search_memory": 0,
            }
            # Restore candidate-search history before queue rows because the
            # latter have a foreign key to runs.  This is intentionally
            # additive: importing a library JSON never overwrites local work.
            for query in exported_discovery.get("saved_queries", [])[:5000]:
                if (
                    not isinstance(query, dict)
                    or not query.get("id")
                    or not query.get("query")
                ):
                    continue
                sources = query.get("sources")
                options = query.get("search_options")
                if not isinstance(sources, list):
                    try:
                        sources = _services().json.loads(
                            query.get("sources_json") or "[]"
                        )
                    except (TypeError, ValueError):
                        sources = []
                if not isinstance(options, dict):
                    try:
                        options = _services().json.loads(
                            query.get("search_options_json") or "{}"
                        )
                    except (TypeError, ValueError):
                        options = {}
                db.execute(
                    """INSERT OR IGNORE INTO saved_queries(
                           id,name,query,sources_json,search_options_json,cadence_hours,
                           active,last_run_at,next_run_at,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        _services().clean_text(query.get("id"), 120),
                        _services().clean_text(query.get("name"), 300),
                        _services().clean_text(query.get("query"), 1000),
                        _services().json.dumps(sources),
                        _services().json.dumps(options, ensure_ascii=False),
                        int(query.get("cadence_hours") or 168),
                        int(bool(query.get("active", True))),
                        _services().clean_text(query.get("last_run_at"), 40),
                        _services().clean_text(query.get("next_run_at"), 40),
                        _services().clean_text(query.get("created_at"), 40)
                        or _services().now_iso(),
                        _services().clean_text(query.get("updated_at"), 40)
                        or _services().now_iso(),
                    ),
                )
                discovery_counts["saved_queries"] += int(
                    db.execute("SELECT changes()").fetchone()[0] > 0
                )
            run_columns = (
                "id",
                "query_id",
                "query_name",
                "status",
                "started_at",
                "completed_at",
                "new_count",
                "duplicate_count",
                "sources_json",
                "source_details_json",
                "error",
                "is_scheduled",
            )
            for run in exported_discovery.get("runs", [])[:10000]:
                if (
                    not isinstance(run, dict)
                    or not run.get("id")
                    or not run.get("status")
                ):
                    continue
                defaults = {
                    "query_id": "",
                    "query_name": "",
                    "completed_at": "",
                    "new_count": 0,
                    "duplicate_count": 0,
                    "sources_json": "[]",
                    "source_details_json": "[]",
                    "error": "",
                    "is_scheduled": 0,
                }
                db.execute(
                    f"INSERT OR IGNORE INTO runs({','.join(run_columns)}) VALUES({','.join('?' for _ in run_columns)})",
                    [
                        run.get(column, defaults.get(column, ""))
                        for column in run_columns
                    ],
                )
                discovery_counts["runs"] += int(
                    db.execute("SELECT changes()").fetchone()[0] > 0
                )
            for snapshot in exported_discovery.get("run_snapshots", [])[:10000]:
                if (
                    not isinstance(snapshot, dict)
                    or not snapshot.get("run_id")
                    or not db.execute(
                        "SELECT 1 FROM runs WHERE id=?", (snapshot["run_id"],)
                    ).fetchone()
                ):
                    continue
                db.execute(
                    "INSERT OR IGNORE INTO run_snapshots(run_id,query,plan_json,summary_json,created_at) VALUES(?,?,?,?,?)",
                    (
                        snapshot["run_id"],
                        _services().clean_text(snapshot.get("query"), 1000),
                        snapshot.get("plan_json", "{}"),
                        snapshot.get("summary_json", "{}"),
                        _services().clean_text(snapshot.get("created_at"), 40)
                        or _services().now_iso(),
                    ),
                )
            for candidate in exported_discovery.get("discovery_candidates", [])[
                :100000
            ]:
                if (
                    not isinstance(candidate, dict)
                    or not candidate.get("id")
                    or not candidate.get("run_id")
                    or not db.execute(
                        "SELECT 1 FROM runs WHERE id=?", (candidate["run_id"],)
                    ).fetchone()
                ):
                    continue
                old_imported_id = _services().clean_text(
                    candidate.get("imported_paper_id"), 120
                )
                imported_paper_id = (
                    imported_ids.get(old_imported_id, "") if old_imported_id else ""
                )
                db.execute(
                    """INSERT OR IGNORE INTO discovery_candidates(id,run_id,candidate_key,record_json,imported_paper_id,created_at)
                       VALUES(?,?,?,?,?,?)""",
                    (
                        _services().clean_text(candidate.get("id"), 140),
                        candidate["run_id"],
                        _services().clean_text(candidate.get("candidate_key"), 600),
                        candidate.get("record_json", "{}"),
                        imported_paper_id,
                        _services().clean_text(candidate.get("created_at"), 40)
                        or _services().now_iso(),
                    ),
                )
                discovery_counts["candidates"] += int(
                    db.execute("SELECT changes()").fetchone()[0] > 0
                )
                snapshot_row = db.execute(
                    "SELECT query,plan_json,summary_json FROM run_snapshots WHERE run_id=?",
                    (candidate["run_id"],),
                ).fetchone()
                record = _services()._json_object(candidate.get("record_json", "{}"))
                summary = _services()._json_object(
                    snapshot_row["summary_json"] if snapshot_row else "{}"
                )
                plan = _services()._json_object(
                    snapshot_row["plan_json"] if snapshot_row else "{}"
                )
                intent_key = _services().clean_text(
                    record.get("intent_key")
                    or summary.get("intent_key")
                    or plan.get("discovery_intent_key"),
                    100,
                )
                if not intent_key and snapshot_row:
                    intent_key = _services().discovery_intent_key(
                        snapshot_row["query"],
                        plan.get("filters") or plan.get("search_options") or {},
                    )
                before_memory = db.total_changes
                _services().record_discovery_search_memory(
                    db,
                    intent_key,
                    record,
                    candidate["run_id"],
                    candidate.get("created_at") or _services().now_iso(),
                )
                discovery_counts["search_memory"] += db.total_changes - before_memory
            for memory in exported_discovery.get("discovery_search_memory", [])[
                :500000
            ]:
                if (
                    not isinstance(memory, dict)
                    or not memory.get("intent_key")
                    or not memory.get("identity_key")
                ):
                    continue
                db.execute(
                    """INSERT OR IGNORE INTO discovery_search_memory(intent_key,identity_key,title,first_seen_at,last_seen_at,seen_count,last_run_id)
                       VALUES(?,?,?,?,?,?,?)""",
                    (
                        _services().clean_text(memory.get("intent_key"), 100),
                        _services().clean_text(memory.get("identity_key"), 700),
                        _services().clean_text(memory.get("title"), 600),
                        _services().clean_text(memory.get("first_seen_at"), 40)
                        or _services().now_iso(),
                        _services().clean_text(memory.get("last_seen_at"), 40)
                        or _services().now_iso(),
                        max(1, int(memory.get("seen_count") or 1)),
                        _services().clean_text(memory.get("last_run_id"), 80),
                    ),
                )
                discovery_counts["search_memory"] += int(
                    db.execute("SELECT changes()").fetchone()[0] > 0
                )
            for job in exported_discovery.get("discovery_triage_jobs", [])[:10000]:
                if (
                    not isinstance(job, dict)
                    or not job.get("run_id")
                    or not db.execute(
                        "SELECT 1 FROM runs WHERE id=?", (job["run_id"],)
                    ).fetchone()
                ):
                    continue
                db.execute(
                    """INSERT OR IGNORE INTO discovery_triage_jobs(
                           run_id,status,model,candidate_count,processed_count,error,created_at,updated_at,completed_at
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        job["run_id"],
                        _services().clean_text(job.get("status"), 40) or "interrupted",
                        _services().clean_text(job.get("model"), 200),
                        max(0, int(job.get("candidate_count") or 0)),
                        max(0, int(job.get("processed_count") or 0)),
                        _services().clean_text(job.get("error"), 1000),
                        _services().clean_text(job.get("created_at"), 40)
                        or _services().now_iso(),
                        _services().clean_text(job.get("updated_at"), 40)
                        or _services().now_iso(),
                        _services().clean_text(job.get("completed_at"), 40),
                    ),
                )
                discovery_counts["triage_jobs"] += int(
                    db.execute("SELECT changes()").fetchone()[0] > 0
                )
            for feedback_row in exported_discovery.get(
                "discovery_candidate_feedback", []
            )[:100000]:
                if (
                    not isinstance(feedback_row, dict)
                    or not feedback_row.get("intent_key")
                    or not feedback_row.get("candidate_key")
                ):
                    continue
                decision = (
                    _services().clean_text(feedback_row.get("decision"), 30).lower()
                )
                if decision not in _services().DISCOVERY_FEEDBACK_DECISIONS | {"new"}:
                    continue
                created_at = (
                    _services().clean_text(feedback_row.get("created_at"), 40)
                    or _services().now_iso()
                )
                db.execute(
                    """INSERT OR IGNORE INTO discovery_candidate_feedback(
                           intent_key,candidate_key,decision,note,candidate_title,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        _services().clean_text(feedback_row.get("intent_key"), 100),
                        _services().clean_text(feedback_row.get("candidate_key"), 600),
                        decision,
                        _services().clean_text(feedback_row.get("note"), 300),
                        _services().clean_text(
                            feedback_row.get("candidate_title"), 600
                        ),
                        created_at,
                        _services().clean_text(feedback_row.get("updated_at"), 40)
                        or created_at,
                    ),
                )
                discovery_counts["feedback"] += int(
                    db.execute("SELECT changes()").fetchone()[0] > 0
                )
            cards_imported = 0
            for card in exported_cards[:10000]:
                if not isinstance(card, dict):
                    continue
                target_id = imported_ids.get(str(card.get("paper_id", "")))
                if (
                    target_id
                    and db.execute(
                        "SELECT 1 FROM papers WHERE id=?", (target_id,)
                    ).fetchone()
                ):
                    _services().save_reading_card(
                        db,
                        target_id,
                        {
                            field: card.get(field, "")
                            for field in _services().READING_CARD_FIELDS
                        },
                    )
                    cards_imported += 1
            revisions_imported = 0
            for revision in exported_revisions[:100000]:
                if not isinstance(revision, dict) or revision.get(
                    "revision_type"
                ) not in {"notes", "reading_card"}:
                    continue
                target_id = imported_ids.get(str(revision.get("paper_id", "")))
                try:
                    content_json = revision.get("content_json", "{}")
                    content = (
                        _services().json.loads(content_json)
                        if isinstance(content_json, str)
                        else content_json
                    )
                    if not isinstance(content, dict):
                        continue
                    created_at = (
                        _services().clean_text(revision.get("created_at", ""), 40)
                        or _services().now_iso()
                    )
                    serialized = _services().json.dumps(
                        content, ensure_ascii=False, sort_keys=True
                    )
                    if (
                        target_id
                        and db.execute(
                            "SELECT 1 FROM papers WHERE id=?", (target_id,)
                        ).fetchone()
                        and not db.execute(
                            "SELECT 1 FROM paper_revisions WHERE paper_id=? AND revision_type=? AND content_json=? AND created_at=?",
                            (
                                target_id,
                                revision["revision_type"],
                                serialized,
                                created_at,
                            ),
                        ).fetchone()
                    ):
                        db.execute(
                            "INSERT INTO paper_revisions(paper_id,revision_type,content_json,source,created_at) VALUES(?,?,?,?,?)",
                            (
                                target_id,
                                revision["revision_type"],
                                serialized,
                                _services().clean_text(
                                    revision.get("source", "import"), 40
                                )
                                or "import",
                                created_at,
                            ),
                        )
                        revisions_imported += 1
                except (TypeError, ValueError, _services().json.JSONDecodeError):
                    errors += 1
            ai_artifacts_imported = 0
            imported_artifact_ids = {}
            for artifact in exported_ai_artifacts[:10000]:
                if not isinstance(artifact, dict):
                    continue
                target_id = imported_ids.get(str(artifact.get("paper_id", "")))
                artifact_type = _services().clean_text(
                    artifact.get("artifact_type", ""), 80
                )
                answer = artifact.get("answer", "")
                if (
                    not target_id
                    or not artifact_type
                    or not isinstance(answer, str)
                    or len(answer) > 2_000_000
                ):
                    continue
                created_at = (
                    _services().clean_text(artifact.get("created_at", ""), 40)
                    or _services().now_iso()
                )
                prompt_version = _services().clean_text(
                    artifact.get("prompt_version", ""), 100
                )
                input_sha256 = _services().clean_text(
                    artifact.get("input_sha256", ""), 64
                )
                source_artifact_id = _services().clean_text(artifact.get("id", ""), 100)
                existing_artifact = db.execute(
                    "SELECT 1 FROM ai_artifacts WHERE paper_id=? AND artifact_type=? AND prompt_version=? AND input_sha256=? AND created_at=?",
                    (
                        target_id,
                        artifact_type,
                        prompt_version,
                        input_sha256,
                        created_at,
                    ),
                ).fetchone()
                if not db.execute(
                    "SELECT 1 FROM papers WHERE id=?", (target_id,)
                ).fetchone():
                    continue
                if existing_artifact:
                    imported_artifact_id = db.execute(
                        "SELECT id FROM ai_artifacts WHERE paper_id=? AND artifact_type=? AND prompt_version=? AND input_sha256=? AND created_at=?",
                        (
                            target_id,
                            artifact_type,
                            prompt_version,
                            input_sha256,
                            created_at,
                        ),
                    ).fetchone()[0]
                else:
                    imported_artifact_id = source_artifact_id or str(
                        _services().uuid.uuid4()
                    )
                    collision = db.execute(
                        "SELECT paper_id FROM ai_artifacts WHERE id=?",
                        (imported_artifact_id,),
                    ).fetchone()
                    if collision and collision[0] != target_id:
                        imported_artifact_id = str(_services().uuid.uuid4())
                    db.execute(
                        "INSERT INTO ai_artifacts(id,paper_id,artifact_type,provider,model,prompt_version,input_sha256,answer,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            imported_artifact_id,
                            target_id,
                            artifact_type,
                            _services().clean_text(artifact.get("provider", ""), 500),
                            _services().clean_text(artifact.get("model", ""), 200),
                            prompt_version,
                            input_sha256,
                            answer,
                            created_at,
                        ),
                    )
                    ai_artifacts_imported += 1
                if source_artifact_id:
                    imported_artifact_ids[source_artifact_id] = imported_artifact_id
                try:
                    saved = _services().json.loads(answer)
                    analysis = (
                        saved.get("analysis", {}) if isinstance(saved, dict) else {}
                    )
                    guide = (
                        analysis.get("prereading_guide", {})
                        if isinstance(analysis, dict)
                        else {}
                    )
                    _services().sync_prereading_study_items(
                        db, target_id, guide, imported_artifact_id, created_at
                    )
                except (TypeError, ValueError, _services().json.JSONDecodeError):
                    pass
            study_items_imported = 0
            for item in exported_study_items[:100000]:
                if not isinstance(item, dict):
                    continue
                target_id = imported_ids.get(str(item.get("paper_id", "")))
                label = _services().clean_text(item.get("label", ""), 240)
                item_type = item.get("item_type")
                mastery = item.get("mastery", "new")
                if (
                    not target_id
                    or not label
                    or item_type not in {"prerequisite", "glossary", "notation"}
                    or mastery not in {"new", "learning", "mastered"}
                ):
                    continue
                item_key = (
                    _services()
                    .re.sub(
                        r"\s+",
                        " ",
                        _services().clean_text(item.get("item_key") or label, 240),
                    )
                    .casefold()
                )
                try:
                    page_number = (
                        int(item.get("page_number"))
                        if item.get("page_number") is not None
                        else None
                    )
                except (TypeError, ValueError):
                    page_number = None
                try:
                    review_interval_days = min(
                        36500, max(0, int(item.get("review_interval_days") or 0))
                    )
                    review_count = min(
                        1_000_000, max(0, int(item.get("review_count") or 0))
                    )
                    review_lapses = min(
                        1_000_000, max(0, int(item.get("review_lapses") or 0))
                    )
                    review_ease = min(
                        3.0, max(1.3, float(item.get("review_ease", 2.5)))
                    )
                except (TypeError, ValueError, OverflowError):
                    review_interval_days, review_count, review_lapses, review_ease = (
                        0,
                        0,
                        0,
                        2.5,
                    )
                created_at = (
                    _services().clean_text(item.get("created_at", ""), 40)
                    or _services().now_iso()
                )
                updated_at = (
                    _services().clean_text(item.get("updated_at", ""), 40) or created_at
                )
                db.execute(
                    """INSERT INTO study_items(
                           id,paper_id,item_type,item_key,label,expansion,explanation,paper_role,basis,
                           necessity,page_number,quote,mastery,review_due_at,review_interval_days,review_count,
                           review_lapses,review_ease,last_reviewed_at,user_note,source_artifact_id,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(paper_id,item_type,item_key) DO UPDATE SET
                           mastery=CASE WHEN study_items.mastery='new' THEN excluded.mastery ELSE study_items.mastery END,
                           user_note=CASE WHEN study_items.user_note='' THEN excluded.user_note ELSE study_items.user_note END,
                           review_due_at=CASE WHEN excluded.last_reviewed_at>study_items.last_reviewed_at THEN excluded.review_due_at ELSE study_items.review_due_at END,
                           review_interval_days=CASE WHEN excluded.last_reviewed_at>study_items.last_reviewed_at THEN excluded.review_interval_days ELSE study_items.review_interval_days END,
                           review_count=CASE WHEN excluded.last_reviewed_at>study_items.last_reviewed_at THEN excluded.review_count ELSE study_items.review_count END,
                           review_lapses=CASE WHEN excluded.last_reviewed_at>study_items.last_reviewed_at THEN excluded.review_lapses ELSE study_items.review_lapses END,
                           review_ease=CASE WHEN excluded.last_reviewed_at>study_items.last_reviewed_at THEN excluded.review_ease ELSE study_items.review_ease END,
                           last_reviewed_at=MAX(study_items.last_reviewed_at,excluded.last_reviewed_at),
                           updated_at=MAX(study_items.updated_at,excluded.updated_at)""",
                    (
                        str(_services().uuid.uuid4()),
                        target_id,
                        item_type,
                        item_key,
                        label,
                        _services().clean_text(item.get("expansion", ""), 500),
                        _services().clean_text(item.get("explanation", ""), 1200),
                        _services().clean_text(item.get("paper_role", ""), 1000),
                        _services().clean_text(item.get("basis", ""), 40),
                        _services().clean_text(item.get("necessity", ""), 20),
                        page_number,
                        _services().clean_text(item.get("quote", ""), 500),
                        mastery,
                        _services().clean_text(item.get("review_due_at", ""), 40),
                        review_interval_days,
                        review_count,
                        review_lapses,
                        review_ease,
                        _services().clean_text(item.get("last_reviewed_at", ""), 40),
                        _services().clean_text(item.get("user_note", ""), 2000),
                        imported_artifact_ids.get(
                            _services().clean_text(
                                item.get("source_artifact_id", ""), 100
                            ),
                            _services().clean_text(
                                item.get("source_artifact_id", ""), 100
                            ),
                        ),
                        created_at,
                        updated_at,
                    ),
                )
                study_items_imported += int(
                    db.execute("SELECT changes()").fetchone()[0] > 0
                )
            agent_counts = {
                "goals": 0,
                "runs": 0,
                "steps": 0,
                "claims": 0,
                "evidence": 0,
                "artifacts": 0,
                "critiques": 0,
                "goal_papers": 0,
                "version_links": 0,
            }
            # Agent data is restored after papers so evidence can be remapped
            # to the imported library IDs.  Existing IDs are kept to preserve
            # long-term links and are ignored on duplicate imports.
            for goal in exported_agent.get("research_goals", [])[:1000]:
                if not isinstance(goal, dict) or not goal.get("id"):
                    continue
                columns = [
                    "id",
                    "name",
                    "objective",
                    "core_question",
                    "scope_json",
                    "time_range_json",
                    "focus_json",
                    "exclusions_json",
                    "deliverables_json",
                    "success_criteria_json",
                    "stop_conditions_json",
                    "sources_json",
                    "workflow",
                    "status",
                    "current_stage",
                    "created_at",
                    "updated_at",
                    "last_run_at",
                    "next_run_at",
                ]
                values = [goal.get(column, "") for column in columns]
                db.execute(
                    f"INSERT OR IGNORE INTO research_goals({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    values,
                )
                agent_counts["goals"] += int(
                    db.execute("SELECT changes()").fetchone()[0] > 0
                )
            for run in exported_agent.get("agent_runs", [])[:5000]:
                if (
                    not isinstance(run, dict)
                    or not run.get("id")
                    or not db.execute(
                        "SELECT 1 FROM research_goals WHERE id=?",
                        (run.get("goal_id", ""),),
                    ).fetchone()
                ):
                    continue
                columns = [
                    "id",
                    "goal_id",
                    "workflow",
                    "status",
                    "current_step_id",
                    "trigger",
                    "plan_json",
                    "budget_json",
                    "error",
                    "created_at",
                    "started_at",
                    "updated_at",
                    "completed_at",
                ]
                db.execute(
                    f"INSERT OR IGNORE INTO agent_runs({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    [run.get(column, "") for column in columns],
                )
                agent_counts["runs"] += int(
                    db.execute("SELECT changes()").fetchone()[0] > 0
                )
            for step in exported_agent.get("agent_steps", [])[:20000]:
                if (
                    not isinstance(step, dict)
                    or not step.get("id")
                    or not db.execute(
                        "SELECT 1 FROM agent_runs WHERE id=?", (step.get("run_id", ""),)
                    ).fetchone()
                ):
                    continue
                columns = [
                    "id",
                    "run_id",
                    "step_key",
                    "agent_type",
                    "title",
                    "tool_name",
                    "tool_class",
                    "ordinal",
                    "dependencies_json",
                    "input_json",
                    "output_json",
                    "input_schema_json",
                    "output_schema_json",
                    "completion_json",
                    "failure_policy_json",
                    "budget_json",
                    "status",
                    "requires_confirmation",
                    "attempts",
                    "max_attempts",
                    "progress_current",
                    "progress_total",
                    "message",
                    "error",
                    "created_at",
                    "started_at",
                    "updated_at",
                    "completed_at",
                ]
                db.execute(
                    f"INSERT OR IGNORE INTO agent_steps({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    [
                        step.get(
                            column,
                            (
                                0
                                if column
                                in {
                                    "ordinal",
                                    "requires_confirmation",
                                    "attempts",
                                    "max_attempts",
                                    "progress_current",
                                    "progress_total",
                                }
                                else ""
                            ),
                        )
                        for column in columns
                    ],
                )
                agent_counts["steps"] += int(
                    db.execute("SELECT changes()").fetchone()[0] > 0
                )
            for call in exported_agent.get("agent_tool_calls", [])[:50000]:
                if (
                    not isinstance(call, dict)
                    or not call.get("id")
                    or not db.execute(
                        "SELECT 1 FROM agent_runs WHERE id=?", (call.get("run_id", ""),)
                    ).fetchone()
                    or not db.execute(
                        "SELECT 1 FROM agent_steps WHERE id=?",
                        (call.get("step_id", ""),),
                    ).fetchone()
                ):
                    continue
                columns = [
                    "id",
                    "run_id",
                    "step_id",
                    "tool_name",
                    "tool_class",
                    "status",
                    "input_json",
                    "output_json",
                    "error",
                    "attempts",
                    "started_at",
                    "completed_at",
                    "duration_ms",
                ]
                db.execute(
                    f"INSERT OR IGNORE INTO agent_tool_calls({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    [
                        call.get(
                            column, 0 if column in {"attempts", "duration_ms"} else ""
                        )
                        for column in columns
                    ],
                )
            for artifact in exported_agent.get("agent_artifacts", [])[:20000]:
                if (
                    not isinstance(artifact, dict)
                    or not artifact.get("id")
                    or not db.execute(
                        "SELECT 1 FROM agent_runs WHERE id=?",
                        (artifact.get("run_id", ""),),
                    ).fetchone()
                    or not db.execute(
                        "SELECT 1 FROM agent_steps WHERE id=?",
                        (artifact.get("step_id", ""),),
                    ).fetchone()
                ):
                    continue
                columns = [
                    "id",
                    "goal_id",
                    "run_id",
                    "step_id",
                    "artifact_type",
                    "title",
                    "content_json",
                    "version",
                    "created_at",
                ]
                db.execute(
                    f"INSERT OR IGNORE INTO agent_artifacts({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    [
                        artifact.get(column, 1 if column == "version" else "")
                        for column in columns
                    ],
                )
                agent_counts["artifacts"] += int(
                    db.execute("SELECT changes()").fetchone()[0] > 0
                )
            for claim in exported_agent.get("research_claims", [])[:50000]:
                if (
                    not isinstance(claim, dict)
                    or not claim.get("id")
                    or not db.execute(
                        "SELECT 1 FROM research_goals WHERE id=?",
                        (claim.get("goal_id", ""),),
                    ).fetchone()
                    or not db.execute(
                        "SELECT 1 FROM agent_runs WHERE id=?",
                        (claim.get("run_id", ""),),
                    ).fetchone()
                ):
                    continue
                columns = [
                    "id",
                    "goal_id",
                    "run_id",
                    "paper_id",
                    "claim_text",
                    "claim_type",
                    "status",
                    "confidence",
                    "evidence_scope",
                    "created_at",
                    "updated_at",
                ]
                values = [
                    (
                        imported_ids.get(
                            str(claim.get(column, "")), claim.get(column, "")
                        )
                        if column == "paper_id"
                        else claim.get(column, "")
                    )
                    for column in columns
                ]
                db.execute(
                    f"INSERT OR IGNORE INTO research_claims({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    values,
                )
                agent_counts["claims"] += int(
                    db.execute("SELECT changes()").fetchone()[0] > 0
                )
            for evidence in exported_agent.get("evidence_spans", [])[:100000]:
                if (
                    not isinstance(evidence, dict)
                    or not evidence.get("id")
                    or not db.execute(
                        "SELECT 1 FROM research_claims WHERE id=?",
                        (evidence.get("claim_id", ""),),
                    ).fetchone()
                ):
                    continue
                columns = [
                    "id",
                    "claim_id",
                    "paper_id",
                    "attachment_id",
                    "page_number",
                    "section",
                    "quote",
                    "evidence_type",
                    "verification_status",
                    "source_sha256",
                    "created_at",
                ]
                values = [
                    (
                        imported_ids.get(
                            str(evidence.get(column, "")), evidence.get(column, "")
                        )
                        if column == "paper_id"
                        else evidence.get(
                            column, None if column == "page_number" else ""
                        )
                    )
                    for column in columns
                ]
                db.execute(
                    f"INSERT OR IGNORE INTO evidence_spans({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    values,
                )
                agent_counts["evidence"] += int(
                    db.execute("SELECT changes()").fetchone()[0] > 0
                )
            for critique in exported_agent.get("claim_critiques", [])[:100000]:
                if (
                    not isinstance(critique, dict)
                    or not critique.get("id")
                    or not db.execute(
                        "SELECT 1 FROM research_claims WHERE id=?",
                        (critique.get("claim_id", ""),),
                    ).fetchone()
                    or not db.execute(
                        "SELECT 1 FROM research_goals WHERE id=?",
                        (critique.get("goal_id", ""),),
                    ).fetchone()
                    or not db.execute(
                        "SELECT 1 FROM agent_runs WHERE id=?",
                        (critique.get("run_id", ""),),
                    ).fetchone()
                ):
                    continue
                columns = [
                    "id",
                    "claim_id",
                    "goal_id",
                    "run_id",
                    "category",
                    "severity",
                    "critique_text",
                    "evidence_json",
                    "status",
                    "created_at",
                ]
                db.execute(
                    f"INSERT OR IGNORE INTO claim_critiques({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    [critique.get(column, "") for column in columns],
                )
                agent_counts["critiques"] += int(
                    db.execute("SELECT changes()").fetchone()[0] > 0
                )
            for goal_paper in exported_agent.get("research_goal_papers", [])[:100000]:
                if (
                    not isinstance(goal_paper, dict)
                    or not goal_paper.get("goal_id")
                    or not goal_paper.get("paper_key")
                    or not db.execute(
                        "SELECT 1 FROM research_goals WHERE id=?",
                        (goal_paper.get("goal_id", ""),),
                    ).fetchone()
                ):
                    continue
                columns = [
                    "goal_id",
                    "paper_key",
                    "local_paper_id",
                    "title",
                    "doi",
                    "arxiv_id",
                    "version_key",
                    "first_seen_at",
                    "last_seen_at",
                    "seen_count",
                    "first_run_id",
                    "last_run_id",
                    "user_decision",
                    "reading_status",
                    "change_summary",
                    "metadata_json",
                ]
                values = [
                    (
                        imported_ids.get(
                            str(goal_paper.get(column, "")), goal_paper.get(column, "")
                        )
                        if column == "local_paper_id"
                        else goal_paper.get(column, 1 if column == "seen_count" else "")
                    )
                    for column in columns
                ]
                db.execute(
                    f"INSERT OR IGNORE INTO research_goal_papers({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    values,
                )
                agent_counts["goal_papers"] += int(
                    db.execute("SELECT changes()").fetchone()[0] > 0
                )
            for link in exported_agent.get("paper_version_links", [])[:100000]:
                if (
                    not isinstance(link, dict)
                    or not link.get("canonical_key")
                    or not link.get("paper_key")
                ):
                    continue
                columns = [
                    "canonical_key",
                    "paper_key",
                    "relation",
                    "confidence",
                    "metadata_json",
                    "first_seen_at",
                    "last_seen_at",
                ]
                db.execute(
                    f"INSERT OR IGNORE INTO paper_version_links({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    [
                        link.get(column, 0 if column == "confidence" else "")
                        for column in columns
                    ],
                )
                agent_counts["version_links"] += int(
                    db.execute("SELECT changes()").fetchone()[0] > 0
                )
            _services().record_audit(
                db,
                "library.imported",
                "import",
                "",
                {
                    "format": fmt,
                    "records": min(len(records), 10000),
                    "imported": created,
                    "duplicates": duplicates,
                    "errors": errors,
                    "reading_cards": cards_imported,
                    "revisions": revisions_imported,
                    "ai_artifacts": ai_artifacts_imported,
                    "study_items": study_items_imported,
                    "agent": agent_counts,
                },
            )
        return self.send_json(
            {
                "imported": created,
                "duplicates": duplicates,
                "errors": errors,
                "reading_cards": cards_imported,
                "revisions": revisions_imported,
                "ai_artifacts": ai_artifacts_imported,
                "study_items": study_items_imported,
                "discovery": discovery_counts,
                "agent": agent_counts,
            }
        )

    def export_json(self):
        with _services().DB_LOCK, _services().db_session() as db:
            papers = [
                _services().paper_dict(r)
                for r in db.execute("SELECT * FROM papers ORDER BY created_at")
            ]
            sources = [
                dict(r) for r in db.execute("SELECT * FROM source_records ORDER BY id")
            ]
            queries = [
                dict(r)
                for r in db.execute("SELECT * FROM saved_queries ORDER BY created_at")
            ]
            for query in queries:
                try:
                    query["sources"] = _services().json.loads(
                        query.pop("sources_json", "[]") or "[]"
                    )
                except _services().json.JSONDecodeError:
                    query["sources"] = []
                try:
                    query["search_options"] = _services().json.loads(
                        query.pop("search_options_json", "{}") or "{}"
                    )
                except _services().json.JSONDecodeError:
                    query["search_options"] = {}
            runs = [
                dict(r) for r in db.execute("SELECT * FROM runs ORDER BY started_at")
            ]
            run_snapshots = [
                dict(r)
                for r in db.execute("SELECT * FROM run_snapshots ORDER BY created_at")
            ]
            run_papers = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM run_papers ORDER BY run_id,search_rank"
                )
            ]
            discovery_candidates = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM discovery_candidates ORDER BY created_at,id"
                )
            ]
            discovery_triage_jobs = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM discovery_triage_jobs ORDER BY created_at,run_id"
                )
            ]
            discovery_candidate_feedback = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM discovery_candidate_feedback ORDER BY updated_at,intent_key,candidate_key"
                )
            ]
            discovery_search_memory = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM discovery_search_memory ORDER BY intent_key,identity_key"
                )
            ]
            attachments = [dict(r) for r in db.execute("SELECT * FROM attachments")]
            matches = [
                dict(r)
                for r in db.execute("SELECT * FROM query_matches ORDER BY last_seen_at")
            ]
            citations = [
                dict(r) for r in db.execute("SELECT * FROM citation_edges ORDER BY id")
            ]
            feedback = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM learning_feedback ORDER BY created_at"
                )
            ]
            study_items = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM study_items ORDER BY paper_id,item_type,label COLLATE NOCASE"
                )
            ]
            audit = [
                dict(r) for r in db.execute("SELECT * FROM audit_events ORDER BY id")
            ]
            artifacts = [
                dict(r)
                for r in db.execute("SELECT * FROM ai_artifacts ORDER BY created_at")
            ]
            model = dict(
                db.execute(
                    "SELECT provider,base_url,api_url,website_url,model,fast_model,analysis_model,vision_model,protocol,temperature,embedding_model,embedding_api_url,deepseek_search_enabled,deepseek_search_api_url,deepseek_fetch_api_url,enabled,updated_at FROM model_config WHERE id=1"
                ).fetchone()
                or {}
            )
            document_pages = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM document_pages ORDER BY paper_id,attachment_id,page_number"
                )
            ]
            reading_cards = [
                _services().reading_card_dict(r, r["paper_id"])
                for r in db.execute("SELECT * FROM reading_cards ORDER BY updated_at")
            ]
            revisions = [
                dict(r) for r in db.execute("SELECT * FROM paper_revisions ORDER BY id")
            ]
            agent_goals = [
                dict(r)
                for r in db.execute("SELECT * FROM research_goals ORDER BY created_at")
            ]
            agent_runs = [
                dict(r)
                for r in db.execute("SELECT * FROM agent_runs ORDER BY created_at")
            ]
            agent_steps = [
                dict(r)
                for r in db.execute("SELECT * FROM agent_steps ORDER BY run_id,ordinal")
            ]
            agent_tool_calls = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM agent_tool_calls ORDER BY started_at"
                )
            ]
            agent_artifacts = [
                dict(r)
                for r in db.execute("SELECT * FROM agent_artifacts ORDER BY created_at")
            ]
            research_claims = [
                dict(r)
                for r in db.execute("SELECT * FROM research_claims ORDER BY created_at")
            ]
            evidence_spans = [
                dict(r)
                for r in db.execute("SELECT * FROM evidence_spans ORDER BY created_at")
            ]
            claim_critiques = [
                dict(r)
                for r in db.execute("SELECT * FROM claim_critiques ORDER BY created_at")
            ]
            goal_papers = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM research_goal_papers ORDER BY first_seen_at"
                )
            ]
            version_links = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM paper_version_links ORDER BY canonical_key,paper_key"
                )
            ]
        payload = {
            "schema_version": _services().DB_SCHEMA_VERSION,
            "exported_at": _services().now_iso(),
            "papers": papers,
            "source_records": sources,
            "saved_queries": queries,
            "runs": runs,
            "run_snapshots": run_snapshots,
            "run_papers": run_papers,
            "discovery_candidates": discovery_candidates,
            "discovery_triage_jobs": discovery_triage_jobs,
            "discovery_candidate_feedback": discovery_candidate_feedback,
            "discovery_search_memory": discovery_search_memory,
            "query_matches": matches,
            "citation_edges": citations,
            "learning_feedback": feedback,
            "study_items": study_items,
            "audit_events": audit,
            "ai_artifacts": artifacts,
            "model_config": model,
            "attachments": attachments,
            "document_pages": document_pages,
            "reading_cards": reading_cards,
            "paper_revisions": revisions,
            "research_goals": agent_goals,
            "agent_runs": agent_runs,
            "agent_steps": agent_steps,
            "agent_tool_calls": agent_tool_calls,
            "agent_artifacts": agent_artifacts,
            "research_claims": research_claims,
            "evidence_spans": evidence_spans,
            "claim_critiques": claim_critiques,
            "research_goal_papers": goal_papers,
            "paper_version_links": version_links,
        }
        return self.send_download(
            "literature-library.json",
            "application/json; charset=utf-8",
            _services()
            .json.dumps(payload, ensure_ascii=False, indent=2)
            .encode("utf-8"),
        )

    def export_bibtex(self):
        with _services().DB_LOCK, _services().db_session() as db:
            papers = [
                _services().paper_dict(r)
                for r in db.execute("SELECT * FROM papers ORDER BY year DESC,title")
            ]
        lines = []
        for p in papers:
            key = _services().re.sub(
                r"\W+",
                "",
                (
                    (p.get("authors", "").split(",")[0].split()[-1:] or ["paper"])[0]
                    + str(p.get("year") or "")
                    + p["id"][:4]
                ),
            )
            fields = [
                ("title", p["title"]),
                ("author", p["authors"]),
                ("year", p["year"]),
                ("journal", p["venue"]),
                ("doi", p["doi"]),
                ("url", p["url"]),
                ("abstract", p["abstract"]),
            ]
            lines.append("@article{" + key + ",")
            for k, v in fields:
                if v:
                    lines.append(
                        f"  {k} = {{{str(v).replace('{','(').replace('}',')')}}},"
                    )
            lines.append("}\n")
        return self.send_download(
            "literature-library.bib",
            "application/x-bibtex; charset=utf-8",
            "\n".join(lines).encode("utf-8"),
        )

    def send_download(self, filename: str, content_type: str, data: bytes):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Request-ID", getattr(self, "request_id", ""))
        self.end_headers()
        self.wfile.write(data)

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

    def agent_brief(self, paper_id: str, data: dict):
        if not data.get("consent"):
            raise ValueError(
                "请先确认允许将这篇论文的元数据/摘要发送到已配置的模型服务"
            )
        settings = _services().model_settings()
        api_key = _services().model_api_key()
        if not api_key or not settings["enabled"]:
            return self.send_json(
                {
                    "error": "尚未启用模型服务。请在页面的模型配置中填写 OpenAI 兼容 API Key 并启用，或设置 LIT_AGENT_API_KEY。"
                },
                503,
            )
        try:
            result = _services().generate_abstract_brief_analysis(paper_id)
        except LookupError as exc:
            return self.send_json({"error": str(exc)}, 404)
        except Exception as exc:
            return self.send_json(
                {
                    "error": f"摘要助读失败（已自动重试）：{_services().model_error_detail(exc, api_key)}"
                },
                502,
            )
        return self.send_json(result)

    def _fulltext_ready(self, data: dict, paper_id: str = "") -> dict | None:
        if not data.get("consent"):
            raise ValueError("请先确认允许将这篇论文的全文发送到已配置的模型服务")
        settings = _services().model_settings()
        api_key = _services().model_api_key()
        if not api_key or not settings["enabled"]:
            self.send_json(
                {
                    "error": "尚未启用模型服务。请在模型配置中填写 OpenAI 兼容 API Key 并启用，或设置 LIT_AGENT_API_KEY。"
                },
                503,
            )
            return None
        if paper_id and not _services().paper_has_fulltext_source(paper_id):
            self.send_json(
                {
                    "error": "这篇论文还没有可用的本地 PDF 或已提取全文；请先添加或下载 PDF，再开始全文分析。"
                },
                400,
            )
            return None
        return settings

    def ai_reading_card(self, paper_id: str, data: dict):
        if self._fulltext_ready(data, paper_id) is None:
            return
        try:
            result = _services().run_fulltext_ai_analysis(
                paper_id,
                "reading_card",
                data.get("template", "fulltext_reading_card"),
                data.get("language", "zh-CN"),
                visual_enabled=False,
            )
        except LookupError as exc:
            return self.send_json({"error": str(exc)}, 404)
        except ValueError as exc:
            message = str(exc)
            status = 502 if "模型输出" in message or "JSON" in message else 400
            return self.send_json({"error": message}, status)
        except Exception as exc:
            api_key = _services().model_api_key()
            return self.send_json(
                {
                    "error": f"全文阅读卡分析失败：{_services().model_error_detail(exc, api_key)}"
                },
                502,
            )
        # A generated reading card is useful immediately.  Apply empty fields
        # by default while keeping ``apply:false`` available for API clients
        # that explicitly want a review-only draft.
        applied = None
        should_apply = data.get("apply", True) is not False
        if should_apply:
            with _services().DB_LOCK, _services().db_session() as db:
                row = db.execute(
                    "SELECT * FROM reading_cards WHERE paper_id=?", (paper_id,)
                ).fetchone()
                existing = _services().reading_card_dict(row, paper_id)
                merged = _services().merge_ai_reading_card(
                    existing,
                    result.get("draft", {}),
                    bool(data.get("merge_empty", True)),
                )
                applied = _services().save_reading_card(db, paper_id, merged, "ai")
                _services().record_audit(
                    db,
                    "reading_card.ai_applied",
                    "paper",
                    paper_id,
                    {
                        "artifact_id": result["artifact_id"],
                        "merge_empty": bool(data.get("merge_empty", True)),
                    },
                )
        return self.send_json(
            {**result, "applied": applied, "auto_applied": bool(should_apply)}
        )

    def ai_deep_read(self, paper_id: str, data: dict):
        if self._fulltext_ready(data, paper_id) is None:
            return
        try:
            visual_enabled = data.get("visual_enabled", True)
            if not isinstance(visual_enabled, bool):
                raise ValueError("visual_enabled 必须是布尔值")
            result = _services().run_fulltext_ai_analysis(
                paper_id,
                "deep_read",
                data.get("template", "graduate_deep_read"),
                data.get("language", "zh-CN"),
                visual_enabled=visual_enabled,
            )
        except LookupError as exc:
            return self.send_json({"error": str(exc)}, 404)
        except ValueError as exc:
            message = str(exc)
            status = 502 if "模型输出" in message or "JSON" in message else 400
            return self.send_json({"error": message}, status)
        except Exception as exc:
            api_key = _services().model_api_key()
            return self.send_json(
                {
                    "error": f"论文精读失败：{_services().model_error_detail(exc, api_key)}"
                },
                502,
            )
        return self.send_json(result)

    def apply_ai_reading_card(self, paper_id: str, data: dict):
        artifact_id = _services().clean_text(data.get("artifact_id", ""), 100)
        if not artifact_id:
            raise ValueError("缺少待应用的阅读卡分析 ID")
        with _services().DB_LOCK, _services().db_session() as db:
            row = db.execute(
                "SELECT answer FROM ai_artifacts WHERE id=? AND paper_id=? AND artifact_type='reading_card_draft'",
                (artifact_id, paper_id),
            ).fetchone()
            if not row:
                return self.send_json(
                    {"error": "阅读卡分析不存在或不属于这篇论文"}, 404
                )
            analysis = _services().parse_model_json(row["answer"])
            if isinstance(analysis.get("analysis"), dict):
                analysis = analysis["analysis"]
            draft = analysis.get("reading_card", analysis)
            current_row = db.execute(
                "SELECT * FROM reading_cards WHERE paper_id=?", (paper_id,)
            ).fetchone()
            current = _services().reading_card_dict(current_row, paper_id)
            merged = _services().merge_ai_reading_card(
                current, draft, bool(data.get("merge_empty", True))
            )
            card = _services().save_reading_card(db, paper_id, merged, "ai")
            _services().record_audit(
                db,
                "reading_card.ai_applied",
                "paper",
                paper_id,
                {
                    "artifact_id": artifact_id,
                    "merge_empty": bool(data.get("merge_empty", True)),
                },
            )
        return self.send_json(
            {"applied": True, "artifact_id": artifact_id, "reading_card": card}
        )


__all__ = ["Handler"]
