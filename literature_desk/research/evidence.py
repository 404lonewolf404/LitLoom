"""Paper identity, evidence, and context preparation for agents."""

from __future__ import annotations

import sqlite3

from ..services import app_services as _services


def _agent_step_output(db: sqlite3.Connection, run_id: str, step_key: str) -> dict:
    row = db.execute(
        "SELECT output_json FROM agent_steps WHERE run_id=? AND step_key=?",
        (run_id, step_key),
    ).fetchone()
    return _services()._json_value(row[0] if row else "", {})


def _bounded_agent_payload(value, max_chars: int = 350000):
    try:
        serialized = _services().json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return {"value": _services().clean_text(value, max_chars)}
    if len(serialized) <= max_chars:
        return value
    return {
        "truncated": True,
        "preview": serialized[:max_chars],
        "original_chars": len(serialized),
    }


def _agent_reduced_paper(item: dict) -> dict:
    reduced = {
        key: item.get(key, "")
        for key in (
            "id",
            "title",
            "authors",
            "year",
            "venue",
            "doi",
            "arxiv_id",
            "openalex_id",
            "external_id",
            "url",
            "pdf_url",
            "source",
            "status",
            "topic",
            "relevance_score",
            "relevance_label",
            "relevance_reasons",
            "discovery_score",
            "discovery_reasons",
            "seen_before",
            "open_access",
            "citation_count",
            "agent_relevance_class",
            "agent_relevance_score",
            "quality_filter_reason",
        )
    }
    reduced["abstract"] = _services().clean_text(item.get("abstract", ""), 1600)
    return reduced


def _agent_resolve_paper_identities(papers: list[dict], limit: int = 100) -> dict:
    """Resolve candidate identities without importing or mutating papers.

    Discovery providers frequently return the same work with different
    transient IDs.  This step deliberately only reads the local library and
    annotates the snapshot, leaving the later version merger responsible for
    grouping editions.  Keeping the status explicit lets a long-running
    follow-up distinguish an exact match from a title-only or unresolved hit.
    """
    items = [item for item in papers if isinstance(item, dict) and item.get("title")][
        :limit
    ]
    resolved = []
    counts = {"doi": 0, "arxiv": 0, "title": 0, "unresolved": 0}
    with _services().DB_LOCK, _services().db_session() as db:
        for raw in items:
            item = _services()._agent_reduced_paper(raw)
            doi = _services().norm_doi(item.get("doi", ""))
            arxiv_id = _services().norm_arxiv(item.get("arxiv_id", ""))
            title = _services().title_key(item.get("title", ""))
            row = None
            status = "unresolved"
            if doi:
                row = db.execute(
                    "SELECT * FROM papers WHERE doi=? LIMIT 1", (doi,)
                ).fetchone()
                if row:
                    status = "doi"
            if not row and arxiv_id:
                row = db.execute(
                    "SELECT * FROM papers WHERE arxiv_id=? LIMIT 1", (arxiv_id,)
                ).fetchone()
                if row:
                    status = "arxiv"
            if not row and title:
                row = db.execute(
                    "SELECT * FROM papers WHERE title_key=? AND (year=? OR ? IS NULL) LIMIT 1",
                    (title, item.get("year"), item.get("year")),
                ).fetchone()
                if row:
                    status = "title"
            counts[status] += 1
            if row:
                local = dict(row)
                item["local_paper_id"] = local.get("id", "")
                item["resolved_title"] = local.get("title", "")
                item["has_local_pdf"] = bool(
                    db.execute(
                        "SELECT 1 FROM attachments WHERE paper_id=? LIMIT 1",
                        (local["id"],),
                    ).fetchone()
                )
                item["local_document_pages"] = int(
                    db.execute(
                        "SELECT COUNT(*) FROM document_pages WHERE paper_id=?",
                        (local["id"],),
                    ).fetchone()[0]
                )
                # Preserve the strongest stable identity available locally.
                item["resolved_identity"] = _services().candidate_identity(local)
            else:
                item["local_paper_id"] = ""
                item["resolved_identity"] = _services().candidate_identity(item)
            item["identity_status"] = status
            resolved.append(item)
    return {
        "papers": resolved,
        "resolved_count": sum(
            value for key, value in counts.items() if key != "unresolved"
        ),
        "unresolved_count": counts["unresolved"],
        "match_counts": counts,
        "status": (
            "resolved"
            if resolved and counts["unresolved"] == 0
            else ("partial" if resolved else "empty")
        ),
    }


def _agent_focus_papers(candidates: list[dict], limit: int = 3) -> list[dict]:
    """Choose a small, explainable focus set instead of always taking index 0.

    Full-text work is expensive. Prefer a local PDF, then relevance and
    novelty, while preserving the original ranking as a final tie-breaker.
    Every selected record carries a human-readable reason for the UI/audit.
    """
    usable = [
        item for item in candidates if isinstance(item, dict) and item.get("title")
    ]
    ranked = sorted(
        enumerate(usable),
        key=lambda pair: (
            bool(pair[1].get("has_local_pdf")),
            bool(pair[1].get("local_document_pages")),
            float(
                pair[1].get("relevance_score") or pair[1].get("discovery_score") or 0
            ),
            int(pair[1].get("year") or 0),
            -pair[0],
        ),
        reverse=True,
    )
    selected = []
    for rank, (_index, item) in enumerate(ranked[: max(1, min(int(limit), 8))], 1):
        copy = dict(item)
        reasons = []
        if copy.get("has_local_pdf"):
            reasons.append("已有本地 PDF，可直接做页级核验")
        if copy.get("local_document_pages"):
            reasons.append("已有页级文本，能复用历史提取结果")
        if copy.get("relevance_label"):
            reasons.append(str(copy["relevance_label"]))
        if not reasons:
            reasons.append("相关性与时间排序靠前")
        copy["focus_rank"] = rank
        copy["focus_reason"] = "；".join(reasons)
        selected.append(copy)
    return selected


def _infer_page_section(page_text: str) -> str:
    """Infer a conservative section label from page headings when available."""
    normalized = (
        _services()
        .re.sub(r"\s+", " ", _services().clean_text(page_text, 20000))
        .strip()
    )
    if not normalized:
        return "PDF 页面文本"
    patterns = (
        (r"\babstract\b|摘要", "Abstract"),
        (r"\bintroduction\b|引言|绪论", "Introduction"),
        (r"\brelated work|background\b|相关工作|背景", "Background / Related Work"),
        (r"\bmethod(?:s|ology)?\b|方法|模型架构", "Methods"),
        (r"\bexperiment(?:s)?\b|实验|评测", "Experiments"),
        (r"\bresult(?:s)?\b|结果", "Results"),
        (r"\bdiscussion\b|讨论", "Discussion"),
        (r"\bconclusion(?:s)?\b|结论", "Conclusion"),
        (r"\blimitation(?:s)?\b|局限|不足", "Limitations"),
    )
    first_lines = " ".join(
        _services().re.split(r"\n+", _services().clean_text(page_text, 6000))[:8]
    )
    for pattern, label in patterns:
        if _services().re.search(pattern, first_lines, _services().re.I):
            return label
    return "PDF 页面文本"


def _agent_paper_key(item: dict) -> str:
    """Return the stable identity used by a research goal's long memory."""
    return _services().candidate_identity(item)


def _unique_agent_papers(*groups: list[dict] | None, limit: int = 100) -> list[dict]:
    """Merge candidate snapshots while preserving one record per stable work.

    A supplementary search intentionally gets a fresh candidate ID.  The
    identity must therefore come from DOI/arXiv/title rather than the
    transient ID, and the first record keeps its ranking while later records
    only fill missing metadata.
    """
    merged: dict[str, dict] = {}
    for group in groups:
        if not isinstance(group, list):
            continue
        for raw in group:
            if not isinstance(raw, dict) or not raw.get("title"):
                continue
            item = dict(raw)
            key = (
                _services()._agent_paper_key(item)
                or f"id:{_services().clean_text(item.get('id', ''), 200)}"
            )
            if key not in merged:
                merged[key] = item
                if len(merged) >= limit:
                    return list(merged.values())
                continue
            existing = merged[key]
            for field, value in item.items():
                if existing.get(field) in (None, "", [], {}):
                    existing[field] = value
    return list(merged.values())


def _unique_agent_claims(*groups: list[dict] | None, limit: int = 200) -> list[dict]:
    """Merge extracted and follow-up claims without losing their provenance."""
    merged: dict[str, dict] = {}
    for group in groups:
        if not isinstance(group, list):
            continue
        for raw in group:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            claim_id = _services().clean_text(
                item.get("id") or item.get("claim_id"), 120
            )
            fingerprint = "|".join(
                _services().clean_text(item.get(field, ""), 1200).casefold()
                for field in ("paper_id", "claim_type", "claim_text", "page")
            )
            key = claim_id or (
                "text:"
                + _services().hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
            )
            if key not in merged:
                merged[key] = item
                if len(merged) >= limit:
                    return list(merged.values())
    return list(merged.values())


def _record_goal_paper_history(
    db: sqlite3.Connection, goal_id: str, run_id: str, papers: list[dict]
) -> dict:
    """Persist discovery history without importing candidates into the library.

    A goal remembers that a work was seen, while the normal candidate inbox
    still requires an explicit user import.  DOI/arXiv identities are stable
    across providers and title/year is used only as a conservative fallback.
    """
    now = _services().now_iso()
    new_items, changed_items, repeated_items = [], [], []
    for raw in papers[:100]:
        if not isinstance(raw, dict) or not _services().clean_text(
            raw.get("title", ""), 1000
        ):
            continue
        key = _services()._agent_paper_key(raw)
        doi = _services().norm_doi(raw.get("doi", ""))
        arxiv_id = _services().norm_arxiv(raw.get("arxiv_id", ""))
        version_key = (
            ("doi:" + doi)
            if doi
            else (
                ("arxiv:" + arxiv_id)
                if arxiv_id
                else "title:" + _services().title_key(raw.get("title", ""))
            )
        )
        local_id = _services().clean_text(
            raw.get("local_paper_id") or raw.get("id", ""), 200
        )
        existing = db.execute(
            "SELECT * FROM research_goal_papers WHERE goal_id=? AND paper_key=?",
            (goal_id, key),
        ).fetchone()
        metadata = {
            field: raw.get(field)
            for field in (
                "year",
                "venue",
                "source",
                "pdf_url",
                "url",
                "openalex_id",
                "external_id",
                "relevance_label",
                "discovery_score",
            )
            if raw.get(field) not in (None, "")
        }
        if existing:
            changed = any(
                _services().clean_text(existing[field], 2000)
                != _services().clean_text(raw.get(field, ""), 2000)
                for field in ("title", "doi", "arxiv_id")
            )
            db.execute(
                """UPDATE research_goal_papers SET local_paper_id=CASE WHEN ?<>'' THEN ? ELSE local_paper_id END,title=CASE WHEN ?<>'' THEN ? ELSE title END,doi=CASE WHEN ?<>'' THEN ? ELSE doi END,arxiv_id=CASE WHEN ?<>'' THEN ? ELSE arxiv_id END,version_key=?,last_seen_at=?,seen_count=seen_count+1,last_run_id=?,change_summary=?,metadata_json=? WHERE goal_id=? AND paper_key=?""",
                (
                    local_id,
                    local_id,
                    _services().clean_text(raw.get("title", ""), 1000),
                    _services().clean_text(raw.get("title", ""), 1000),
                    doi,
                    doi,
                    arxiv_id,
                    arxiv_id,
                    version_key,
                    now,
                    run_id,
                    "元数据发生变化" if changed else "",
                    _services().json.dumps(metadata, ensure_ascii=False),
                    goal_id,
                    key,
                ),
            )
            (changed_items if changed else repeated_items).append(
                {
                    "paper_key": key,
                    "title": raw.get("title", ""),
                    "change_summary": "元数据发生变化" if changed else "",
                }
            )
        else:
            db.execute(
                """INSERT INTO research_goal_papers(goal_id,paper_key,local_paper_id,title,doi,arxiv_id,version_key,first_seen_at,last_seen_at,seen_count,first_run_id,last_run_id,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    goal_id,
                    key,
                    local_id,
                    _services().clean_text(raw.get("title", ""), 1000),
                    doi,
                    arxiv_id,
                    version_key,
                    now,
                    now,
                    1,
                    run_id,
                    run_id,
                    _services().json.dumps(metadata, ensure_ascii=False),
                ),
            )
            new_items.append(
                {
                    "paper_key": key,
                    "title": raw.get("title", ""),
                    "change_summary": "首次发现",
                }
            )
    return {
        "new": new_items,
        "changed": changed_items,
        "repeated": repeated_items,
        "new_count": len(new_items),
        "changed_count": len(changed_items),
        "repeated_count": len(repeated_items),
    }


def _agent_fulltext_reading_card(paper: dict, payload: dict) -> dict:
    """Run the real page-aware multimodal reading-card pipeline for the Agent.

    Agent drafts are review-only: the persistent AI job writes an artifact but
    does not overwrite the user's reading card. The same progress, cancellation
    and restart semantics used by the UI therefore apply to Agent runs too.
    """
    paper_id = _services().clean_text(
        paper.get("local_paper_id") or paper.get("id", ""), 200
    )
    if not paper_id and not paper.get("title"):
        return {
            "status": "empty",
            "paper_id": "",
            "reading_card": {},
            "source_scope": "none",
            "requires_fulltext": True,
            "warning": "当前运行没有可供阅读的候选论文",
        }
    if not paper_id or paper_id.startswith("candidate:"):
        return {
            "status": "unavailable",
            "paper_id": paper_id,
            "reading_card": {},
            "source_scope": "metadata_or_abstract",
            "requires_fulltext": True,
            "warning": "候选论文尚未导入本地文献库或没有本地 PDF；Agent 不会静默下载论文",
        }
    if payload.get("confirm") is not True:
        return {
            "status": "unavailable",
            "paper_id": paper_id,
            "reading_card": {},
            "source_scope": "consent_required",
            "requires_fulltext": True,
            "warning": "发送全文和页面图像给模型前需要明确确认；请以 consent_external=true 批准本次 Agent 运行",
        }
    with _services().DB_LOCK, _services().db_session() as db:
        exists = db.execute("SELECT 1 FROM papers WHERE id=?", (paper_id,)).fetchone()
        attachment_count = (
            int(
                db.execute(
                    "SELECT COUNT(*) FROM attachments WHERE paper_id=?", (paper_id,)
                ).fetchone()[0]
            )
            if exists
            else 0
        )
    if not exists:
        return {
            "status": "unavailable",
            "paper_id": paper_id,
            "reading_card": {},
            "source_scope": "metadata_or_abstract",
            "requires_fulltext": True,
            "warning": "论文不在本地文献库，无法进行可追溯全文分析",
        }
    if not attachment_count:
        return {
            "status": "unavailable",
            "paper_id": paper_id,
            "reading_card": {},
            "source_scope": "metadata_or_abstract",
            "requires_fulltext": True,
            "warning": "论文没有本地 PDF；请先下载或添加附件后再运行全文精读",
        }
    settings = _services().model_settings()
    if (
        not settings.get("enabled")
        or not settings.get("api_key_set")
        or not settings.get("model")
        or not (settings.get("api_url") or settings.get("base_url"))
    ):
        return {
            "status": "unavailable",
            "paper_id": paper_id,
            "reading_card": {},
            "source_scope": "local_fulltext_pending_model",
            "requires_fulltext": True,
            "warning": "全文阅读需要已启用的 OpenAI 兼容模型配置（API 地址、模型名和密钥）",
        }
    options = {
        "template": _services().clean_text(
            payload.get("template", "fulltext_reading_card"), 120
        ),
        "language": _services().clean_text(payload.get("language", "zh-CN"), 30),
        "apply": False,
        "merge_empty": True,
    }
    submitted = _services().submit_ai_job(paper_id, "reading_card", options)
    job = submitted.get("job") if isinstance(submitted, dict) else None
    job_id = _services().clean_text((job or {}).get("id", ""), 100)
    if not job_id:
        raise RuntimeError("全文阅读任务提交失败：没有返回任务 ID")
    try:
        wait_seconds = min(max(int(payload.get("wait_seconds", 840)), 30), 900)
    except (TypeError, ValueError):
        wait_seconds = 840
    deadline = _services().time.monotonic() + wait_seconds
    last_progress = None
    while _services().time.monotonic() < deadline:
        snapshot = _services().get_ai_job(job_id, include_result=True)
        if not snapshot:
            raise RuntimeError("全文阅读任务已丢失，无法恢复结果")
        status = snapshot.get("status")
        if status == "success":
            result = (
                snapshot.get("result")
                if isinstance(snapshot.get("result"), dict)
                else {}
            )
            card = (
                result.get("reading_card")
                if isinstance(result.get("reading_card"), dict)
                else {}
            )
            if not card:
                raise RuntimeError("全文阅读任务完成但没有返回结构化阅读卡")
            return {
                "status": "completed",
                "paper_id": paper_id,
                "reading_card": card,
                "analysis": result,
                "artifact_id": snapshot.get("artifact_id", ""),
                "job_id": job_id,
                "meta": snapshot.get("meta", {}),
                "source_scope": "fulltext_multimodal",
                "requires_fulltext": False,
                "deduplicated": bool(submitted.get("deduplicated")),
            }
        if status in {"failed", "cancelled", "interrupted"}:
            detail = _services().clean_text(
                snapshot.get("error") or snapshot.get("message") or status, 1000
            )
            raise RuntimeError(f"全文阅读任务{status}：{detail}")
        progress = (
            snapshot.get("progress_current"),
            snapshot.get("progress_total"),
            snapshot.get("stage"),
            snapshot.get("message"),
        )
        if payload.get("step_id") and progress != last_progress:
            last_progress = progress
            _services()._agent_update_step(
                payload.get("run_id", ""),
                payload["step_id"],
                progress_current=int(snapshot.get("progress_current") or 0),
                progress_total=int(snapshot.get("progress_total") or 0),
                message=_services().clean_text(
                    snapshot.get("message") or snapshot.get("stage") or "等待全文分析",
                    300,
                ),
            )
        _services().time.sleep(0.75)
    raise TimeoutError(
        f"全文阅读任务在 {wait_seconds} 秒内未完成；任务 {job_id} 仍可在 AI 任务列表中继续观察"
    )


def _paper_analysis_overview(
    papers: list[dict],
    claims: list[dict] | None = None,
    verification: dict | None = None,
    critique: dict | None = None,
) -> dict:
    """Build a bounded, source-grounded professional analysis for each paper.

    This is deliberately local and deterministic.  It does not ask a model to
    invent a paper-level narrative: every positive statement is either a
    verbatim page/abstract excerpt or an existing Claim, while missing fields
    become explicit questions and actions.  A later model pass may summarize
    this object, but it cannot upgrade an ``abstract_only`` result to a
    full-text conclusion.
    """
    claims = claims if isinstance(claims, list) else []
    verification = verification if isinstance(verification, dict) else {}
    critique = critique if isinstance(critique, dict) else {}
    verification_by_id = {
        str(item.get("claim_id")): item
        for item in (verification.get("results") or [])
        if isinstance(item, dict)
    }
    required_sections = (
        "Abstract",
        "Introduction",
        "Methods",
        "Experiments",
        "Results",
        "Discussion",
        "Limitations",
        "Conclusion",
    )
    reproducibility_terms = {
        "数据/数据集": ["dataset", "benchmark", "corpus", "data set", "数据集", "基准"],
        "代码/实现": ["code", "github", "repository", "implementation", "开源", "代码"],
        "训练设置": [
            "hyperparameter",
            "learning rate",
            "batch size",
            "epoch",
            "训练设置",
            "超参数",
        ],
        "基线与对照": ["baseline", "compared with", "ablation", "基线", "消融", "对照"],
        "资源与成本": [
            "gpu",
            "compute",
            "inference time",
            "memory",
            "计算资源",
            "推理时间",
        ],
        "统计与不确定性": [
            "confidence interval",
            "standard deviation",
            "significance",
            "置信区间",
            "标准差",
            "显著性",
        ],
    }
    field_terms = {
        "problem": [
            "objective",
            "research question",
            "we address",
            "motivat",
            "研究问题",
            "目标",
            "挑战",
        ],
        "method": [
            "method",
            "methodology",
            "approach",
            "architecture",
            "algorithm",
            "模型",
            "方法",
            "网络",
        ],
        "assumptions": ["assume", "assumption", "under the assumption", "假设", "前提"],
        "data_and_experiments": [
            "dataset",
            "benchmark",
            "experiment",
            "evaluation",
            "数据集",
            "实验",
            "评测",
        ],
        "results": [
            "result",
            "accuracy",
            "performance",
            "outperform",
            "improv",
            "结果",
            "性能",
            "提升",
        ],
        "limitations": [
            "limitation",
            "failure",
            "future work",
            "threat",
            "局限",
            "不足",
            "失败",
            "未来工作",
        ],
    }

    def _clean_quote(value: object, limit: int = 420) -> str:
        text = (
            _services().re.sub(r"\s+", " ", _services().clean_text(value, 5000)).strip()
        )
        return text[:limit]

    def _claim_text(claim: dict) -> str:
        return (
            _services()
            .re.sub(
                r"^(原文摘录|摘要摘录|比较矩阵[^：:]*摘录)\s*[：:]\s*",
                "",
                _services().clean_text(claim.get("claim_text", ""), 1000),
            )
            .strip()
        )

    def _claim_refs(items: list[dict], paper_id: str, limit: int = 3) -> list[dict]:
        refs = []
        for claim in items:
            if not isinstance(claim, dict):
                continue
            page = claim.get("page")
            if page in (None, "", 0):
                continue
            quote = _claim_text(claim)
            if not quote:
                continue
            refs.append(
                {
                    "paper_id": paper_id,
                    "page": int(page),
                    "section": _services().clean_text(
                        claim.get("section") or "PDF 页面文本", 80
                    ),
                    "quote": quote[:420],
                    "evidence_id": _services().clean_text(
                        claim.get("evidence_id", ""), 100
                    ),
                    "source_sha256": _services().clean_text(
                        claim.get("source_sha256", ""), 120
                    ),
                    "evidence_type": "claim_page",
                }
            )
            if len(refs) >= limit:
                break
        return refs

    overviews = []
    for raw in [item for item in (papers or []) if isinstance(item, dict)][:8]:
        paper = dict(raw)
        paper_id = _services().clean_text(
            paper.get("local_paper_id") or paper.get("id", ""), 200
        )
        aliases = {
            value
            for value in (
                paper_id,
                _services().clean_text(paper.get("id", ""), 200),
                _services().clean_text(paper.get("local_paper_id", ""), 200),
            )
            if value
        }
        paper_claims = [
            claim
            for claim in claims
            if isinstance(claim, dict)
            and aliases.intersection(
                {
                    _services().clean_text(claim.get("paper_id", ""), 200),
                    _services().clean_text(claim.get("local_paper_id", ""), 200),
                }
            )
        ]
        by_type = {}
        for claim in paper_claims:
            by_type.setdefault(
                _services().clean_text(claim.get("claim_type") or "other", 80), []
            ).append(claim)
        page_rows = []
        with _services().DB_LOCK, _services().db_session() as db:
            if paper_id and not paper_id.startswith("candidate:"):
                page_rows = [
                    dict(row)
                    for row in db.execute(
                        "SELECT page_number,text,text_sha256,quality FROM document_pages WHERE paper_id=? AND quality<>'empty' AND trim(text)<>'' ORDER BY page_number",
                        (paper_id,),
                    ).fetchall()
                ]
        page_sections = {}
        for row in page_rows:
            section = _services()._infer_page_section(row.get("text", ""))
            page_sections.setdefault(section, []).append(row)
        normalized_pages = [
            (
                row,
                _services()
                .re.sub(
                    r"\s+", " ", _services().clean_text(row.get("text", ""), 500000)
                )
                .strip(),
            )
            for row in page_rows
        ]

        def _page_refs(terms: list[str], limit: int = 3) -> list[dict]:
            refs = []
            lowered = [term.casefold() for term in terms]
            for row, text in normalized_pages:
                haystack = text.casefold()
                position = next(
                    (
                        haystack.find(term)
                        for term in lowered
                        if term and haystack.find(term) >= 0
                    ),
                    -1,
                )
                if position < 0:
                    continue
                start = max(0, position - 150)
                quote = text[start : min(len(text), position + 360)]
                refs.append(
                    {
                        "paper_id": paper_id,
                        "page": int(row.get("page_number") or 0),
                        "section": _services()._infer_page_section(row.get("text", "")),
                        "quote": _clean_quote(quote),
                        "source_sha256": _services().clean_text(
                            row.get("text_sha256", ""), 120
                        ),
                        "evidence_type": "page_text",
                    }
                )
                if len(refs) >= limit:
                    break
            return refs

        def _abstract_ref() -> list[dict]:
            abstract = _clean_quote(paper.get("abstract", ""), 620)
            if not abstract:
                return []
            return [
                {
                    "paper_id": paper_id
                    or _services().clean_text(paper.get("id", ""), 200),
                    "page": None,
                    "section": "abstract",
                    "quote": abstract,
                    "source_sha256": _services()
                    .hashlib.sha256(abstract.encode("utf-8"))
                    .hexdigest(),
                    "evidence_type": "abstract_text",
                }
            ]

        def _field(name: str, claim_types: tuple[str, ...], terms: list[str]) -> dict:
            selected = []
            for kind in claim_types:
                selected.extend(by_type.get(kind, []))
            refs = _claim_refs(selected, paper_id, 3) if page_rows else []
            if not refs:
                refs = _page_refs(terms, 3) if page_rows else _abstract_ref()
            text_values = [_claim_text(item) for item in selected if _claim_text(item)]
            if not text_values and refs:
                text_values = [ref["quote"] for ref in refs[:2]]
            if not text_values and name == "problem" and paper.get("abstract"):
                text_values = [_clean_quote(paper.get("abstract"), 620)]
            scope = (
                "fulltext_page"
                if any(ref.get("page") is not None for ref in refs)
                else ("abstract_only" if refs else "unverified")
            )
            return {
                "text": (
                    "；".join(dict.fromkeys(text_values))[:1200]
                    if text_values
                    else "未在已保存来源中定位"
                ),
                "evidence_refs": refs,
                "status": "verified" if scope == "fulltext_page" else scope,
            }

        analysis = {
            "problem": _field(
                "problem", ("research_question",), field_terms["problem"]
            ),
            "method": _field("method", ("method",), field_terms["method"]),
            "assumptions": _field(
                "assumptions", ("assumption",), field_terms["assumptions"]
            ),
            "data_and_experiments": _field(
                "data_and_experiments",
                ("dataset", "result"),
                field_terms["data_and_experiments"],
            ),
            "results": _field("results", ("result",), field_terms["results"]),
            "limitations": _field(
                "limitations", ("limitation",), field_terms["limitations"]
            ),
        }
        section_coverage = {
            section: {
                "pages": [
                    int(row.get("page_number") or 0)
                    for row in page_sections.get(section, [])
                ],
                "page_count": len(page_sections.get(section, [])),
            }
            for section in required_sections
        }
        detected_sections = [
            section
            for section, value in section_coverage.items()
            if value["page_count"]
        ]
        missing_sections = [
            section for section in required_sections if section not in detected_sections
        ]
        reproducibility = []
        for label, terms in reproducibility_terms.items():
            refs = _page_refs(terms, 2) if page_rows else []
            reproducibility.append(
                {
                    "item": label,
                    "status": "evidence_found" if refs else "not_located",
                    "evidence_refs": refs,
                    "next_step": (
                        "核对原文中的具体设置、版本或数值"
                        if refs
                        else "回到全文搜索该项，并在必要时查看附录/代码仓库"
                    ),
                }
            )
        visual_refs = (
            _page_refs(["figure", "table", "equation", "图", "表", "公式"], 6)
            if page_rows
            else []
        )
        risks = []
        for item in critique.get("critiques") or []:
            if isinstance(item, dict) and (
                not item.get("claim_id")
                or any(
                    str(claim.get("id")) == str(item.get("claim_id"))
                    for claim in paper_claims
                )
            ):
                risks.append(
                    {
                        "category": _services().clean_text(
                            item.get("category") or "review", 80
                        ),
                        "severity": _services().clean_text(
                            item.get("severity") or "medium", 20
                        ),
                        "text": _services().clean_text(item.get("critique", ""), 700),
                        "claim_id": _services().clean_text(
                            item.get("claim_id", ""), 100
                        ),
                    }
                )
        open_questions = []
        for section in missing_sections:
            open_questions.append(
                f"{section} 未在保存的页面文本中可靠定位，需要人工确认是否存在、是否提取失败或是否被合并到其他章节"
            )
        for item in reproducibility:
            if item["status"] == "not_located":
                open_questions.append(f"尚未定位{item['item']}证据")
        if not page_rows:
            open_questions.append(
                "当前只有摘要/元数据，不能判断公式、图表、实验设置、局限和复现条件"
            )
        next_actions = []
        if page_rows:
            if missing_sections:
                next_actions.append(
                    {
                        "priority": "high",
                        "action": "核对缺失章节或重新提取/ OCR 页面",
                        "why": "避免把未读取到的章节误判为论文没有该内容",
                        "sections": missing_sections[:4],
                    }
                )
            if visual_refs:
                next_actions.append(
                    {
                        "priority": "high",
                        "action": "逐页查看图表、公式和消融结果",
                        "why": "文本提取不能可靠表达视觉布局、公式和图例关系",
                        "pages": [ref["page"] for ref in visual_refs[:6]],
                    }
                )
            next_actions.append(
                {
                    "priority": "medium",
                    "action": "按复现清单核对数据、代码、训练设置和基线公平性",
                    "why": "形成可执行复现判断，而不是只复述摘要",
                }
            )
        else:
            next_actions.append(
                {
                    "priority": "high",
                    "action": "获取并提取 PDF 全文后再做页级精读",
                    "why": "当前证据范围仅为摘要，不能完成专业全文判断",
                }
            )
        evidence_count = sum(
            len(value.get("evidence_refs") or []) for value in analysis.values()
        ) + sum(len(item.get("evidence_refs") or []) for item in reproducibility)
        source_scope = (
            "fulltext_page"
            if page_rows
            else ("abstract_only" if paper.get("abstract") else "no_source")
        )
        overviews.append(
            {
                "paper_id": paper_id
                or _services().clean_text(paper.get("id", ""), 200),
                "title": _services().clean_text(paper.get("title", ""), 500),
                "year": paper.get("year"),
                "venue": _services().clean_text(paper.get("venue", ""), 200),
                "source_scope": source_scope,
                "analysis": analysis,
                "section_coverage": section_coverage,
                "evidence_coverage": {
                    "page_count": len(page_rows),
                    "detected_sections": detected_sections,
                    "missing_sections": missing_sections,
                    "evidence_count": evidence_count,
                    "coverage_percent": (
                        round(len(detected_sections) / len(required_sections) * 100)
                        if page_rows
                        else 0
                    ),
                },
                "reproducibility": reproducibility,
                "visual_inventory": visual_refs,
                "critical_risks": risks[:20],
                "open_questions": open_questions[:30],
                "next_actions": next_actions[:10],
                "uncertainties": (
                    ["全文页面文本已保存，但章节识别依赖页面标题，仍需人工确认章节边界"]
                    if page_rows
                    else ["没有本地页级全文证据，所有结论均限制在摘要/元数据范围"]
                )
                + (["批判项来自规则化检查，不能替代领域专家判断"] if risks else []),
            }
        )
    fulltext_count = sum(
        item.get("source_scope") == "fulltext_page" for item in overviews
    )
    abstract_only_count = sum(
        item.get("source_scope") == "abstract_only" for item in overviews
    )
    evidence_count = sum(
        int(item.get("evidence_coverage", {}).get("evidence_count") or 0)
        for item in overviews
    )
    return {
        "paper_overviews": overviews,
        "overview_count": len(overviews),
        "fulltext_count": fulltext_count,
        "abstract_only_count": abstract_only_count,
        "evidence_count": evidence_count,
        "status": "completed" if overviews else "unavailable",
        "warning": "分析总览只使用已保存的页面文本、摘要和已有 Claim；未定位内容会明确标记为缺口",
    }


__all__ = [
    "_agent_step_output",
    "_bounded_agent_payload",
    "_agent_reduced_paper",
    "_agent_resolve_paper_identities",
    "_agent_focus_papers",
    "_infer_page_section",
    "_agent_paper_key",
    "_unique_agent_papers",
    "_unique_agent_claims",
    "_record_goal_paper_history",
    "_agent_fulltext_reading_card",
    "_paper_analysis_overview",
]
