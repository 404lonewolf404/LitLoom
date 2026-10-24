from __future__ import annotations
from ..services import app_services as _services


class _HandlerWriteMixin:
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
            if path == "/api/auth/local-login":
                if not self._same_origin_local_browser_request():
                    return self.send_json(
                        {"error": "默认用户入口仅允许本机同源页面使用"}, 403
                    )
                try:
                    session = _services().create_local_auto_login_session()
                    if not session:
                        return self.send_json({"error": "本机默认用户入口未启用"}, 404)
                    session_token, csrf_token, user = session
                except (PermissionError, ValueError) as exc:
                    return self.send_json({"error": str(exc)}, 403)
                return self.send_json(
                    {"user": user},
                    headers={
                        "Set-Cookie": self._auth_cookie_values(
                            session_token, csrf_token
                        )
                    },
                )
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
            if path == "/api/agent/autonomous":
                try:
                    return self.send_json(
                        _services().autonomous_agent(self.read_json())
                    )
                except (
                    PermissionError,
                    ValueError,
                    RuntimeError,
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
                    override = probe.get("settings")
                    if isinstance(override, dict):
                        result = _services().test_model_connection(
                            deep=bool(probe.get("deep", True)),
                            settings_override=override,
                        )
                    else:
                        # Preserve the small public callable contract used by
                        # integrations that replace the probe for local tests.
                        result = _services().test_model_connection(
                            deep=bool(probe.get("deep", True))
                        )
                    return self.send_json(result)
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
            if path.startswith("/api/papers/") and path.endswith(
                "/ai-reading-segmentation"
            ):
                paper_id, data = path.split("/")[3], self.read_json()
                if data.get("consent") is not True:
                    return self.send_json(
                        {"error": "请先确认允许将论文短文本片段发送给模型判断分块"},
                        400,
                    )
                if self._fulltext_ready(data, paper_id) is None:
                    return
                result = _services().submit_ai_job(
                    paper_id, "reading_segmentation", data
                )
                return self.send_json(
                    result, 200 if result.get("deduplicated") else 202
                )
            if path.startswith("/api/papers/") and path.endswith("/paragraph-analysis"):
                paper_id, data = path.split("/")[3], self.read_json()
                if data.get("consent") is not True:
                    return self.send_json(
                        {"error": "请先确认允许将本论文上下文发送给模型"}, 400
                    )
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
