"""Research tracking, reading routes, comparisons, and briefing tools."""

from __future__ import annotations

from ..services import app_services as _services

def _tool_track_follow_up(payload: dict) -> dict:
    previous = payload.get("previous") or {}
    # Histories are recorded at the merge and bounded expansion steps.  A
    # follow-up report intentionally compares stable paper identities and
    # never treats a provider's transient candidate ID as new work.
    buckets = {"new": {}, "changed": {}, "repeated": {}}
    for source_key in ("merge_versions", "expand_search", "expand_citations"):
        history = (
            previous.get(source_key, {}).get("history", {})
            if isinstance(previous.get(source_key), dict)
            else {}
        )
        for bucket in buckets:
            for item in (
                history.get(bucket, [])
                if isinstance(history.get(bucket), list)
                else []
            ):
                key = _services().clean_text(
                    item.get("paper_key"), 300
                ) or _services().title_key(item.get("title", ""))
                if key:
                    buckets[bucket][key] = item
    return {
        "artifact_type": "follow_up_digest",
        "status": "completed",
        "new_papers": list(buckets["new"].values())[:100],
        "changed_papers": list(buckets["changed"].values())[:100],
        "repeated_papers": list(buckets["repeated"].values())[:100],
        "new_count": len(buckets["new"]),
        "changed_count": len(buckets["changed"]),
        "repeated_count": len(buckets["repeated"]),
        "summary": "本次运行只报告相对研究目标历史新增或发生变化的论文；重复命中保留为审计信息。",
    }


def _tool_generate_reading_route(payload: dict) -> dict:
    papers = (
        payload.get("papers") if isinstance(payload.get("papers"), list) else []
    )
    claims = (
        payload.get("claims") if isinstance(payload.get("claims"), list) else []
    )
    claim_by_paper = {}
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        for alias in (claim.get("paper_id"), claim.get("local_paper_id")):
            if alias:
                claim_by_paper.setdefault(str(alias), []).append(claim)

    def _route_role(item: dict) -> tuple[int, str, str, str, list[str]]:
        aliases = {str(item.get("id") or ""), str(item.get("local_paper_id") or "")}
        own_claims = [
            claim for alias in aliases for claim in claim_by_paper.get(alias, [])
        ]
        types = {claim.get("claim_type") for claim in own_claims}
        year = int(item.get("year") or 0)
        recent = (
            year >= _services().datetime.now(_services().timezone.utc).year - 2
            if year
            else False
        )
        if "research_question" in types or (
            year
            and year < _services().datetime.now(_services().timezone.utc).year - 5
        ):
            return (
                0,
                "先修/奠基",
                "先建立问题定义、术语和评价口径",
                "读完后写出问题边界与至少三项比较维度",
                ["问题定义是否一致？", "论文解决的范围是什么？"],
            )
        if "method" in types or not own_claims:
            return (
                1,
                "核心方法",
                "拆解方法结构、关键假设和输入输出",
                "画出方法流程并记录需要复现的设置",
                ["方法依赖哪些假设？", "哪一步决定主要收益？"],
            )
        if recent:
            return (
                2,
                "最新进展",
                "确认近期方法变化、数据集和指标趋势",
                "更新研究地图并标记可迁移模块",
                ["改进来自方法还是数据/协议？", "与基线是否公平？"],
            )
        if "limitation" in types:
            return (
                3,
                "反例/局限",
                "寻找失败案例、适用边界和未解决问题",
                "形成批判清单和后续实验假设",
                ["结论在哪些条件下失效？", "局限是否有实验证据？"],
            )
        return (
            4,
            "复现与补充",
            "补齐相邻方法、实现和可复现信息",
            "整理复现 checklist 或补证任务",
            ["代码、数据和训练设置是否可获得？", "结果能否在同一协议下比较？"],
        )

    ordered = sorted(
        papers,
        key=lambda item: (
            _route_role(item)[0],
            -(item.get("relevance_score") or item.get("discovery_score") or 0),
            item.get("year") or 9999,
        ),
    )
    route = []
    for index, item in enumerate(ordered, 1):
        has_fulltext = bool(
            item.get("local_document_pages")
            or item.get("has_local_pdf")
            or item.get("pdf_url")
        )
        _priority, stage, reason, expected_output, questions = _route_role(item)
        route.append(
            {
                "order": index,
                "paper_id": item.get("id"),
                "title": item.get("title"),
                "stage": stage,
                "reason": reason,
                "reading_goal": reason,
                "expected_output": expected_output,
                "verification_questions": questions,
                "prerequisites": (
                    "先掌握该方向的任务定义、数据集和评价指标"
                    if stage != "先修/奠基"
                    else "无；先建立共同术语和比较口径"
                ),
                "evidence_ready": has_fulltext,
                "evidence_scope": (
                    "fulltext_available"
                    if has_fulltext
                    else ("abstract_only" if item.get("abstract") else "unverified")
                ),
            }
        )
    return {
        "route": route,
        "route_count": len(route),
        "unreadable_count": sum(not item.get("evidence_ready") for item in route),
    }


def _tool_compare_papers(payload: dict) -> dict:
    papers = (
        payload.get("papers") if isinstance(payload.get("papers"), list) else []
    )
    claims = (
        payload.get("claims") if isinstance(payload.get("claims"), list) else []
    )
    claims_by_paper = {}
    for claim in claims:
        if isinstance(claim, dict):
            # A triage candidate keeps its transient candidate ID while
            # extracted Claims use the stable local paper ID.  Index both
            # aliases so a local full text is not reported as missing.
            for alias in (claim.get("paper_id"), claim.get("local_paper_id")):
                alias = str(alias or "")
                if alias:
                    claims_by_paper.setdefault(alias, []).append(claim)
    dimensions = [
        "研究问题",
        "任务与适用边界",
        "方法",
        "关键假设",
        "数据集",
        "基线与实验",
        "评价指标可比性",
        "结果",
        "局限",
        "复现线索",
        "证据范围",
        "年份/来源",
        "开放获取",
    ]
    rows = []
    gaps = []
    for paper in papers:
        paper_id = str(paper.get("id") or "")
        aliases = [paper_id, str(paper.get("local_paper_id") or "")]
        paper_claims = []
        seen_claim_ids = set()
        for alias in aliases:
            for claim in claims_by_paper.get(alias, []):
                claim_key = claim.get("id") or claim.get("claim_id") or id(claim)
                if claim_key not in seen_claim_ids:
                    seen_claim_ids.add(claim_key)
                    paper_claims.append(claim)
        by_type = {}
        for claim in paper_claims:
            by_type.setdefault(claim.get("claim_type") or "other", []).append(
                _services().clean_text(claim.get("claim_text", ""), 700)
            )
        all_claim_text = " ".join(
            value for values in by_type.values() for value in values
        )
        page_claims = [
            claim
            for claim in paper_claims
            if claim.get("page") not in (None, "", 0)
            or claim.get("evidence_scope") == "fulltext_page"
        ]
        metric_tokens = [
            "accuracy",
            "precision",
            "recall",
            "f1",
            "auc",
            "rmse",
            "mae",
            "性能",
            "准确率",
            "指标",
        ]
        metric_signal = any(
            token in all_claim_text.casefold() for token in metric_tokens
        )
        reproducibility_signal = any(
            token in all_claim_text.casefold()
            for token in (
                "code",
                "github",
                "hyperparameter",
                "learning rate",
                "代码",
                "开源",
                "超参数",
            )
        )
        row = {
            "paper_id": paper_id,
            "title": paper.get("title"),
            "year": paper.get("year"),
            "venue": paper.get("venue"),
            "topic": paper.get("topic"),
            "relevance": paper.get("relevance_label"),
            "open_access": bool(paper.get("open_access")),
            "research_question": by_type.get("research_question", [])[:3],
            "method": by_type.get("method", [])[:3],
            "datasets": by_type.get("dataset", [])[:3],
            "results": by_type.get("result", [])[:3],
            "limitations": by_type.get("limitation", [])[:3],
            "evidence_scope": sorted(
                {
                    claim.get("evidence_scope")
                    for claim in paper_claims
                    if claim.get("evidence_scope")
                }
            ),
            "assumptions": by_type.get("assumption", [])[:3],
            "task_boundary": _services().clean_text(
                paper.get("topic")
                or (
                    by_type.get("research_question", [""])[0]
                    if by_type.get("research_question")
                    else ""
                ),
                500,
            ),
            "evidence_level": (
                "page_verified"
                if page_claims
                else (
                    "abstract_only"
                    if any(
                        claim.get("evidence_scope") == "abstract"
                        for claim in paper_claims
                    )
                    else "unverified"
                )
            ),
            "metric_comparability": (
                "有可比指标线索，仍需核对数据集、划分和统计协议"
                if metric_signal
                else "未定位统一指标线索，不能直接比较数值"
            ),
            "reproducibility_signals": (
                "已定位实现/训练设置线索"
                if reproducibility_signal
                else "未定位实现或训练设置线索"
            ),
            "applicability_boundary": "需要结合任务、数据集和局限 Claim 判断，不能仅按相关性迁移",
        }
        row["missing_fields"] = [
            field
            for field, value in (
                ("research_question", row["research_question"]),
                ("method", row["method"]),
                ("datasets", row["datasets"]),
                ("baselines", by_type.get("baseline", [])),
                ("results", row["results"]),
                ("limitations", row["limitations"]),
            )
            if not value
        ]
        if row["missing_fields"]:
            gaps.append(
                {
                    "paper_id": paper_id,
                    "title": paper.get("title"),
                    "fields": row["missing_fields"],
                    "why": "该字段没有可回溯的 Claim 或页级证据，不能用摘要印象补齐",
                    "next_step": "回到全文的对应章节、表格、附录或代码/数据说明核验",
                }
            )
        rows.append(row)
    return {
        "dimensions": dimensions,
        "rows": rows,
        "row_count": len(rows),
        "gaps": gaps[:100],
        "claim_count": len(claims),
    }


def _tool_resolve_comparison_gaps(payload: dict) -> dict:
    papers = (
        payload.get("papers") if isinstance(payload.get("papers"), list) else []
    )
    claims = (
        payload.get("claims") if isinstance(payload.get("claims"), list) else []
    )
    comparison = (
        payload.get("comparison")
        if isinstance(payload.get("comparison"), dict)
        else {}
    )
    goal = payload.get("goal") or {}
    run_id = _services().clean_text(payload.get("run_id", ""), 80)
    gaps = (
        comparison.get("gaps") if isinstance(comparison.get("gaps"), list) else []
    )
    # A gap is counted per paper/field.  This keeps the tool bounded even
    # when a broad comparison contains hundreds of candidates.
    work = []
    for gap in gaps:
        if not isinstance(gap, dict):
            continue
        paper_id = _services().clean_text(gap.get("paper_id", ""), 200)
        fields = gap.get("fields") if isinstance(gap.get("fields"), list) else []
        for field in fields:
            field = _services().clean_text(field, 80)
            if field and paper_id:
                work.append((paper_id, field))
    work = work[:30]
    paper_by_id = {}
    for paper in papers:
        if not isinstance(paper, dict):
            continue
        for alias in (paper.get("id"), paper.get("local_paper_id")):
            alias = _services().clean_text(alias, 200)
            if alias:
                paper_by_id[alias] = paper
    existing_fingerprints = set()
    for claim in claims:
        if isinstance(claim, dict):
            existing_fingerprints.add(
                "|".join(
                    _services().clean_text(claim.get(key, ""), 1200).casefold()
                    for key in ("paper_id", "claim_type", "claim_text")
                )
            )
    field_terms = {
        "method": [
            "method",
            "methodology",
            "approach",
            "architecture",
            "algorithm",
            "模型",
            "方法",
        ],
        "datasets": [
            "dataset",
            "benchmark",
            "data set",
            "corpus",
            "sample",
            "数据集",
            "基准",
            "样本",
        ],
        "results": [
            "result",
            "results",
            "accuracy",
            "performance",
            "f1",
            "auc",
            "improv",
            "outperform",
            "结果",
            "性能",
            "实验",
        ],
        "baselines": ["baseline", "comparison", "compared with", "基线", "比较"],
        "research_question": [
            "objective",
            "research question",
            "we address",
            "研究问题",
            "目标",
        ],
        "limitations": [
            "limitation",
            "failure",
            "future work",
            "局限",
            "不足",
            "失败",
            "未来工作",
        ],
    }
    attempted = 0
    resolved_count = 0
    evidence_candidates = []
    new_claims = []
    no_local = 0
    now = _services().now_iso()
    with _services().DB_LOCK, _services().db_session() as db:
        for candidate_id, field in work:
            attempted += 1
            paper = paper_by_id.get(candidate_id) or {}
            local_id = _services().clean_text(
                paper.get("local_paper_id") or candidate_id, 200
            )
            # Candidate IDs are snapshots only.  The Agent must never
            # silently download or import a PDF just to fill a matrix.
            if local_id.startswith("candidate:"):
                no_local += 1
                continue
            page_count = int(
                db.execute(
                    "SELECT COUNT(*) FROM document_pages WHERE paper_id=? AND quality='good'",
                    (local_id,),
                ).fetchone()[0]
            )
            if page_count == 0:
                no_local += 1
                continue
            terms = field_terms.get(field, [field])
            matches = []
            matched_term = ""
            for term in terms:
                matches = _services().search_document_pages(local_id, term, 3)
                if matches:
                    matched_term = term
                    break
            if not matches:
                continue
            for page in matches[:1]:
                page_number = int(page.get("page_number") or 0)
                if not page_number:
                    continue
                source_row = db.execute(
                    "SELECT text,text_sha256 FROM document_pages WHERE paper_id=? AND page_number=? ORDER BY extracted_at DESC LIMIT 1",
                    (local_id, page_number),
                ).fetchone()
                full_text = _services().clean_text(
                    source_row["text"] if source_row else "", 500000
                )
                if not full_text:
                    continue
                # `search_document_pages` intentionally returns a UI
                # snippet and may insert ellipses.  Evidence must be an
                # exact contiguous excerpt from the normalized page so a
                # later verifier can locate it again.
                normalized_text = _services().re.sub(r"\s+", " ", full_text).strip()
                needle = _services().clean_text(matched_term, 120).casefold()
                position = normalized_text.casefold().find(needle) if needle else -1
                if position >= 0:
                    quote = normalized_text[
                        max(0, position - 180) : min(
                            len(normalized_text), position + len(needle) + 420
                        )
                    ]
                else:
                    quote = normalized_text[:700]
                quote = _services().clean_text(quote, 700)
                if not quote:
                    continue
                claim_type = (
                    "dataset"
                    if field == "datasets"
                    else (
                        "result"
                        if field == "results"
                        else (
                            "limitation"
                            if field == "limitations"
                            else (
                                "research_question"
                                if field == "research_question"
                                else "method"
                            )
                        )
                    )
                )
                claim_text = f"比较矩阵 {field}：原文摘录：{quote}"
                fingerprint = "|".join(
                    _services().clean_text(value, 1200).casefold()
                    for value in (local_id, claim_type, claim_text)
                )
                if fingerprint in existing_fingerprints:
                    continue
                claim_id = str(_services().uuid.uuid4())
                evidence_id = str(_services().uuid.uuid4())
                source_hash = _services().clean_text(
                    (
                        source_row["text_sha256"]
                        if source_row
                        else page.get("text_sha256", "")
                    ),
                    120,
                )
                section = _services()._infer_page_section(full_text)
                db.execute(
                    "INSERT INTO research_claims(id,goal_id,run_id,paper_id,claim_text,claim_type,status,confidence,evidence_scope,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        claim_id,
                        _services().clean_text(goal.get("id", ""), 80),
                        run_id,
                        local_id,
                        claim_text,
                        claim_type,
                        "page_quote_matched",
                        "medium",
                        "fulltext_page",
                        now,
                        now,
                    ),
                )
                db.execute(
                    "INSERT INTO evidence_spans(id,claim_id,paper_id,page_number,section,quote,evidence_type,verification_status,source_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        evidence_id,
                        claim_id,
                        local_id,
                        page_number,
                        section,
                        quote,
                        "page_text",
                        "quote_match_page_text",
                        source_hash,
                        now,
                    ),
                )
                item = {
                    "id": claim_id,
                    "paper_id": local_id,
                    "paper_title": paper.get("title", ""),
                    "claim_text": claim_text,
                    "claim_type": claim_type,
                    "evidence_id": evidence_id,
                    "page": page_number,
                    "section": section,
                    "source_sha256": source_hash,
                    "evidence_scope": "fulltext_page",
                    "status": "page_quote_matched",
                    "comparison_field": field,
                }
                new_claims.append(item)
                evidence_candidates.append(
                    {
                        "claim_id": claim_id,
                        "paper_id": local_id,
                        "field": field,
                        "page": page_number,
                        "section": section,
                        "evidence_id": evidence_id,
                        "quote": quote,
                        "source_sha256": source_hash,
                    }
                )
                existing_fingerprints.add(fingerprint)
                resolved_count += 1
    remaining_count = max(0, len(work) - resolved_count)
    if not work:
        status = "no_gaps"
    elif resolved_count:
        status = "resolved" if remaining_count == 0 else "partial"
    elif no_local == len(work):
        status = "no_local_fulltext"
    else:
        status = "no_match"
    return {
        "attempted": attempted,
        "resolved_count": resolved_count,
        "remaining_count": remaining_count,
        "claims": new_claims,
        "evidence_candidates": evidence_candidates,
        "status": status,
        "warning": "只使用已有本地页级文本；候选论文不会自动下载或写入文献库",
    }


def _tool_generate_research_brief(payload: dict) -> dict:
    goal = payload.get("goal") or {}
    triage = payload.get("triage") or {}
    papers = (
        triage.get("candidates")
        if isinstance(triage.get("candidates"), list)
        else []
    )
    previous = payload.get("previous") or {}
    expanded = previous.get("expand_search", {}).get("papers", [])
    cited = previous.get("expand_citations", {}).get("papers", [])
    papers = _services()._unique_agent_papers(
        papers,
        expanded if isinstance(expanded, list) else [],
        cited if isinstance(cited, list) else [],
    )
    grouped = {}
    for paper in papers:
        key = paper.get("topic") or paper.get("venue") or "未分类"
        grouped[key] = grouped.get(key, 0) + 1
    claims = _services()._unique_agent_claims(
        previous.get("extract_claims", {}).get("claims", []),
        previous.get("resolve_comparison_gaps", {}).get("claims", []),
    )
    verification = (
        payload.get("verification")
        if isinstance(payload.get("verification"), dict)
        else (
            previous.get("verify_comparison_evidence")
            or previous.get("verify_evidence_refined")
            or previous.get("verify_evidence", {})
        )
    )
    critique = (
        payload.get("critique")
        if isinstance(payload.get("critique"), dict)
        else (
            previous.get("critique_claims_final")
            or previous.get("critique_claims_refined")
            or previous.get("critique_claims", {})
        )
    )
    coverage = (
        (payload.get("previous") or {})
        .get("evaluate_coverage", {})
        .get("coverage", {})
    )
    merge = previous.get("merge_versions", {})
    history = merge.get("history", {}) if isinstance(merge, dict) else {}
    artifact_type = (
        "follow_up_digest"
        if goal.get("workflow") == "follow_up"
        else (
            "research_map"
            if goal.get("workflow") == "research_map"
            else "evidence_brief"
        )
    )
    route_output = (
        previous.get("reading_route")
        if isinstance(previous.get("reading_route"), dict)
        else _services().call_agent_tool(
            "generate_reading_route", {"papers": papers}
        )
    )
    comparison_output = (
        previous.get("refresh_comparison")
        if isinstance(previous.get("refresh_comparison"), dict)
        else (
            previous.get("compare_candidates")
            if isinstance(previous.get("compare_candidates"), dict)
            else _services().call_agent_tool(
                "compare_papers", {"papers": papers, "claims": claims}
            )
        )
    )
    card_draft = (
        previous.get("draft_reading_card")
        if isinstance(previous.get("draft_reading_card"), dict)
        else {}
    )
    paper_overview = (
        previous.get("paper_analysis_overview", {})
        if isinstance(previous.get("paper_analysis_overview"), dict)
        else {}
    )
    artifact = {
        "artifact_type": artifact_type,
        "title": goal.get("name", "研究目标") + " · Agent 研究产物",
        "overview": "基于候选快照、版本关系、页级证据和单篇论文专业分析生成的可追溯研究产物。候选论文尚未自动写入文献库，需由你选择导入。",
        "coverage": {
            "candidate_count": len(papers),
            "topic_groups": grouped,
            "missing_abstract": sum(not bool(p.get("abstract")) for p in papers),
            "missing_fulltext": sum(
                not bool(p.get("pdf_url") or p.get("has_local_pdf")) for p in papers
            ),
            **coverage,
        },
        "fulltext_plan": previous.get("prepare_fulltext", {}),
        "reading_route": route_output.get("route", []),
        "comparison": comparison_output,
        "comparison_gap_resolution": previous.get("resolve_comparison_gaps", {}),
        "paper_analysis_overview": paper_overview,
        "reading_card_draft": card_draft,
        "version_groups": merge.get("groups", []),
        "candidates": papers,
        "claims": claims[:50],
        "evidence_status": verification,
        "critique": critique,
        "new_papers": history.get("new", []),
        "changed_papers": history.get("changed", []),
        "repeated_papers": history.get("repeated", []),
        "uncertainties": [
            "候选结果基于元数据和摘要；摘要匹配只证明引文存在于摘要，不证明全文结论",
            "未获取全文的论文不能视为已验证证据",
            "单篇分析的正向结论必须回到页码或摘要引用；未定位内容会作为缺口保留",
            "结果来自当前来源快照，后续运行可能发现新版本或重复记录",
        ],
    }
    run = payload.get("run") or {}
    if (
        (run.get("budget") or {}).get("consent_external") is True
        and _services().model_settings().get("enabled")
        and _services().model_api_key()
    ):
        # The model receives the local, evidence-scoped analysis as well as
        # metadata.  This keeps synthesis useful after full-text review
        # without allowing it to silently promote an abstract-only claim.
        compact_overviews = []
        candidate_scope = {}
        candidate_refs = {}
        for overview in (
            paper_overview.get("paper_overviews", [])
            if isinstance(paper_overview, dict)
            else []
        )[:16]:
            if not isinstance(overview, dict):
                continue
            analysis = (
                overview.get("analysis")
                if isinstance(overview.get("analysis"), dict)
                else {}
            )
            overview_id = _services().clean_text(overview.get("paper_id", ""), 200)
            candidate = next(
                (
                    item
                    for item in papers[:16]
                    if overview_id
                    in {
                        _services().clean_text(item.get("id", ""), 200),
                        _services().clean_text(item.get("local_paper_id", ""), 200),
                    }
                ),
                None,
            )
            candidate_id = (
                _services().clean_text(candidate.get("id", ""), 200)
                if candidate
                else overview_id
            )
            candidate_scope[candidate_id] = overview.get(
                "source_scope", "unverified"
            )
            refs = []
            for value in analysis.values():
                if isinstance(value, dict):
                    refs.extend(
                        item
                        for item in (value.get("evidence_refs") or [])
                        if isinstance(item, dict)
                    )
            deduped_refs = []
            seen_refs = set()
            for ref in refs:
                key = (ref.get("page"), ref.get("section"), ref.get("quote"))
                if key in seen_refs:
                    continue
                seen_refs.add(key)
                deduped_refs.append(
                    {
                        "page": ref.get("page"),
                        "section": _services().clean_text(
                            ref.get("section", ""), 80
                        ),
                        "quote": _services().clean_text(ref.get("quote", ""), 360),
                    }
                )
            candidate_refs[candidate_id] = deduped_refs[:8]
            compact_overviews.append(
                {
                    "candidate_id": candidate_id,
                    "title": overview.get("title", ""),
                    "source_scope": overview.get("source_scope", "unverified"),
                    "analysis": {
                        key: _services().clean_text(
                            (analysis.get(key) or {}).get("text", ""), 900
                        )
                        for key in (
                            "problem",
                            "method",
                            "assumptions",
                            "data_and_experiments",
                            "results",
                            "limitations",
                        )
                    },
                    "evidence_refs": deduped_refs[:8],
                    "critical_risks": [
                        _services().clean_text(item.get("text", ""), 500)
                        for item in (overview.get("critical_risks") or [])[:4]
                        if isinstance(item, dict)
                    ],
                    "open_questions": [
                        _services().clean_text(item, 400)
                        for item in (overview.get("open_questions") or [])[:6]
                    ],
                }
            )
        model_input = {
            "objective": goal.get("objective", ""),
            "focus": goal.get("focus", []),
            "papers": [
                {
                    "id": paper.get("id"),
                    "title": paper.get("title"),
                    "year": paper.get("year"),
                    "abstract": _services().clean_text(
                        paper.get("abstract", ""), 1200
                    ),
                }
                for paper in papers[:16]
            ],
            "evidence_scoped_overviews": compact_overviews,
            "comparison": {
                "rows": (comparison_output.get("rows") or [])[:16],
                "gaps": (comparison_output.get("gaps") or [])[:20],
            },
        }
        prompt = "你是严谨的研究生论文综合助理。只根据输入生成严格 JSON: {synthesis:string, themes:[{text:string,candidate_ids:[string],reason:string}], gaps:[{text:string,candidate_ids:[string],reason:string}], cited_candidate_ids:[string]}。source_scope=abstract_only 只能写摘要支持的线索；source_scope=fulltext_page 也只能写给定的分析和逐字证据，不得补造页码、数值、实验、数据集或因果结论。每个主题、缺口和综合判断必须精确列出支持它的候选 ID；没有具体支持 ID 的项目不要输出。证据不足必须明确写‘证据不足，需回到原文核验’，不能用泛泛常识填空。不要自行生成引文或证据范围，服务端会根据 ID 附加已经保存的真实证据。"
        try:
            draft = _services().model_json_request(prompt, model_input, timeout=90)
            allowed_ids = {paper.get("id") for paper in papers}
            cited_ids = (
                [
                    item
                    for item in draft.get("cited_candidate_ids", [])
                    if item in allowed_ids
                ]
                if isinstance(draft.get("cited_candidate_ids"), list)
                else []
            )

            def normalize_supported_items(values):
                normalized, notes = [], []
                for value in values[:20] if isinstance(values, list) else []:
                    item = (
                        value
                        if isinstance(value, dict)
                        else {"text": value, "candidate_ids": cited_ids}
                    )
                    text_value = _services().clean_text(item.get("text", ""), 800)
                    support_ids = (
                        list(
                            dict.fromkeys(
                                candidate_id
                                for candidate_id in item.get("candidate_ids", [])
                                if candidate_id in allowed_ids
                            )
                        )
                        if isinstance(item.get("candidate_ids"), list)
                        else []
                    )
                    if not text_value or not support_ids:
                        continue
                    normalized.append(text_value)
                    for candidate_id in support_ids:
                        scope = candidate_scope.get(candidate_id, "unverified")
                        for ref in candidate_refs.get(candidate_id, [])[:3]:
                            note = {
                                "candidate_id": candidate_id,
                                "scope": scope,
                                **ref,
                            }
                            if note not in notes:
                                notes.append(note)
                return normalized, notes

            themes, theme_evidence = normalize_supported_items(draft.get("themes"))
            gaps, gap_evidence = normalize_supported_items(draft.get("gaps"))
            synthesis = _services().clean_text(draft.get("synthesis", ""), 12000)
            synthesis_evidence = []
            for candidate_id in cited_ids:
                for ref in candidate_refs.get(candidate_id, [])[:2]:
                    note = {
                        "candidate_id": candidate_id,
                        "scope": candidate_scope.get(candidate_id, "unverified"),
                        **ref,
                    }
                    if note not in synthesis_evidence:
                        synthesis_evidence.append(note)
            artifact["model_draft"] = {
                "synthesis": synthesis,
                "themes": themes,
                "gaps": gaps,
                "theme_evidence": theme_evidence[:30],
                "gap_evidence": gap_evidence[:30],
                "synthesis_evidence": synthesis_evidence[:24],
                "cited_candidate_ids": cited_ids,
                "uncited_claim_count": int(bool(synthesis)) if not cited_ids else 0,
                "unsupported_theme_count": (
                    max(0, len(draft.get("themes", [])) - len(themes))
                    if isinstance(draft.get("themes"), list)
                    else 0
                ),
                "unsupported_gap_count": (
                    max(0, len(draft.get("gaps", [])) - len(gaps))
                    if isinstance(draft.get("gaps"), list)
                    else 0
                ),
                "model": _services().model_settings().get("model", ""),
            }
        except Exception as exc:
            artifact["model_draft_error"] = _services().clean_text(
                _services().model_error_detail(exc, _services().model_api_key()),
                500,
            )
    return artifact


__all__ = ['_tool_track_follow_up', '_tool_generate_reading_route', '_tool_compare_papers', '_tool_resolve_comparison_gaps', '_tool_generate_research_brief']
