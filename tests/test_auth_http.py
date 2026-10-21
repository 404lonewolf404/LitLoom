import json
import os
import os
import tempfile
import threading
import unittest
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

os.environ["LITLOOM_DATABASE_URL"] = ""
import app


class TeamAuthHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.previous_env = os.environ.get("LITLOOM_AUTH_MODE")
        self.previous_tenant_env = os.environ.get("LITLOOM_TENANT_MODE")
        os.environ["LITLOOM_AUTH_MODE"] = "team"
        os.environ.pop("LITLOOM_TENANT_MODE", None)
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
        owner = self._bootstrap()
        self.assertEqual(owner["role"], "owner")
        status, state = self._request(self.owner_opener, "/api/auth/status")
        self.assertEqual(status, 200)
        self.assertTrue(state["authenticated"])
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
