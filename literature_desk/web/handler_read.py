from __future__ import annotations
from pathlib import Path
from ..services import app_services as _services


class _HandlerReadMixin:
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
                return self.send_json(
                    {
                        "enabled": False,
                        "authenticated": True,
                        "tenant_mode": "single_user",
                        "workspace_isolation": "disabled",
                    }
                )
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
                    "tenant_mode": _services()
                    .os.environ.get("LITLOOM_TENANT_MODE", "shared")
                    .strip()
                    .casefold()
                    or "shared",
                    "workspace_isolation": (
                        "isolated"
                        if _services().tenant_isolation_enabled()
                        else "shared_workspace"
                    ),
                    "bootstrap_token_path": (
                        str(_services()._bootstrap_token_path())
                        if bootstrap_required
                        else ""
                    ),
                    "local_auto_login_available": bool(
                        _services().local_auto_login_enabled()
                        and self._loopback_browser_request()
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
        pdf_page_match = _services().re.fullmatch(r"/api/papers/([^/]+)/pdf-page", path)
        if pdf_page_match:
            paper_id = _services().unquote(pdf_page_match.group(1))
            params = _services().parse_qs(parsed.query)
            try:
                page = int((params.get("page") or ["0"])[0])
            except (TypeError, ValueError):
                return self.send_json({"error": "PDF 页码无效"}, 400)
            if page < 1 or page > 5000:
                return self.send_json({"error": "PDF 页码超出允许范围"}, 400)
            attachment_id = _services().clean_text(
                (params.get("attachment_id") or [""])[0], 120
            )
            with _services().DB_LOCK, _services().db_session() as db:
                if not db.execute(
                    "SELECT 1 FROM papers WHERE id=?", (paper_id,)
                ).fetchone():
                    return self.send_json({"error": "论文不存在"}, 404)
                if attachment_id:
                    row = db.execute(
                        "SELECT id,stored_name,sha256 FROM attachments WHERE id=? AND paper_id=?",
                        (attachment_id, paper_id),
                    ).fetchone()
                else:
                    row = db.execute(
                        "SELECT id,stored_name,sha256 FROM attachments WHERE paper_id=? ORDER BY added_at LIMIT 1",
                        (paper_id,),
                    ).fetchone()
            if not row:
                return self.send_json({"error": "论文没有可用的本地 PDF"}, 404)
            try:
                rendered = _services().render_pdf_page_visual(dict(row), page, dpi=144)
            except (LookupError, ValueError) as exc:
                return self.send_json({"error": str(exc)}, 400)
            target = _services().Path(rendered.get("path", "")).resolve()
            visual_root = _services().active_visuals_dir().resolve()
            if visual_root not in target.parents:
                return self.send_error(404)
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
            if _services().team_auth_enabled() and not self.auth_user:
                # Health/readiness is intentionally public for a local process
                # manager, but it must not disclose tenant counts, filesystem
                # paths, backup timestamps, or database labels before login.
                database = health.get("database") or {}
                health["database"] = {
                    "status": database.get("status", "error"),
                    "integrity": (
                        "ok" if database.get("integrity") == "ok" else "unavailable"
                    ),
                }
                health.pop("latest_backup_at", None)
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
                    (params.get("paper_id") or [""])[0],
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
            return self.send_json(
                {
                    "configured": settings["api_key_set"],
                    "framework": _services().agent_framework_status(),
                    **settings,
                }
            )
        if path == "/api/agent/framework":
            return self.send_json(_services().agent_framework_status())
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
