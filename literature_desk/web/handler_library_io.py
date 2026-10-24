from __future__ import annotations
from ..services import app_services as _services


class _HandlerLibraryIOMixin:
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
        model_settings = _services().model_settings()
        model_fields = (
            "provider",
            "base_url",
            "api_url",
            "website_url",
            "model",
            "fast_model",
            "analysis_model",
            "vision_model",
            "protocol",
            "temperature",
            "embedding_model",
            "embedding_api_url",
            "deepseek_search_enabled",
            "deepseek_search_api_url",
            "deepseek_fetch_api_url",
            "enabled",
            "updated_at",
        )
        model = {field: model_settings.get(field, "") for field in model_fields}
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
