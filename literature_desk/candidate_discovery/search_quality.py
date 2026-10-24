"""Search run quality and state transitions."""

from __future__ import annotations

from ..services import app_services as _services

def _apply_search_quality_and_history(fetch_state) -> None:
    if fetch_state.model_web_source_errors and fetch_state.model_web_result.get("results"):
        fetch_state.source_error_text = "模型检索部分来源不可用：" + "；".join(
            fetch_state.model_web_source_errors[:4]
        )
        if fetch_state.source_error_text not in fetch_state.errors:
            fetch_state.errors.append(fetch_state.source_error_text)
    fetch_state.attempted_source_count = len(fetch_state.fetch_sources) or len(fetch_state.sources)
    fetch_state.status = (
        "partial"
        if fetch_state.errors and len(fetch_state.errors) < fetch_state.attempted_source_count
        else ("failed" if fetch_state.errors else "success")
    )
    fetch_state.error_text = "\n".join(fetch_state.errors)
    with _services().DB_LOCK, _services().db_session() as fetch_state.db:
        # Keep the durable run in ``running`` while ranking, candidate
        # persistence, and (when requested) AI-screening setup are still being
        # committed below. Publishing ``success`` here would let a browser
        # reload observe a half-written snapshot and a stale triage state.
        fetch_state.db.execute(
            "UPDATE sync_jobs SET status='running',stage=?,last_error=?,updated_at=? WHERE id=?",
            ("ranking", fetch_state.error_text[:1000], _services().now_iso(), fetch_state.run_id),
        )
    with _services().DB_LOCK, _services().db_session() as fetch_state.db:
        fetch_state.results = []
        fetch_state.known_rows = fetch_state.db.execute(
            "SELECT title,abstract,doi,arxiv_id,year FROM papers WHERE id NOT IN ({})".format(
                ",".join("?" for _ in fetch_state.paper_ids) if fetch_state.paper_ids else "''"
            ),
            tuple(fetch_state.paper_ids),
        ).fetchall()
        # Indexed, durable memory covers every prior run for this research
        # intent. Unlike scanning a capped history window, it keeps working
        # after years of repeated searches.
        fetch_state.identity_keys_by_paper = {
            fetch_state.paper_id: _services().candidate_identity_variants(
                fetch_state.search_observations.get(fetch_state.paper_id)
                or fetch_state.candidate_records.get(fetch_state.paper_id)
                or {}
            )
            for fetch_state.paper_id in fetch_state.paper_ids
        }
        fetch_state.current_identity_keys = sorted(
            {fetch_state.key for fetch_state.keys in fetch_state.identity_keys_by_paper.values() for fetch_state.key in fetch_state.keys}
        )
        fetch_state.seen_identities = set()
        for fetch_state.start in range(0, len(fetch_state.current_identity_keys), 700):
            fetch_state.chunk = fetch_state.current_identity_keys[fetch_state.start : fetch_state.start + 700]
            if fetch_state.chunk:
                fetch_state.seen_identities.update(
                    fetch_state.row[0]
                    for fetch_state.row in fetch_state.db.execute(
                        "SELECT identity_key FROM discovery_search_memory WHERE intent_key=? AND identity_key IN ({})".format(
                            ",".join("?" for _ in fetch_state.chunk)
                        ),
                        (fetch_state.intent_key, *fetch_state.chunk),
                    )
                )
        fetch_state.query_seen_ids = set()
        if fetch_state.query_id:
            fetch_state.query_seen_rows = fetch_state.db.execute(
                "SELECT qm.paper_id,p.title,p.abstract,p.doi,p.arxiv_id,p.year,qm.user_decision FROM query_matches qm LEFT JOIN papers p ON p.id=qm.paper_id WHERE qm.query_id=?",
                (fetch_state.query_id,),
            ).fetchall()
            fetch_state.query_seen_ids = {fetch_state.row[0] for fetch_state.row in fetch_state.query_seen_rows}
            for fetch_state.row in fetch_state.query_seen_rows:
                fetch_state.seen_identities.update(
                    _services().candidate_identity_variants(
                        {
                            "title": fetch_state.row[1] or "",
                            "abstract": fetch_state.row[2] or "",
                            "doi": fetch_state.row[3] or "",
                            "arxiv_id": fetch_state.row[4] or "",
                            "year": fetch_state.row[5] or "",
                        }
                    )
                )
        fetch_state.feedback_rows = (
            fetch_state.db.execute(
                """SELECT p.title,p.abstract,qm.user_decision FROM query_matches qm
            JOIN papers p ON p.id=qm.paper_id WHERE qm.query_id=? AND qm.user_decision IN ('relevant','irrelevant','ignored')""",
                (fetch_state.query_id,),
            ).fetchall()
            if fetch_state.query_id
            else []
        )
        fetch_state.feedback_rows += fetch_state.db.execute(
            """SELECT p.title,p.abstract,lf.decision AS user_decision FROM learning_feedback lf
            JOIN papers p ON p.id=lf.paper_id WHERE lf.scope='global' AND lf.decision IN ('relevant','irrelevant')"""
        ).fetchall()
        fetch_state.feedback_by_candidate_key = _services()._discovery_feedback_by_key(
            fetch_state.db, fetch_state.intent_key
        )
        fetch_state.unique_candidate_feedback = {}
        for fetch_state.feedback in fetch_state.feedback_by_candidate_key.values():
            if fetch_state.feedback.get("decision") not in _services().DISCOVERY_FEEDBACK_DECISIONS:
                continue
            fetch_state.marker = (
                _services().title_key(fetch_state.feedback.get("candidate_title", "")),
                fetch_state.feedback.get("decision"),
            )
            fetch_state.unique_candidate_feedback[fetch_state.marker] = fetch_state.feedback
        fetch_state.feedback_rows += [
            {
                "title": fetch_state.feedback.get("candidate_title", ""),
                "abstract": "",
                "user_decision": fetch_state.feedback.get("decision", ""),
            }
            for fetch_state.feedback in fetch_state.unique_candidate_feedback.values()
        ]
        if not fetch_state.persist_results and fetch_state.candidate_records:
            fetch_state.db.executemany(
                "INSERT INTO discovery_candidates(id,run_id,candidate_key,record_json,created_at) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET run_id=excluded.run_id,candidate_key=excluded.candidate_key,record_json=excluded.record_json,created_at=excluded.created_at",
                [
                    (
                        fetch_state.candidate_id,
                        fetch_state.run_id,
                        _services().candidate_identity(fetch_state.record),
                        _services().json.dumps(fetch_state.record, ensure_ascii=False),
                        _services().now_iso(),
                    )
                    for fetch_state.candidate_id, fetch_state.record in fetch_state.candidate_records.items()
                ],
            )
        for fetch_state.paper_id in fetch_state.paper_ids:
            fetch_state.row = (
                fetch_state.db.execute("SELECT * FROM papers WHERE id=?", (fetch_state.paper_id,)).fetchone()
                if fetch_state.persist_results
                else None
            )
            if fetch_state.row or (not fetch_state.persist_results and fetch_state.paper_id in fetch_state.candidate_records):
                fetch_state.paper = (
                    _services().paper_dict(fetch_state.row)
                    if fetch_state.row
                    else dict(fetch_state.candidate_records[fetch_state.paper_id])
                )
                fetch_state.paper["sources"] = (
                    [
                        dict(fetch_state.source_row)
                        for fetch_state.source_row in fetch_state.db.execute(
                            "SELECT source,external_id,fetched_at FROM source_records WHERE paper_id=? ORDER BY fetched_at DESC",
                            (fetch_state.paper_id,),
                        )
                    ]
                    if fetch_state.row
                    else list(
                        fetch_state.candidate_records[fetch_state.paper_id].get("sources")
                        or [
                            {
                                "source": fetch_state.candidate_records[fetch_state.paper_id].get("source", ""),
                                "external_id": fetch_state.candidate_records[fetch_state.paper_id].get(
                                    "external_id", ""
                                ),
                                "fetched_at": fetch_state.started,
                            }
                        ]
                    )
                )
                if not fetch_state.row:
                    fetch_state.paper.pop("tags_json", None)
                    fetch_state.paper["tags"] = []
                fetch_state.paper["discovery_paths"] = fetch_state.paper_paths.get(fetch_state.paper_id, [])
                fetch_state.signals = fetch_state.paper_signals.get(
                    fetch_state.paper_id,
                    {"citation_count": 0, "open_access": False, "lanes": set()},
                )
                fetch_state.paper["citation_count"] = int(fetch_state.signals.get("citation_count") or 0)
                fetch_state.paper["open_access"] = bool(fetch_state.signals.get("open_access"))
                fetch_state.paper["discovery_lanes"] = sorted(fetch_state.signals.get("lanes") or [])
                fetch_state.rank_signal = fetch_state.paper_rank_signals.get(
                    fetch_state.paper_id, {"rrf": [], "sources": set(), "variants": set()}
                )
                fetch_state.paper["rank_fusion_score"] = _services().rank_fusion_score(fetch_state.rank_signal)
                fetch_state.paper["rank_sources"] = sorted(fetch_state.rank_signal.get("sources") or [])
                fetch_state.paper["rank_paths"] = len(fetch_state.rank_signal.get("rrf") or [])
                fetch_state.results.append(fetch_state.paper)
    fetch_state.query_terms = [
        fetch_state.term
        for fetch_state.term in _services().re.findall(r"[a-z0-9]{3,}", fetch_state.q.lower())
        if fetch_state.term
        not in {
            "and",
            "or",
            "the",
            "for",
            "with",
            "from",
            "via",
            "using",
            "based",
            "model",
            "models",
        }
    ]
    fetch_state.current_year = _services().datetime.now(_services().timezone.utc).year
    for fetch_state.paper in fetch_state.results:
        fetch_state.title = _services().title_key(fetch_state.paper["title"])
        fetch_state.abstract = (fetch_state.paper.get("abstract") or "").lower()
        fetch_state.title_hits = [fetch_state.term for fetch_state.term in fetch_state.query_terms if fetch_state.term in fetch_state.title]
        fetch_state.abstract_hits = [fetch_state.term for fetch_state.term in fetch_state.query_terms if fetch_state.term in fetch_state.abstract]
        fetch_state.paper["match_reason"] = (
            "标题命中：" + "、".join(dict.fromkeys(fetch_state.title_hits))
            if fetch_state.title_hits
            else (
                "摘要命中：" + "、".join(dict.fromkeys(fetch_state.abstract_hits))
                if fetch_state.abstract_hits
                else "来源检索结果；请人工核对相关性"
            )
        )
        fetch_state.triage = _services().score_relevance(fetch_state.paper, fetch_state.q, fetch_state.known_rows, fetch_state.feedback_rows)
        fetch_state.paper["relevance_score"] = fetch_state.triage["score"]
        fetch_state.paper["relevance_label"] = fetch_state.triage["label"]
        fetch_state.paper["relevance_reasons"] = fetch_state.triage["reasons"]
        fetch_state.model_label = (
            _services().clean_text(fetch_state.paper.get("model_relevance_label"), 40).casefold()
        )
        if fetch_state.source == "model_web" and fetch_state.model_label == "direct":
            fetch_state.paper["relevance_score"] = min(100, fetch_state.paper["relevance_score"] + 14)
            fetch_state.paper["relevance_reasons"].append("模型检索判定为直接相关")
        elif fetch_state.source == "model_web" and fetch_state.model_label == "adjacent":
            fetch_state.paper["relevance_score"] = min(100, fetch_state.paper["relevance_score"] + 6)
            fetch_state.paper["relevance_reasons"].append("模型检索判定为邻近方向")
        if fetch_state.source == "model_web" and fetch_state.model_label in {"direct", "adjacent"}:
            fetch_state.paper["relevance_label"] = _services().relevance_label(
                fetch_state.paper["relevance_score"]
            )
        # Keep retrieval ranking separate from the later AI candidate triage.
        # ``relevance_label`` remains the legacy field used by ranking and
        # exports, while these explicit fields make the public contract
        # unambiguous after a reload (a paper may be high in retrieval rank
        # while its abstract-only AI screen is still uncertain).
        fetch_state.paper["retrieval_relevance_score"] = fetch_state.paper.get("relevance_score")
        fetch_state.paper["retrieval_relevance_label"] = fetch_state.paper.get("relevance_label")
        fetch_state.paper["retrieval_relevance_reasons"] = list(
            fetch_state.paper.get("relevance_reasons") or []
        )
        fetch_state.paper["retrieval_relevance_source"] = "rank_fusion_local"
        # Saved topics remember their own history. A paper already in another
        # topic is still a first discovery for this topic; ad-hoc searches use
        # the global library state as the only available memory.
        fetch_state.paper["candidate_identity"] = _services().candidate_identity(fetch_state.paper)
        fetch_state.paper["candidate_key"] = fetch_state.paper["candidate_identity"]
        fetch_state.paper["intent_key"] = fetch_state.intent_key
        fetch_state.paper_identity_keys = _services().candidate_identity_variants(
            {**fetch_state.paper, **(fetch_state.search_observations.get(fetch_state.paper["id"]) or {})}
        )
        fetch_state.prior_feedback = next(
            (
                fetch_state.feedback_by_candidate_key[fetch_state.key]
                for fetch_state.key in fetch_state.paper_identity_keys
                if fetch_state.key in fetch_state.feedback_by_candidate_key
            ),
            None,
        )
        if fetch_state.prior_feedback and fetch_state.prior_feedback.get("decision") == "new":
            # ``new`` is an audit-safe tombstone for a revoked decision, not
            # a recommendation label to expose or reuse in the next run.
            fetch_state.prior_feedback = None
        if fetch_state.prior_feedback:
            # This is a user memory, not an AI verdict.  Keep it separate so
            # the UI can explain why a repeated candidate is demoted without
            # pretending that the current model decided it again.
            fetch_state.paper["prior_feedback"] = dict(fetch_state.prior_feedback)
            fetch_state.paper["review_decision"] = fetch_state.prior_feedback["decision"]
            fetch_state.paper["user_decision"] = fetch_state.prior_feedback["decision"]
        else:
            fetch_state.paper["prior_feedback"] = None
            fetch_state.paper["review_decision"] = fetch_state.paper.get("user_decision", "new") or "new"
        fetch_state.paper["seen_before"] = fetch_state.paper["id"] in fetch_state.query_seen_ids or bool(
            fetch_state.paper_identity_keys & fetch_state.seen_identities
        )
        fetch_state.paper["seen_status"] = (
            STATUS_LABELS.get(fetch_state.paper.get("status"), fetch_state.paper.get("status", ""))
            if "STATUS_LABELS" in globals()
            else fetch_state.paper.get("status", "")
        )
        fetch_state.paper["query_id"] = fetch_state.query_id
        fetch_state.age = max(0, fetch_state.current_year - int(fetch_state.paper["year"])) if fetch_state.paper.get("year") else 12
        fetch_state.paper["recency_score"] = max(0, 100 - min(100, fetch_state.age * 10))
        fetch_state.paper["novelty_score"] = 100 if not fetch_state.paper["seen_before"] else 35
        fetch_state.paper["source_count"] = len(
            {fetch_state.source.get("source") for fetch_state.source in fetch_state.paper.get("sources", [])}
        )
        fetch_state.citation_count = int(fetch_state.paper.get("citation_count") or 0)
        fetch_state.paper["impact_score"] = (
            min(100, round((fetch_state.citation_count**0.5) * 10)) if fetch_state.citation_count else 0
        )
        fetch_state.paper["source_quality_score"] = min(
            100,
            fetch_state.paper["source_count"] * 25
            + (10 if fetch_state.paper.get("doi") else 0)
            + (8 if fetch_state.paper.get("open_access") else 0),
        )
        fetch_state.paper["coverage_score"] = min(
            100,
            round(
                (
                    len(
                        _services().expanded_relevance_terms(fetch_state.q)
                        & (
                            _services().relevance_tokens(fetch_state.paper.get("title", ""))
                            | _services().relevance_tokens(fetch_state.paper.get("abstract", ""))
                        )
                    )
                    / max(1, len(_services().expanded_relevance_terms(fetch_state.q)))
                )
                * 100
            ),
        )
        fetch_state.paper["library_distance_score"] = max(
            (
                _services().text_overlap(
                    _services().paper_text(fetch_state.paper),
                    f"{fetch_state.row['title'] or ''} {fetch_state.row['abstract'] or ''}",
                )["score"]
                for fetch_state.row in fetch_state.known_rows
            ),
            default=0,
        )
        fetch_state.paper["discovery_class"] = (
            "高相关"
            if fetch_state.paper["relevance_score"] >= 60
            else "邻近方向" if fetch_state.paper["relevance_score"] >= 35 else "跨领域线索"
        )
        fetch_state.paper["discovery_score"] = round(
            fetch_state.paper["relevance_score"] * 0.52
            + fetch_state.paper["rank_fusion_score"] * 0.18
            + fetch_state.paper["recency_score"] * 0.12
            + fetch_state.paper["source_quality_score"] * 0.08
            + fetch_state.paper["novelty_score"] * 0.10
        )
        fetch_state.paper["discovery_reasons"] = list(fetch_state.paper.get("relevance_reasons", []))
        if fetch_state.paper.get("discovery_paths"):
            fetch_state.path_count = len(fetch_state.paper["discovery_paths"])
            if fetch_state.path_count > 1:
                fetch_state.paper["discovery_reasons"].append(f"多路检索确认：{fetch_state.path_count} 条路径")
            fetch_state.aliases = [
                fetch_state.path["query"]
                for fetch_state.path in fetch_state.paper["discovery_paths"]
                if fetch_state.path.get("variant_id") != "primary"
            ]
            if fetch_state.aliases:
                fetch_state.paper["discovery_reasons"].append(
                    "概念变体命中：" + "、".join(dict.fromkeys(fetch_state.aliases))[:120]
                )
        if fetch_state.paper["source_count"] > 1:
            fetch_state.paper["discovery_reasons"].append(
                f"多源交叉确认：{fetch_state.paper['source_count']} 个来源"
            )
        if fetch_state.paper.get("rank_fusion_score"):
            fetch_state.paper["discovery_reasons"].append(
                f"融合排序信号：{fetch_state.paper['rank_fusion_score']}（{fetch_state.paper.get('rank_paths', 0)} 条来源/变体路径）"
            )
        if fetch_state.paper.get("discovery_class") == "跨领域线索":
            fetch_state.paper["discovery_reasons"].append(
                "与原问题词面较远，作为跨领域探索线索保留"
            )
        elif fetch_state.paper.get("library_distance_score", 0) >= 48:
            fetch_state.paper["discovery_reasons"].append("与馆藏研究方向高度相连")
        if fetch_state.paper["recency_score"] >= 80:
            fetch_state.paper["discovery_reasons"].append("较新发表记录")
        if fetch_state.paper["seen_before"]:
            fetch_state.paper["discovery_reasons"].append("已在此前轮次出现，降低重复优先级")
        else:
            fetch_state.paper["discovery_reasons"].append("本主题首次发现")
        if fetch_state.paper["citation_count"]:
            fetch_state.paper["discovery_reasons"].append(
                f"来源记录引用量：{fetch_state.paper['citation_count']}"
            )
        if fetch_state.paper.get("open_access"):
            fetch_state.paper["discovery_reasons"].append("存在开放获取线索")
        fetch_state.lanes = fetch_state.paper.get("discovery_lanes") or []
        if fetch_state.lanes:
            fetch_state.paper["discovery_reasons"].append("召回层：" + "、".join(fetch_state.lanes))
    fetch_state.model_web_quality_filtered = 0
    fetch_state.model_web_quality_refill = 0
    if fetch_state.search_options.get("search_backend") == "model_web":
        fetch_state.results, fetch_state.model_web_quality_filtered = _services().filter_model_web_quality(
            fetch_state.results, fetch_state.q, fetch_state.search_options
        )
        if fetch_state.model_web_quality_filtered:
            fetch_state.quality_message = (
                f"已隐藏 {fetch_state.model_web_quality_filtered} 篇未同时命中研究对象和核心任务的候选，"
                "避免为了避免重复而返回无关论文。"
            )
            if isinstance(fetch_state.model_web_result, dict):
                fetch_state.gaps = fetch_state.model_web_result.setdefault("gaps", [])
                if fetch_state.quality_message not in fetch_state.gaps:
                    fetch_state.gaps.append(fetch_state.quality_message)
            if not fetch_state.results and isinstance(fetch_state.model_web_result, dict):
                fetch_state.no_result_message = "本轮没有达到相关性门槛的新候选；可使用下一轮建议检索式继续扩展，或切换到探索模式查看跨领域线索。"
                fetch_state.gaps = fetch_state.model_web_result.setdefault("gaps", [])
                if fetch_state.no_result_message not in fetch_state.gaps:
                    fetch_state.gaps.append(fetch_state.no_result_message)
        # The first history refill happens before ranking so the model has a
        # large enough pool to choose from.  A strict object/task quality gate
        # can then remove several of those rows.  Refill once more after that
        # gate, otherwise a repeated search can still show only a few rows
        # even though the same research intent has durable, previously
        # accepted candidates available.  This lane never invents metadata,
        # skips explicit negative feedback, and remains disabled only for a
        # genuinely strict fresh-only API request.
        try:
            fetch_state.quality_target = min(100, max(1, int(fetch_state.limit)))
        except (TypeError, ValueError):
            fetch_state.quality_target = 25
        if (
            len(fetch_state.results) < fetch_state.quality_target
            and (
                not fetch_state.search_options.get("suppress_seen")
                or fetch_state.search_options.get("fill_repeats")
            )
            and fetch_state.intent_key
        ):
            fetch_state.quality_history = _services()._model_search_history_fill(
                fetch_state.intent_key,
                fetch_state.q,
                fetch_state.results,
                fetch_state.quality_target,
                fetch_state.search_options,
            )
            if fetch_state.quality_history:
                for fetch_state.item in fetch_state.quality_history:
                    fetch_state.item["selection_backfilled"] = True
                    fetch_state.item.setdefault("relevance_label", "uncertain")
                    fetch_state.item.setdefault(
                        "relevance_reason",
                        "相关性门槛后从同一研究意图的历史候选补位；建议人工复核",
                    )
                (
                    fetch_state.accepted_history,
                    fetch_state.rejected_history,
                ) = _services().filter_model_web_quality(
                    fetch_state.quality_history,
                    fetch_state.q,
                    fetch_state.search_options,
                )
                # These rows are appended after the main ranking pass, so
                # hydrate the same durable discovery fields that ordinary
                # candidates receive.  Without this, a history refill could
                # count toward the page while appearing as a new paper or
                # sorting unpredictably after a reload.
                for fetch_state.item in fetch_state.accepted_history:
                    fetch_state.item["source"] = fetch_state.item.get("source") or "model_web"
                    fetch_state.item["query_id"] = fetch_state.query_id
                    fetch_state.item["intent_key"] = fetch_state.intent_key
                    fetch_state.item["candidate_identity"] = _services().candidate_identity(fetch_state.item)
                    fetch_state.item["candidate_key"] = fetch_state.item["candidate_identity"]
                    fetch_state.item["seen_before"] = True
                    fetch_state.item["seen_status"] = (
                        STATUS_LABELS.get(fetch_state.item.get("status"), fetch_state.item.get("status", ""))
                        if "STATUS_LABELS" in globals()
                        else fetch_state.item.get("status", "")
                    )
                    fetch_state.item["review_decision"] = (
                        fetch_state.item.get("review_decision")
                        or fetch_state.item.get("user_decision")
                        or "new"
                    )
                    fetch_state.item["user_decision"] = fetch_state.item.get("user_decision") or "new"
                    fetch_state.item["novelty_score"] = 35
                    try:
                        fetch_state.paper_year = (
                            int(fetch_state.item.get("year"))
                            if fetch_state.item.get("year")
                            else fetch_state.current_year - 12
                        )
                    except (TypeError, ValueError):
                        fetch_state.paper_year = fetch_state.current_year - 12
                    fetch_state.age = max(0, fetch_state.current_year - fetch_state.paper_year)
                    fetch_state.item["recency_score"] = max(0, 100 - min(100, fetch_state.age * 10))
                    fetch_state.item["citation_count"] = max(
                        0, int(fetch_state.item.get("citation_count") or 0)
                    )
                    fetch_state.item["open_access"] = bool(fetch_state.item.get("open_access"))
                    fetch_state.item["source_count"] = (
                        len(
                            {
                                fetch_state.row.get("source")
                                for fetch_state.row in (fetch_state.item.get("provider_sources") or [])
                                if isinstance(fetch_state.row, dict) and fetch_state.row.get("source")
                            }
                        )
                        or 1
                    )
                    fetch_state.item["impact_score"] = (
                        min(100, round((fetch_state.item["citation_count"] ** 0.5) * 10))
                        if fetch_state.item["citation_count"]
                        else 0
                    )
                    fetch_state.item["source_quality_score"] = min(
                        100,
                        fetch_state.item["source_count"] * 25
                        + (10 if fetch_state.item.get("doi") else 0)
                        + (8 if fetch_state.item.get("open_access") else 0),
                    )
                    fetch_state.relevance = _services().score_relevance(
                        fetch_state.item, fetch_state.q, fetch_state.known_rows, fetch_state.feedback_rows
                    )
                    fetch_state.item["relevance_score"] = fetch_state.relevance["score"]
                    fetch_state.item["relevance_label"] = fetch_state.relevance["label"]
                    fetch_state.item["relevance_reasons"] = list(fetch_state.relevance["reasons"])
                    fetch_state.expanded_terms = _services().expanded_relevance_terms(fetch_state.q)
                    fetch_state.item["coverage_score"] = min(
                        100,
                        round(
                            (
                                len(
                                    fetch_state.expanded_terms
                                    & (
                                        _services().relevance_tokens(
                                            fetch_state.item.get("title", "")
                                        )
                                        | _services().relevance_tokens(
                                            fetch_state.item.get("abstract", "")
                                        )
                                    )
                                )
                                / max(1, len(fetch_state.expanded_terms))
                            )
                            * 100
                        ),
                    )
                    fetch_state.item["library_distance_score"] = max(
                        (
                            _services().text_overlap(
                                _services().paper_text(fetch_state.item),
                                f"{fetch_state.row['title'] or ''} {fetch_state.row['abstract'] or ''}",
                            )["score"]
                            for fetch_state.row in fetch_state.known_rows
                        ),
                        default=0,
                    )
                    fetch_state.item["discovery_class"] = (
                        "高相关"
                        if fetch_state.item["relevance_score"] >= 60
                        else (
                            "邻近方向"
                            if fetch_state.item["relevance_score"] >= 35
                            else "跨领域线索"
                        )
                    )
                    fetch_state.item["discovery_score"] = round(
                        fetch_state.item["relevance_score"] * 0.52
                        + fetch_state.item["recency_score"] * 0.12
                        + fetch_state.item["source_quality_score"] * 0.08
                        + fetch_state.item["novelty_score"] * 0.10
                    )
                    fetch_state.item["discovery_paths"] = fetch_state.item.get("search_paths") or []
                    fetch_state.item["discovery_lanes"] = ["history"]
                    fetch_state.item["rank_fusion_score"] = 0
                    fetch_state.item["rank_sources"] = ["history"]
                    fetch_state.item["rank_paths"] = 1
                    fetch_state.item["match_reason"] = "同一研究意图的历史候选补位"
                    fetch_state.item["discovery_reasons"] = list(fetch_state.item["relevance_reasons"])
                    fetch_state.item["discovery_reasons"].append("已在此前轮次出现，降低重复优先级")
                    fetch_state.item["sources"] = (
                        fetch_state.item.get("sources")
                        or fetch_state.item.get("provider_sources")
                        or [
                            {
                                "source": "history",
                                "external_id": fetch_state.item.get("external_id", ""),
                                "fetched_at": fetch_state.started,
                            }
                        ]
                    )
                if not fetch_state.persist_results:
                    # ``accepted_history`` is appended after the first
                    # candidate staging pass. Give each refill row a fresh
                    # ID in this run so ``candidate_result_ids`` resolves
                    # after a reload instead of pointing at a row belonging
                    # to an earlier run.
                    fetch_state.persisted_history = []
                    for fetch_state.history_item in fetch_state.accepted_history:
                        fetch_state.history_item = dict(fetch_state.history_item)
                        fetch_state.history_id = "candidate:" + str(_services().uuid.uuid4())
                        fetch_state.history_item["id"] = fetch_state.history_id
                        fetch_state.history_item["candidate_key"] = _services().candidate_identity(
                            fetch_state.history_item
                        )
                        fetch_state.history_item["intent_key"] = fetch_state.intent_key
                        fetch_state.history_item["raw"] = _services().compact_search_raw(
                            fetch_state.history_item.get("raw")
                            if isinstance(fetch_state.history_item.get("raw"), dict)
                            else fetch_state.history_item
                        )
                        fetch_state.history_item["sources"] = (
                            fetch_state.history_item.get("sources")
                            or fetch_state.history_item.get("provider_sources")
                            or [
                                {
                                    "source": "history",
                                    "external_id": fetch_state.history_item.get("external_id", ""),
                                    "fetched_at": fetch_state.started,
                                }
                            ]
                        )
                        fetch_state.late_candidate_records[fetch_state.history_id] = fetch_state.history_item
                        fetch_state.persisted_history.append(fetch_state.history_item)
                    fetch_state.accepted_history = fetch_state.persisted_history
                fetch_state.results.extend(fetch_state.accepted_history)
                fetch_state.model_web_quality_filtered += fetch_state.rejected_history
                fetch_state.model_web_quality_refill = len(fetch_state.accepted_history)
                if fetch_state.accepted_history and isinstance(fetch_state.model_web_result, dict):
                    fetch_state.model_web_result["quality_history_fill_count"] = len(
                        fetch_state.accepted_history
                    )
                    fetch_state.gaps = fetch_state.model_web_result.setdefault("gaps", [])
                    fetch_state.message = (
                        f"相关性门槛后已从同一研究意图的历史候选补充 {len(fetch_state.accepted_history)} 篇，"
                        "这些论文已标记为历史补位并需要人工复核。"
                    )
                    if fetch_state.message not in fetch_state.gaps:
                        fetch_state.gaps.append(fetch_state.message)
    # A connector can fail before the normal history-refill lane runs (for
    # example, a provider returns HTTP 429 while a repeated topic is being
    # searched). Do not let that transient source failure turn a previously
    # useful topic into an empty page. Reuse only candidates from this exact
    # intent, keep the source error visible, and mark every reused row as a
    # history fill so the user can distinguish it from a fresh hit. Strict
    # fresh-only mode deliberately skips this fallback.
    try:
        fetch_state.connector_history_target = min(100, max(1, int(fetch_state.limit)))
    except (TypeError, ValueError):
        fetch_state.connector_history_target = 25
    if (
        fetch_state.errors
        and len(fetch_state.results) < fetch_state.connector_history_target
        and fetch_state.prior_memory_count > 0
        and fetch_state.intent_key
        and (
            not fetch_state.search_options.get("suppress_seen")
            or fetch_state.search_options.get("fill_repeats")
        )
        and fetch_state.search_options.get("search_backend") != "model_web"
    ):
        fetch_state.connector_history = _services()._model_search_history_fill(
            fetch_state.intent_key,
            fetch_state.q,
            fetch_state.results,
            fetch_state.connector_history_target,
            {**fetch_state.search_options, "search_backend": "connectors"},
        )
        fetch_state.hydrated_history = []
        for fetch_state.history_item in fetch_state.connector_history:
            fetch_state.item = dict(fetch_state.history_item)
            fetch_state.item["source"] = fetch_state.item.get("source") or "history"
            fetch_state.item["query_id"] = fetch_state.query_id
            fetch_state.item["intent_key"] = fetch_state.intent_key
            fetch_state.item["candidate_identity"] = _services().candidate_identity(fetch_state.item)
            fetch_state.item["candidate_key"] = fetch_state.item["candidate_identity"]
            fetch_state.item["seen_before"] = True
            fetch_state.item["seen_status"] = (
                STATUS_LABELS.get(fetch_state.item.get("status"), fetch_state.item.get("status", ""))
                if "STATUS_LABELS" in globals()
                else fetch_state.item.get("status", "")
            )
            fetch_state.item["review_decision"] = (
                fetch_state.item.get("review_decision") or fetch_state.item.get("user_decision") or "new"
            )
            fetch_state.item["user_decision"] = fetch_state.item.get("user_decision") or "new"
            fetch_state.item["novelty_score"] = 35
            try:
                fetch_state.paper_year = (
                    int(fetch_state.item.get("year")) if fetch_state.item.get("year") else fetch_state.current_year - 12
                )
            except (TypeError, ValueError):
                fetch_state.paper_year = fetch_state.current_year - 12
            fetch_state.item["recency_score"] = max(
                0, 100 - min(100, max(0, fetch_state.current_year - fetch_state.paper_year) * 10)
            )
            fetch_state.item["citation_count"] = max(0, int(fetch_state.item.get("citation_count") or 0))
            fetch_state.item["open_access"] = bool(fetch_state.item.get("open_access"))
            fetch_state.item["source_count"] = (
                len(
                    {
                        fetch_state.row.get("source")
                        for fetch_state.row in (fetch_state.item.get("provider_sources") or [])
                        if isinstance(fetch_state.row, dict) and fetch_state.row.get("source")
                    }
                )
                or 1
            )
            fetch_state.item["impact_score"] = (
                min(100, round((fetch_state.item["citation_count"] ** 0.5) * 10))
                if fetch_state.item["citation_count"]
                else 0
            )
            fetch_state.item["source_quality_score"] = min(
                100,
                fetch_state.item["source_count"] * 25
                + (10 if fetch_state.item.get("doi") else 0)
                + (8 if fetch_state.item.get("open_access") else 0),
            )
            fetch_state.relevance = _services().score_relevance(fetch_state.item, fetch_state.q, fetch_state.known_rows, fetch_state.feedback_rows)
            fetch_state.item["relevance_score"] = fetch_state.relevance["score"]
            fetch_state.item["relevance_label"] = fetch_state.relevance["label"]
            fetch_state.item["relevance_reasons"] = list(fetch_state.relevance["reasons"])
            fetch_state.expanded_terms = _services().expanded_relevance_terms(fetch_state.q)
            fetch_state.item["coverage_score"] = min(
                100,
                round(
                    (
                        len(
                            fetch_state.expanded_terms
                            & (
                                _services().relevance_tokens(fetch_state.item.get("title", ""))
                                | _services().relevance_tokens(fetch_state.item.get("abstract", ""))
                            )
                        )
                        / max(1, len(fetch_state.expanded_terms))
                    )
                    * 100
                ),
            )
            fetch_state.item["library_distance_score"] = max(
                (
                    _services().text_overlap(
                        _services().paper_text(fetch_state.item),
                        f"{fetch_state.row['title'] or ''} {fetch_state.row['abstract'] or ''}",
                    )["score"]
                    for fetch_state.row in fetch_state.known_rows
                ),
                default=0,
            )
            fetch_state.item["discovery_class"] = (
                "高相关"
                if fetch_state.item["relevance_score"] >= 60
                else "邻近方向" if fetch_state.item["relevance_score"] >= 35 else "跨领域线索"
            )
            fetch_state.item["discovery_score"] = round(
                fetch_state.item["relevance_score"] * 0.52
                + fetch_state.item["recency_score"] * 0.12
                + fetch_state.item["source_quality_score"] * 0.08
                + fetch_state.item["novelty_score"] * 0.10
            )
            fetch_state.item["discovery_paths"] = fetch_state.item.get("search_paths") or []
            fetch_state.item["discovery_lanes"] = ["history"]
            fetch_state.item["rank_fusion_score"] = 0
            fetch_state.item["rank_sources"] = ["history"]
            fetch_state.item["rank_paths"] = 1
            fetch_state.item["selection_backfilled"] = True
            fetch_state.item["match_reason"] = "来源暂时不可用，已从同一研究意图的历史候选补位"
            fetch_state.item["discovery_reasons"] = list(fetch_state.item["relevance_reasons"])
            fetch_state.item["discovery_reasons"].append(
                "来源暂时不可用，已从同一研究意图的历史候选补位，建议人工复核"
            )
            fetch_state.item["sources"] = (
                fetch_state.item.get("sources")
                or fetch_state.item.get("provider_sources")
                or [
                    {
                        "source": "history",
                        "external_id": fetch_state.item.get("external_id", ""),
                        "fetched_at": fetch_state.started,
                    }
                ]
            )
            fetch_state.hydrated_history.append(fetch_state.item)
        if not fetch_state.persist_results:
            fetch_state.persisted_history = []
            for fetch_state.history_item in fetch_state.hydrated_history:
                fetch_state.history_item = dict(fetch_state.history_item)
                fetch_state.history_id = "candidate:" + str(_services().uuid.uuid4())
                fetch_state.history_item["id"] = fetch_state.history_id
                fetch_state.history_item["candidate_key"] = _services().candidate_identity(
                    fetch_state.history_item
                )
                fetch_state.history_item["intent_key"] = fetch_state.intent_key
                fetch_state.history_item["raw"] = _services().compact_search_raw(
                    fetch_state.history_item.get("raw")
                    if isinstance(fetch_state.history_item.get("raw"), dict)
                    else fetch_state.history_item
                )
                fetch_state.history_item["sources"] = fetch_state.history_item.get("sources") or [
                    {
                        "source": "history",
                        "external_id": fetch_state.history_item.get("external_id", ""),
                        "fetched_at": fetch_state.started,
                    }
                ]
                fetch_state.late_candidate_records[fetch_state.history_id] = fetch_state.history_item
                fetch_state.persisted_history.append(fetch_state.history_item)
            fetch_state.hydrated_history = fetch_state.persisted_history
        fetch_state.results.extend(fetch_state.hydrated_history)
        if fetch_state.hydrated_history:
            for fetch_state.detail in fetch_state.details:
                if fetch_state.detail.get("status") == "error":
                    fetch_state.detail.setdefault("history_fill_count", 0)
                    fetch_state.detail["history_fill_count"] += len(fetch_state.hydrated_history)
            fetch_state.plan["connector_history_fill"] = len(fetch_state.hydrated_history)

