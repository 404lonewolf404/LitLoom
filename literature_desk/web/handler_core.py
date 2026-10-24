from __future__ import annotations
from ..services import app_services as _services


class _HandlerCoreMixin:
    def _loopback_browser_request(self) -> bool:
        """Require both a loopback peer and a loopback Host for local entry."""
        try:
            peer = _services().ipaddress.ip_address(self.client_address[0])
            host = _services().urlparse("//" + self.headers.get("Host", "")).hostname
            host_is_local = host == "localhost" or bool(
                host and _services().ipaddress.ip_address(host).is_loopback
            )
            return peer.is_loopback and host_is_local
        except (ValueError, TypeError):
            return False

    def _same_origin_local_browser_request(self) -> bool:
        if not self._loopback_browser_request():
            return False
        origin_header = self.headers.get("Origin", "")
        origin = _services().urlparse(origin_header)
        host = self.headers.get("Host", "")
        fetch_site = self.headers.get("Sec-Fetch-Site", "")
        return bool(
            origin.scheme in {"http", "https"}
            and origin.netloc.casefold() == host.casefold()
            and (not fetch_site or fetch_site.casefold() == "same-origin")
        )

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
                "error_code": value.get("error_code")
                or _services().status_error_code(status),
                "retryable": bool(
                    value.get("retryable", status in {408, 429, 502, 503, 504})
                ),
                "request_id": value.get("request_id")
                or getattr(self, "request_id", ""),
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
            and path
            in {
                "/api/auth/login",
                "/api/auth/bootstrap",
                "/api/auth/local-login",
            }
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
