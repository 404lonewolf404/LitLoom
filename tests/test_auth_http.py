import json
import os
import os
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

os.environ["LITLOOM_DATABASE_URL"] = ""
os.environ["LITLOOM_TESTING"] = "1"
import app


class TeamAuthHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.previous_env = os.environ.get("LITLOOM_AUTH_MODE")
        self.previous_tenant_env = os.environ.get("LITLOOM_TENANT_MODE")
        self.previous_local_auto_login = os.environ.get("LITLOOM_LOCAL_AUTO_LOGIN")
        self.previous_local_auto_login_username = os.environ.get("LITLOOM_LOCAL_AUTO_LOGIN_USERNAME")
        self.previous_local_auto_login_name = os.environ.get("LITLOOM_LOCAL_AUTO_LOGIN_DISPLAY_NAME")
        os.environ["LITLOOM_AUTH_MODE"] = "team"
        os.environ.pop("LITLOOM_TENANT_MODE", None)
        os.environ.pop("LITLOOM_LOCAL_AUTO_LOGIN", None)
        os.environ.pop("LITLOOM_LOCAL_AUTO_LOGIN_USERNAME", None)
        os.environ.pop("LITLOOM_LOCAL_AUTO_LOGIN_DISPLAY_NAME", None)
        self.previous_paths = (app.DATA_DIR, app.FILES_DIR, app.BACKUP_DIR, app.DB_PATH, app.WORKSPACE_DB_DIR)
        app.DATA_DIR = self.root / "data"
        app.FILES_DIR = app.DATA_DIR / "files"
        app.BACKUP_DIR = app.DATA_DIR / "backups"
        app.DB_PATH = app.DATA_DIR / "literature.sqlite3"
        app.WORKSPACE_DB_DIR = app.DATA_DIR / "workspaces"
        app.init_db()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.owner_opener = self._opener()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()
        app.DATA_DIR, app.FILES_DIR, app.BACKUP_DIR, app.DB_PATH, app.WORKSPACE_DB_DIR = self.previous_paths
        if self.previous_env is None:
            os.environ.pop("LITLOOM_AUTH_MODE", None)
        else:
            os.environ["LITLOOM_AUTH_MODE"] = self.previous_env
        if self.previous_tenant_env is None:
            os.environ.pop("LITLOOM_TENANT_MODE", None)
        else:
            os.environ["LITLOOM_TENANT_MODE"] = self.previous_tenant_env
        for key, value in (
            ("LITLOOM_LOCAL_AUTO_LOGIN", self.previous_local_auto_login),
            ("LITLOOM_LOCAL_AUTO_LOGIN_USERNAME", self.previous_local_auto_login_username),
            ("LITLOOM_LOCAL_AUTO_LOGIN_DISPLAY_NAME", self.previous_local_auto_login_name),
        ):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    @staticmethod
    def _opener():
        return build_opener(HTTPCookieProcessor(CookieJar()))

    def _request(self, opener, path, method="GET", body=None, headers=None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})

        def decode(response):
            payload = response.read()
            if not payload:
                return {}
            content_type = response.headers.get_content_type()
            if content_type == "application/json" or content_type.endswith("+json"):
                return json.loads(payload.decode("utf-8"))
            return payload.decode("utf-8", errors="replace")

        try:
            with opener.open(request, timeout=5) as response:
                return response.status, decode(response)
        except HTTPError as response:
            return response.code, decode(response)

    @staticmethod
    def _csrf(opener):
        cookie_jar = next(handler.cookiejar for handler in opener.handlers if isinstance(handler, HTTPCookieProcessor))
        return next(cookie.value for cookie in cookie_jar if cookie.name == "litloom_csrf")

    def _bootstrap(self):
        status, state = self._request(self.owner_opener, "/api/auth/status")
        self.assertEqual(status, 200)
        self.assertTrue(state["bootstrap_required"])
        token_path = Path(state["bootstrap_token_path"])
        token = token_path.read_text(encoding="utf-8").strip()
        status, result = self._request(self.owner_opener, "/api/auth/bootstrap", "POST", {
            "bootstrap_token": token,
            "username": "owner.one",
            "display_name": "Workspace Owner",
            "password": "WorkspaceOwner2026",
        })
        self.assertEqual(status, 200, result)
        self.assertFalse(token_path.exists())
        return result["user"]

    def test_anonymous_requests_are_rejected_and_owner_bootstrap_creates_session(self):
        self.assertEqual(self._request(self.owner_opener, "/")[0], 200)
        self.assertEqual(self._request(self.owner_opener, "/api/state")[0], 401)
        self.assertEqual(self._request(self.owner_opener, "/files/private.pdf")[0], 401)
        status, health = self._request(self.owner_opener, "/api/health")
        self.assertEqual(status, 200)
        self.assertNotIn("path", health["database"])
        self.assertNotIn("papers", health["database"])
        owner = self._bootstrap()
        self.assertEqual(owner["role"], "owner")
        status, state = self._request(self.owner_opener, "/api/auth/status")
        self.assertEqual(status, 200)
        self.assertTrue(state["authenticated"])
        self.assertEqual(state["workspace_isolation"], "shared_workspace")
        self.assertNotIn("csrf_hash", state["user"])
        self.assertEqual(self._request(self.owner_opener, "/api/state")[0], 200)
        cookie_jar = next(handler.cookiejar for handler in self.owner_opener.handlers if isinstance(handler, HTTPCookieProcessor))
        session = next(cookie for cookie in cookie_jar if cookie.name == "litloom_session")
        self.assertIn("HttpOnly", session._rest)

    def test_default_single_user_mode_keeps_existing_api_access(self):
        os.environ.pop("LITLOOM_AUTH_MODE", None)

        status, auth_status = self._request(self.owner_opener, "/api/auth/status")
        self.assertEqual(status, 200)
        self.assertFalse(auth_status["enabled"])

        self.assertEqual(self._request(self.owner_opener, "/api/state")[0], 200)
        status, paper = self._request(self.owner_opener, "/api/papers", "POST", {"title": "Local mode compatibility"})
        self.assertEqual(status, 201, paper)
        self.assertTrue(paper["id"])

    def test_local_default_entry_uses_researcher_session_and_preserves_owner_bootstrap(self):
        os.environ["LITLOOM_LOCAL_AUTO_LOGIN"] = "1"
        status, auth_state = self._request(self.owner_opener, "/api/auth/status")
        self.assertEqual(status, 200)
        self.assertTrue(auth_state["local_auto_login_available"])

        status, denied = self._request(
            self.owner_opener,
            "/api/auth/local-login",
            "POST",
            {},
            {"Origin": "http://attacker.example"},
        )
        self.assertEqual(status, 403, denied)

        status, entered = self._request(
            self.owner_opener,
            "/api/auth/local-login",
            "POST",
            {},
            {"Origin": self.base},
        )
        self.assertEqual(status, 200, entered)
        self.assertEqual(entered["user"]["username"], "local.default")
        self.assertEqual(entered["user"]["role"], "researcher")
        self.assertEqual(self._request(self.owner_opener, "/api/state")[0], 200)

        # Databases created before the default entry was editable may contain
        # this convenience account as viewer/reviewer. Re-entering repairs
        # only that ordinary role and must never create admin access.
        with app.control_db_session() as db:
            db.execute(
                "UPDATE workspace_memberships SET role=? WHERE user_id=(SELECT id FROM users WHERE username=?)",
                ("viewer", "local.default"),
            )
        status, repaired = self._request(
            self.owner_opener,
            "/api/auth/local-login",
            "POST",
            {},
            {"Origin": self.base},
        )
        self.assertEqual(status, 200, repaired)
        self.assertEqual(repaired["user"]["role"], "researcher")

        csrf = self._csrf(self.owner_opener)
        with patch.object(
            app,
            "test_model_connection",
            side_effect=lambda deep=False: {
                "ok": True,
                "tested": "inference" if deep else "models",
                "probe_mode": "first_token" if deep else "catalog",
            },
        ) as model_probe:
            status, quick = self._request(
                self.owner_opener,
                "/api/settings/model/test",
                "POST",
                {"deep": False},
                {"Origin": self.base, "X-LitLoom-CSRF": csrf},
            )
            self.assertEqual(status, 200, quick)
            self.assertEqual(quick["tested"], "models")
            status, first_token = self._request(
                self.owner_opener,
                "/api/settings/model/test",
                "POST",
                {"deep": True},
                {"Origin": self.base, "X-LitLoom-CSRF": csrf},
            )
            self.assertEqual(status, 200, first_token)
            self.assertEqual(first_token["tested"], "inference")
            self.assertEqual(first_token["probe_mode"], "first_token")
            self.assertEqual(
                [call.kwargs["deep"] for call in model_probe.call_args_list],
                [False, True],
            )
        status, created = self._request(
            self.owner_opener,
            "/api/papers",
            "POST",
            {"title": "Default user editable paper"},
            {"Origin": self.base, "X-LitLoom-CSRF": csrf},
        )
        self.assertEqual(status, 201, created)
        paper_id = created["id"]
        status, updated = self._request(
            self.owner_opener,
            f"/api/papers/{paper_id}",
            "PATCH",
            {"notes": "default user can save notes", "status": "to_read"},
            {"Origin": self.base, "X-LitLoom-CSRF": csrf},
        )
        self.assertEqual(status, 200, updated)
        status, card = self._request(
            self.owner_opener,
            f"/api/papers/{paper_id}/reading-card",
            "PUT",
            {"research_question": "default user can save a reading card"},
            {"Origin": self.base, "X-LitLoom-CSRF": csrf},
        )
        self.assertEqual(status, 200, card)
        self.assertEqual(card["research_question"], "default user can save a reading card")
        status, deleted = self._request(
            self.owner_opener,
            f"/api/papers/{paper_id}",
            "DELETE",
            {},
            {"Origin": self.base, "X-LitLoom-CSRF": csrf},
        )
        self.assertEqual(status, 200, deleted)
        self.assertEqual(deleted["deleted"], 1)
        self.assertEqual(
            self._request(
                self.owner_opener,
                f"/api/papers/{paper_id}",
                "PATCH",
                {"notes": "missing csrf must fail"},
            )[0],
            403,
        )

        status, forbidden = self._request(
            self.owner_opener,
            "/api/workspace/members",
            "POST",
            {
                "username": "should.not.be.admin",
                "password": "TemporaryPassword2026",
                "role": "researcher",
            },
            {"Origin": self.base, "X-LitLoom-CSRF": self._csrf(self.owner_opener)},
        )
        self.assertEqual(status, 403, forbidden)

        status, state = self._request(self.owner_opener, "/api/auth/status")
        self.assertTrue(state["bootstrap_required"])
        token_path = Path(state["bootstrap_token_path"])
        bootstrap_token = token_path.read_text(encoding="utf-8").strip()
        status, owner = self._request(self._opener(), "/api/auth/bootstrap", "POST", {
            "bootstrap_token": bootstrap_token,
            "username": "owner.one",
            "display_name": "Workspace Owner",
            "password": "WorkspaceOwner2026",
        })
        self.assertEqual(status, 200, owner)
        self.assertEqual(owner["user"]["role"], "owner")

    def test_isolated_tenant_databases_keep_workspace_papers_separate(self):
        owner = self._bootstrap()
        os.environ["LITLOOM_TENANT_MODE"] = "isolated"
        default_db = app.ensure_workspace_database(owner["workspace_id"], clone_current=True)
        owner_actor = app.resolve_auth_session(next(cookie.value for handler in self.owner_opener.handlers if isinstance(handler, HTTPCookieProcessor) for cookie in handler.cookiejar if cookie.name == "litloom_session"))
        token = app.AUTH_ACTOR.set(owner_actor)
        try:
            first_id, _ = app.upsert_paper({"title": "Workspace One Paper"}, source="manual")
            second = app.create_workspace({"name": "Second Lab"}, owner_actor)
            second_actor = dict(owner_actor, workspace_id=second["id"], workspace_name=second["name"], role="owner")
            app.ensure_workspace_database(second["id"], clone_current=False)
            app.AUTH_ACTOR.set(second_actor)
            second_id, _ = app.upsert_paper({"title": "Workspace Two Paper"}, source="manual")
            self.assertNotEqual(first_id, second_id)
            with app.db_session() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT title FROM papers").fetchone()[0], "Workspace Two Paper")
        finally:
            app.AUTH_ACTOR.reset(token)
        self.assertTrue(default_db.exists())
        self.assertTrue(app.workspace_database_path(second["id"]).exists())

    def test_workspace_list_and_switch_are_available_to_members(self):
        owner = self._bootstrap()
        os.environ["LITLOOM_TENANT_MODE"] = "isolated"
        status, created = self._request(self.owner_opener, "/api/workspaces", "POST", {"name": "Second Lab"}, {"X-LitLoom-CSRF": self._csrf(self.owner_opener)})
        self.assertEqual(status, 201, created)
        second_id = created["id"]
        status, listed = self._request(self.owner_opener, "/api/workspaces")
        self.assertEqual(status, 200)
        self.assertEqual({item["id"] for item in listed["workspaces"]}, {owner["workspace_id"], second_id})
        status, switched = self._request(self.owner_opener, "/api/auth/switch-workspace", "POST", {"workspace_id": second_id}, {"X-LitLoom-CSRF": self._csrf(self.owner_opener)})
        self.assertEqual(status, 200, switched)
        self.assertEqual(switched["user"]["workspace_id"], second_id)
        status, state = self._request(self.owner_opener, "/api/state")
        self.assertEqual(status, 200, state)
        self.assertEqual(state["total"], 0)

    def test_tenant_backup_restore_targets_active_workspace_database(self):
        owner = self._bootstrap()
        os.environ["LITLOOM_TENANT_MODE"] = "isolated"
        owner_actor = app.resolve_auth_session(next(cookie.value for handler in self.owner_opener.handlers if isinstance(handler, HTTPCookieProcessor) for cookie in handler.cookiejar if cookie.name == "litloom_session"))
        token = app.AUTH_ACTOR.set(owner_actor)
        try:
            app.ensure_workspace_database(owner["workspace_id"], clone_current=True)
            app.init_db()
            first_id, _ = app.upsert_paper({"title": "Before restore"})
            archive = app.create_backup_archive()
            app.upsert_paper({"title": "After restore"})
            handler = app.Handler
            # Exercise the same restore implementation through a lightweight
            # instance rather than replacing the control database.
            class RestoreHarness:
                def send_json(self, payload, status=200, headers=None):
                    return payload
            result = RestoreHarness()
            app.Handler.restore_backup(result, {"filename": archive["filename"], "data": __import__("base64").b64encode(Path(archive["path"]).read_bytes()).decode("ascii")})
            with app.db_session() as db:
                titles = {row[0] for row in db.execute("SELECT title FROM papers")}
            self.assertIn("Before restore", titles)
            self.assertNotIn("After restore", titles)
            control_db = app.connect(app.DB_PATH)
            try:
                self.assertEqual(control_db.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 0)
            finally:
                control_db.close()
        finally:
            app.AUTH_ACTOR.reset(token)

    def test_csrf_and_role_checks_protect_mutations_and_member_suspension_revokes_sessions(self):
        self._bootstrap()
        status, created = self._request(self.owner_opener, "/api/workspace/members", "POST", {
            "username": "read.only",
            "display_name": "Read Only",
            "password": "ReadOnlyUser2026",
            "role": "viewer",
        }, {"X-LitLoom-CSRF": self._csrf(self.owner_opener)})
        self.assertEqual(status, 201, created)
        viewer = self._opener()
        status, _ = self._request(viewer, "/api/auth/login", "POST", {"username": "read.only", "password": "ReadOnlyUser2026"})
        self.assertEqual(status, 200)
        self.assertEqual(self._request(viewer, "/api/state")[0], 200)
        self.assertEqual(self._request(viewer, "/api/audit")[0], 403)
        app.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        (app.BACKUP_DIR / "private.zip").write_bytes(b"private backup fixture")
        self.assertEqual(self._request(viewer, "/api/backups/private.zip")[0], 403)
        self.assertEqual(self._request(self.owner_opener, "/api/audit")[0], 200)
        self.assertEqual(self._request(self.owner_opener, "/api/backups/private.zip")[0], 200)
        status, error = self._request(viewer, "/api/papers", "POST", {"title": "Blocked"}, {"X-LitLoom-CSRF": self._csrf(viewer)})
        self.assertEqual(status, 403)
        self.assertIn("查看权限", error["error"])
        self.assertEqual(self._request(viewer, "/api/papers", "POST", {"title": "Missing CSRF"})[0], 403)

        member_id = created["member"]["id"]
        status, result = self._request(self.owner_opener, f"/api/workspace/members/{member_id}", "PATCH", {"active": False}, {"X-LitLoom-CSRF": self._csrf(self.owner_opener)})
        self.assertEqual(status, 200, result)
        self.assertEqual(self._request(viewer, "/api/state")[0], 401)

    def test_postgres_shared_mode_rejects_false_workspace_isolation(self):
        owner = self._bootstrap()
        with patch.object(app, "using_postgres", return_value=True):
            with self.assertRaises(PermissionError):
                app.create_workspace({"name": "Unsafe second workspace"}, {
                    "user_id": owner["user_id"],
                    "workspace_id": owner["workspace_id"],
                    "role": "owner",
                })
            with self.assertRaises(PermissionError):
                app.switch_workspace(owner["user_id"], owner["workspace_id"])

    def test_postgres_shared_mode_rejects_noncanonical_login_session_and_workspace_listing(self):
        owner = self._bootstrap()
        other_workspace_id = "00000000-0000-0000-0000-000000000222"
        with app.control_db_session() as db:
            db.execute(
                "INSERT INTO workspaces(id,name,created_at) VALUES(?,?,?)",
                (other_workspace_id, "Noncanonical workspace", "9999-01-01T00:00:00+00:00"),
            )
            db.execute(
                "INSERT INTO workspace_memberships(workspace_id,user_id,role,created_at) VALUES(?,?,?,?)",
                (other_workspace_id, owner["user_id"], "owner", app.now_iso()),
            )

        with patch.object(app, "using_postgres", return_value=True):
            with self.assertRaises(PermissionError):
                app.authenticate_user(
                    "owner.one",
                    "WorkspaceOwner2026",
                    "127.0.0.1",
                    other_workspace_id,
                )
            with app.control_db_session() as db:
                with self.assertRaises(PermissionError):
                    app.create_auth_session(db, owner["user_id"], other_workspace_id)

            workspaces = app.list_user_workspaces(owner["user_id"])
            self.assertEqual([item["id"] for item in workspaces], [owner["workspace_id"]])

            # Even a legacy or manually inserted session cannot enter a
            # noncanonical PostgreSQL shared workspace.
            with app.control_db_session() as db:
                session_token, _, _ = app.create_auth_session(
                    db, owner["user_id"], owner["workspace_id"]
                )
            with app.control_db_session() as db:
                db.execute(
                    "UPDATE auth_sessions SET workspace_id=? WHERE token_hash=?",
                    (other_workspace_id, app.token_digest(session_token)),
                )
            self.assertIsNone(app.resolve_auth_session(session_token))

    def test_password_rotation_revokes_other_sessions_and_logout_revokes_current_session(self):
        self._bootstrap()
        second_session = self._opener()
        status, _ = self._request(second_session, "/api/auth/login", "POST", {"username": "owner.one", "password": "WorkspaceOwner2026"})
        self.assertEqual(status, 200)
        status, _ = self._request(self.owner_opener, "/api/auth/password", "PATCH", {
            "current_password": "WorkspaceOwner2026",
            "new_password": "WorkspaceOwnerUpdated2026",
        }, {"X-LitLoom-CSRF": self._csrf(self.owner_opener)})
        self.assertEqual(status, 200)
        self.assertEqual(self._request(self.owner_opener, "/api/state")[0], 200)
        self.assertEqual(self._request(second_session, "/api/state")[0], 401)
        self.assertEqual(self._request(self.owner_opener, "/api/auth/logout", "POST", {}, {"X-LitLoom-CSRF": self._csrf(self.owner_opener)})[0], 200)
        self.assertEqual(self._request(self.owner_opener, "/api/state")[0], 401)

    def test_mcp_service_token_is_bound_to_member_and_replaces_only_csrf(self):
        self._bootstrap()
        previous = {key: os.environ.get(key) for key in ("LITLOOM_MCP_TOKEN", "LITLOOM_MCP_USERNAME")}
        os.environ["LITLOOM_MCP_TOKEN"] = "local-mcp-fixture-token"
        os.environ["LITLOOM_MCP_USERNAME"] = "owner.one"
        try:
            mcp = self._opener()
            headers = {"Authorization": "Bearer local-mcp-fixture-token"}
            status, state = self._request(mcp, "/api/workspace/members", headers=headers)
            self.assertEqual(status, 200)
            self.assertTrue(any(item["username"] == "owner.one" for item in state["members"]))
            self.assertEqual(self._request(mcp, "/api/state", headers=headers)[0], 200)
            status, paper = self._request(mcp, "/api/papers", "POST", {"title": "MCP authenticated paper"}, headers)
            self.assertEqual(status, 201, paper)
            bad_headers = {"Authorization": "Bearer wrong-token"}
            self.assertEqual(self._request(self._opener(), "/api/state", headers=bad_headers)[0], 401)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
