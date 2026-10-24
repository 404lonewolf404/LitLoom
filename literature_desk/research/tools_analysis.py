"""Agent analysis tool implementations."""

from __future__ import annotations

from ..services import app_services as _services

def _tool_draft_reading_card(payload: dict) -> dict:
    paper = payload.get("paper") if isinstance(payload.get("paper"), dict) else {}
    return _services()._agent_fulltext_reading_card(paper, payload)


def _tool_extract_claims(payload: dict) -> dict:
    papers = (
        payload.get("papers") if isinstance(payload.get("papers"), list) else []
    )
    goal = payload.get("goal") or {}
    run_id = _services().clean_text(payload.get("run_id", ""), 80)
    claims = []
    now = _services().now_iso()
    with _services().DB_LOCK, _services().db_session() as db:
        for paper in papers[:50]:
            local_paper_id = _services().clean_text(
                paper.get("local_paper_id", ""), 200
            )
            page_rows = (
                db.execute(
                    "SELECT page_number,text,text_sha256 FROM document_pages WHERE paper_id=? AND quality<>'empty' AND trim(text)<>'' ORDER BY page_number LIMIT 160",
                    (local_paper_id,),
                ).fetchall()
                if local_paper_id
                else []
            )
            if page_rows:
                # Cover the paper's structure instead of taking only the
                # first three pages.  One representative page per
                # section plus pages containing result/ablation/figure
                # cues gives the critic evidence from methods, results and
                # limitations while keeping the claim budget bounded.
                selected_pages = []
                seen_sections = set()
                cue_terms = (
                    "result",
                    "experiment",
                    "ablation",
                    "limitation",
                    "conclusion",
                    "figure",
                    "table",
                    "结果",
                    "实验",
                    "消融",
                    "局限",
                    "结论",
                    "图",
                    "表",
                )
                for row in page_rows:
                    section = _services()._infer_page_section(row["text"])
                    normalized = (
                        _services()
                        .re.sub(
                            r"\s+", " ", _services().clean_text(row["text"], 5000)
                        )
                        .strip()
                    )
                    if section not in seen_sections or any(
                        term in normalized.casefold() for term in cue_terms
                    ):
                        selected_pages.append(row)
                        seen_sections.add(section)
                    if len(selected_pages) >= 12:
                        break
                quote_data = []
                for row in selected_pages:
                    normalized = (
                        _services()
                        .re.sub(
                            r"\s+", " ", _services().clean_text(row["text"], 5000)
                        )
                        .strip()
                    )
                    quote = normalized[:700]
                    for cue in cue_terms:
                        position = normalized.casefold().find(cue.casefold())
                        if position >= 0:
                            quote = normalized[
                                max(0, position - 180) : position + 520
                            ]
                            break
                    quote_data.append(
                        {
                            "quote": quote[:700],
                            "page": int(row["page_number"]),
                            "sha256": row["text_sha256"],
                            "section": _services()._infer_page_section(row["text"]),
                        }
                    )
                scope = "fulltext_page"
            else:
                abstract = _services().clean_text(paper.get("abstract", ""), 30000)
                if not abstract:
                    continue
                quote_data = [
                    {
                        "quote": _services()
                        .re.sub(r"\s+", " ", abstract)
                        .strip()[:700],
                        "page": None,
                        "sha256": _services()
                        .hashlib.sha256(abstract.encode("utf-8"))
                        .hexdigest(),
                        "section": "abstract",
                    }
                ]
                scope = "abstract"
            if not quote_data:
                continue
            for evidence in quote_data[:8]:
                quote = evidence["quote"]
                if not quote:
                    continue
                claim_id = str(_services().uuid.uuid4())
                lower_quote = quote.casefold()
                if any(
                    term in lower_quote
                    for term in (
                        "limitation",
                        "failure",
                        "future work",
                        "局限",
                        "不足",
                        "失败",
                    )
                ):
                    claim_type = "limitation"
                elif any(
                    term in lower_quote
                    for term in (
                        "dataset",
                        "benchmark",
                        "data set",
                        "数据集",
                        "样本",
                    )
                ):
                    claim_type = "dataset"
                elif any(
                    term in lower_quote
                    for term in (
                        "outperform",
                        "accuracy",
                        "f1",
                        "auc",
                        "实验",
                        "结果",
                        "improv",
                    )
                ):
                    claim_type = "result"
                elif any(
                    term in lower_quote
                    for term in (
                        "method",
                        "approach",
                        "architecture",
                        "model",
                        "方法",
                        "模型",
                    )
                ):
                    claim_type = "method"
                else:
                    claim_type = "research_question"
                claim_text = (
                    "原文摘录：" if evidence["page"] else "摘要摘录："
                ) + quote
                claim_status = "needs_verification"
                db.execute(
                    "INSERT INTO research_claims(id,goal_id,run_id,paper_id,claim_text,claim_type,status,confidence,evidence_scope,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        claim_id,
                        goal["id"],
                        run_id,
                        _services().clean_text(
                            local_paper_id or paper.get("id", ""), 200
                        ),
                        claim_text,
                        claim_type,
                        claim_status,
                        "medium",
                        scope,
                        now,
                        now,
                    ),
                )
                evidence_id = str(_services().uuid.uuid4())
                db.execute(
                    "INSERT INTO evidence_spans(id,claim_id,paper_id,page_number,section,quote,evidence_type,verification_status,source_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        evidence_id,
                        claim_id,
                        _services().clean_text(
                            local_paper_id or paper.get("id", ""), 200
                        ),
                        evidence["page"],
                        evidence["section"],
                        quote,
                        "page_text" if evidence["page"] else "abstract_text",
                        "unverified",
                        evidence["sha256"],
                        now,
                    ),
                )
                claims.append(
                    {
                        "id": claim_id,
                        "paper_id": local_paper_id or paper.get("id"),
                        "paper_title": paper.get("title"),
                        "claim_text": claim_text,
                        "claim_type": claim_type,
                        "evidence_id": evidence_id,
                        "page": evidence["page"],
                        "evidence_scope": scope,
                        "status": claim_status,
                    }
                )
                if len(claims) >= 240:
                    break
            if len(claims) >= 240:
                break
    return {
        "claims": claims,
        "claim_count": len(claims),
        "claim_types": {
            kind: sum(item.get("claim_type") == kind for item in claims)
            for kind in {item.get("claim_type") for item in claims}
        },
        "page_scoped": sum(item.get("page") is not None for item in claims),
        "abstract_only": sum(item.get("page") is None for item in claims),
        "warning": "摘要证据不能替代全文页码证据；页码摘录仅证明文本命中，不证明研究结论正确",
    }


def _tool_resolve_evidence_gaps(payload: dict) -> dict:
    previous = payload.get("previous") or {}
    claims = previous.get("extract_claims", {}).get("claims", [])
    verification = payload.get("verification") or previous.get(
        "verify_evidence", {}
    )
    result_rows = (
        verification.get("results") if isinstance(verification, dict) else []
    )
    unresolved_ids = {
        row.get("claim_id")
        for row in result_rows
        if isinstance(row, dict)
        and row.get("status")
        in {"abstract_only", "evidence_unverified", "quote_not_found"}
    }
    if not unresolved_ids:
        unresolved_ids = {
            claim.get("id")
            for claim in claims
            if claim.get("evidence_scope") == "abstract"
        }
    attempted = resolved = 0
    evidence_candidates = []
    now = _services().now_iso()
    with _services().DB_LOCK, _services().db_session() as db:
        for claim in claims[:50]:
            claim_id = _services().clean_text(claim.get("id", ""), 80)
            if not claim_id or claim_id not in unresolved_ids:
                continue
            paper_id = _services().clean_text(claim.get("paper_id", ""), 200)
            if not paper_id or paper_id.startswith("candidate:"):
                continue
            attempted += 1
            terms = [
                term
                for term in _services().re.findall(
                    r"[A-Za-z0-9][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}",
                    _services().clean_text(claim.get("claim_text", ""), 1000),
                )
                if term.casefold() not in {"原文摘录", "摘要摘录", "claim"}
            ]
            query = " ".join(dict.fromkeys(terms[:5]))
            if not query:
                continue
            pages = _services().search_document_pages(paper_id, query, 3)
            if not pages:
                # Search one discriminative term when an AND-style query
                # is too strict for a sparse or OCR-derived page index.
                pages = (
                    _services().search_document_pages(paper_id, terms[0], 3)
                    if terms
                    else []
                )
            for page in pages[:2]:
                page_number = int(page.get("page_number") or page.get("page") or 0)
                source_row = db.execute(
                    "SELECT text,text_sha256 FROM document_pages WHERE paper_id=? AND page_number=? ORDER BY extracted_at DESC LIMIT 1",
                    (paper_id, page_number),
                ).fetchone()
                source_text = _services().clean_text(
                    source_row["text"] if source_row else page.get("snippet", ""),
                    500000,
                )
                quote = _services().clean_text(source_text[:700], 1000)
                if not page_number or not quote:
                    continue
                page_hash = _services().clean_text(
                    (
                        source_row["text_sha256"]
                        if source_row
                        else page.get("text_sha256", "")
                    ),
                    100,
                )
                evidence_id = str(_services().uuid.uuid4())
                db.execute(
                    "INSERT INTO evidence_spans(id,claim_id,paper_id,page_number,section,quote,evidence_type,verification_status,source_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        evidence_id,
                        claim_id,
                        paper_id,
                        page_number,
                        _services().clean_text(
                            page.get("section") or "PDF 页面文本", 200
                        ),
                        quote[:700],
                        "page_text",
                        "unverified",
                        page_hash,
                        now,
                    ),
                )
                evidence_candidates.append(
                    {
                        "claim_id": claim_id,
                        "paper_id": paper_id,
                        "page": page_number,
                        "evidence_id": evidence_id,
                        "quote": quote[:700],
                    }
                )
                resolved += 1
    remaining = max(
        0,
        len(unresolved_ids)
        - len({item["claim_id"] for item in evidence_candidates}),
    )
    return {
        "round": int(payload.get("round", 1) or 1),
        "attempted": attempted,
        "resolved_count": resolved,
        "remaining_count": remaining,
        "evidence_candidates": evidence_candidates,
        "status": (
            "resolved"
            if resolved
            else ("no_local_fulltext" if attempted == 0 else "no_match")
        ),
    }


def _tool_verify_evidence(payload: dict) -> dict:
    goal = payload.get("goal") or {}
    run_id = _services().clean_text(payload.get("run_id", ""), 80)
    verified = needs_fulltext = missing = 0
    results = []
    with _services().DB_LOCK, _services().db_session() as db:
        rows = db.execute(
            "SELECT c.id,c.paper_id,c.claim_text,c.claim_type,e.id AS evidence_id,e.quote,e.section,e.page_number,e.source_sha256 FROM research_claims c JOIN evidence_spans e ON e.claim_id=c.id WHERE c.run_id=? ORDER BY c.created_at",
            (run_id,),
        ).fetchall()
        grouped = {}
        for row in rows:
            grouped.setdefault(row["id"], []).append(row)
        for claim_id, claim_rows in grouped.items():
            page_found = abstract_found = False
            page_evidence_ids, abstract_evidence_ids, missing_evidence_ids = (
                [],
                [],
                [],
            )
            for row in claim_rows:
                quote = _services().re.sub(r"\s+", " ", row["quote"] or "").strip()
                if row["section"] == "abstract":
                    candidate_row = db.execute(
                        "SELECT record_json FROM discovery_candidates WHERE id=?",
                        (row["paper_id"],),
                    ).fetchone()
                    paper = (
                        _services()._json_value(candidate_row[0], {})
                        if candidate_row
                        else {}
                    )
                    haystack = (
                        _services()
                        .re.sub(
                            r"\s+",
                            " ",
                            _services().clean_text(
                                paper.get("abstract", ""), 30000
                            ),
                        )
                        .strip()
                    )
                    if not haystack and quote:
                        haystack = quote
                    found = bool(quote and quote.casefold() in haystack.casefold())
                    if found:
                        abstract_found = True
                        abstract_evidence_ids.append(row["evidence_id"])
                        db.execute(
                            "UPDATE evidence_spans SET verification_status='quote_match_abstract_only' WHERE id=?",
                            (row["evidence_id"],),
                        )
                    else:
                        missing_evidence_ids.append(row["evidence_id"])
                        db.execute(
                            "UPDATE evidence_spans SET verification_status='quote_not_found' WHERE id=?",
                            (row["evidence_id"],),
                        )
                else:
                    page_row = db.execute(
                        "SELECT text,text_sha256 FROM document_pages WHERE paper_id=? AND page_number=?",
                        (row["paper_id"], row["page_number"]),
                    ).fetchone()
                    haystack = (
                        _services()
                        .re.sub(
                            r"\s+",
                            " ",
                            _services().clean_text(page_row["text"], 500000),
                        )
                        .strip()
                        if page_row
                        else ""
                    )
                    found = bool(quote and quote.casefold() in haystack.casefold())
                    if found:
                        page_found = True
                        page_evidence_ids.append(row["evidence_id"])
                        db.execute(
                            "UPDATE evidence_spans SET verification_status='quote_match_page_text' WHERE id=?",
                            (row["evidence_id"],),
                        )
                    else:
                        missing_evidence_ids.append(row["evidence_id"])
                        db.execute(
                            "UPDATE evidence_spans SET verification_status='quote_not_found' WHERE id=?",
                            (row["evidence_id"],),
                        )
            if page_found:
                claim_status, evidence_status = (
                    "page_quote_matched",
                    "quote_match_page_text",
                )
                verified += 1
            elif abstract_found:
                claim_status, evidence_status = (
                    "abstract_only",
                    "quote_match_abstract_only",
                )
                verified += 1
                needs_fulltext += 1
            else:
                claim_status, evidence_status = (
                    "evidence_unverified",
                    "quote_not_found",
                )
                missing += 1
            db.execute(
                "UPDATE research_claims SET status=?,updated_at=? WHERE id=?",
                (claim_status, _services().now_iso(), claim_id),
            )
            results.append(
                {
                    "claim_id": claim_id,
                    "status": claim_status,
                    "evidence_status": evidence_status,
                    "page_evidence_count": len(page_evidence_ids),
                    "abstract_evidence_count": len(abstract_evidence_ids),
                    "unmatched_evidence_count": len(missing_evidence_ids),
                }
            )
    return {
        "claims_checked": len(results),
        "abstract_quote_matches": verified,
        "needs_fulltext": needs_fulltext,
        "evidence_not_found": missing,
        "results": results,
        "goal_id": goal.get("id"),
    }


def _tool_critique_claims(payload: dict) -> dict:
    goal = payload.get("goal") or {}
    run_id = _services().clean_text(payload.get("run_id", ""), 80)
    claims = (
        payload.get("claims")
        if isinstance(payload.get("claims"), list)
        else (payload.get("previous") or {})
        .get("extract_claims", {})
        .get("claims", [])
    )
    previous = payload.get("previous") or {}
    verification = (
        payload.get("verification")
        or previous.get("verify_evidence_refined")
        or previous.get("verify_evidence", {})
    )
    verification_by_id = {
        item.get("claim_id"): item
        for item in (verification.get("results") or [])
        if isinstance(item, dict)
    }
    critiques = []
    for claim in claims[:100]:
        claim_id = _services().clean_text(claim.get("id", ""), 80)
        verification_status = verification_by_id.get(claim_id, {}).get("status", "")
        scope = (
            "fulltext_page"
            if verification_status == "page_quote_matched"
            else (claim.get("evidence_scope") or "unknown")
        )
        text = _services().clean_text(claim.get("claim_text", ""), 1000)
        if scope == "abstract":
            critiques.append(
                {
                    "claim_id": claim_id,
                    "category": "evidence_boundary",
                    "severity": "high",
                    "critique": "该 Claim 只有摘要证据，不能支持对完整实验、公式或局限的强结论。",
                }
            )
        if claim.get("claim_type") == "result" and not _services().re.search(
            r"\d|improv|outperform|accuracy|f1|auc|结果", text, _services().re.I
        ):
            critiques.append(
                {
                    "claim_id": claim_id,
                    "category": "unsupported_result",
                    "severity": "medium",
                    "critique": "被标记为结果 Claim，但摘录没有明确数值或比较对象，需要回到表格和实验设置核对。",
                }
            )
        if claim.get("claim_type") == "method" and scope != "fulltext_page":
            critiques.append(
                {
                    "claim_id": claim_id,
                    "category": "method_detail_missing",
                    "severity": "medium",
                    "critique": "方法 Claim 缺少全文页级证据，公式、超参数和实现细节尚未核验。",
                }
            )
    if verification.get("evidence_not_found"):
        critiques.append(
            {
                "claim_id": "",
                "category": "evidence_integrity",
                "severity": "high",
                "critique": f"有 {verification['evidence_not_found']} 条证据摘录无法在保存的来源中重新定位。",
            }
        )
    now = _services().now_iso()
    with _services().DB_LOCK, _services().db_session() as db:
        for item in critiques:
            if not item.get("claim_id"):
                continue
            db.execute(
                "INSERT INTO claim_critiques(id,claim_id,goal_id,run_id,category,severity,critique_text,evidence_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    str(_services().uuid.uuid4()),
                    item["claim_id"],
                    _services().clean_text(goal.get("id", ""), 80),
                    run_id,
                    item["category"],
                    item["severity"],
                    item["critique"],
                    _services().json.dumps(
                        {
                            "scope": next(
                                (
                                    c.get("evidence_scope")
                                    for c in claims
                                    if c.get("id") == item["claim_id"]
                                ),
                                "unknown",
                            )
                        },
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )
    categories = {}
    for item in critiques:
        categories[item["category"]] = categories.get(item["category"], 0) + 1
    return {
        "critiques": critiques,
        "critique_count": len(critiques),
        "categories": categories,
        "open_issues": [
            item["critique"]
            for item in critiques
            if item["severity"] in {"high", "critical"}
        ],
        "status": "needs_review" if critiques else "no_obvious_issue",
    }


def _tool_build_paper_analysis_overview(payload: dict) -> dict:
    papers = (
        payload.get("papers") if isinstance(payload.get("papers"), list) else []
    )
    claims = (
        payload.get("claims") if isinstance(payload.get("claims"), list) else []
    )
    verification = (
        payload.get("verification")
        if isinstance(payload.get("verification"), dict)
        else {}
    )
    critique = (
        payload.get("critique") if isinstance(payload.get("critique"), dict) else {}
    )
    return _services()._paper_analysis_overview(
        papers, claims, verification, critique
    )


def _tool_check_citation_completeness(payload: dict) -> dict:
    previous = payload.get("previous") or {}
    claims = _services()._unique_agent_claims(
        previous.get("extract_claims", {}).get("claims", []),
        previous.get("resolve_comparison_gaps", {}).get("claims", []),
    )
    artifact = (
        previous.get("synthesize_map")
        or previous.get("draft_review")
        or previous.get("follow_up_map")
        or {}
    )
    # The synthesis artifact carries the original Claim IDs.  Comparing a
    # Claim ID with its paper ID made every non-empty review look incomplete
    # even when the source Claim had been preserved verbatim.
    cited = {
        item.get("id") or item.get("claim_id")
        for item in artifact.get("claims", [])
        if isinstance(item, dict)
    }
    missing = [item.get("id") for item in claims if item.get("id") not in cited]
    claim_groups = {}
    for item in artifact.get("claims", []):
        if not isinstance(item, dict):
            continue
        text = (
            _services()
            .re.sub(
                r"\s+",
                " ",
                _services().clean_text(item.get("claim_text", ""), 2000),
            )
            .strip()
            .casefold()
        )
        if len(text) >= 24:
            claim_groups.setdefault(text, []).append(
                item.get("id") or item.get("claim_id")
            )
    duplicate_claim_groups = [
        ids
        for ids in claim_groups.values()
        if len([value for value in ids if value]) > 1
    ]
    duplicate_claim_ids = [
        value for group in duplicate_claim_groups for value in group if value
    ][:100]
    return {
        "claims_total": len(claims),
        "claims_cited": len(claims) - len(missing),
        "missing_claim_ids": missing[:100],
        "duplicate_claim_groups": duplicate_claim_groups[:50],
        "duplicate_claim_ids": duplicate_claim_ids,
        "duplicate_claim_count": len(duplicate_claim_ids),
        "complete": not missing and not duplicate_claim_groups,
    }


def _tool_evaluate_stop_conditions(payload: dict) -> dict:
    previous = payload.get("previous") or {}
    coverage = previous.get("evaluate_coverage", {}).get("coverage", {})
    verification = (
        previous.get("verify_comparison_evidence")
        or previous.get("verify_evidence_refined")
        or previous.get("verify_evidence", {})
    )
    budget = payload.get("run", {}).get("budget", {})
    blocking_reasons = []
    if verification.get("needs_fulltext"):
        blocking_reasons.append("仍有摘要级证据需要全文核验")
    critique = (
        previous.get("critique_claims_final")
        or previous.get("critique_claims_refined")
        or previous.get("critique_claims", {})
    )
    if critique.get("open_issues"):
        blocking_reasons.append("存在高风险批判项")
    citation_check = previous.get("check_citation_completeness", {})
    if citation_check.get("duplicate_claim_count"):
        blocking_reasons.append("研究产物包含重复 Claim，需要合并后再停止")
    comparison_resolution = previous.get("resolve_comparison_gaps", {})
    if int(comparison_resolution.get("remaining_count") or 0) > 0:
        blocking_reasons.append(
            f"比较矩阵仍有 {int(comparison_resolution.get('remaining_count') or 0)} 个字段缺少页级证据"
        )
    if coverage.get("candidate_count", 0) < 5:
        blocking_reasons.append("候选数量尚未达到最低覆盖")
    stop = not blocking_reasons
    reasons = (
        ["覆盖、证据和批判检查均已达到当前预算"] if stop else list(blocking_reasons)
    )
    return {
        "stop": stop,
        "reasons": reasons,
        "blocking_reasons": blocking_reasons,
        "budget": budget,
    }


def _tool_save_reading_card(payload: dict) -> dict:
    paper_id = _services().clean_text(payload.get("paper_id", ""), 200)
    values = (
        payload.get("reading_card")
        if isinstance(payload.get("reading_card"), dict)
        else {}
    )
    if not paper_id:
        raise ValueError("需要 paper_id")
    with _services().DB_LOCK, _services().db_session() as db:
        if not db.execute(
            "SELECT 1 FROM papers WHERE id=?", (paper_id,)
        ).fetchone():
            raise ValueError("论文不存在或尚未导入文献库")
        current = _services().reading_card_dict(
            db.execute(
                "SELECT * FROM reading_cards WHERE paper_id=?", (paper_id,)
            ).fetchone(),
            paper_id,
        )
        merged = _services().merge_ai_reading_card(
            current, values, bool(payload.get("merge_empty", True))
        )
        result = _services().save_reading_card(db, paper_id, merged, "agent")
        _services().record_audit(
            db,
            "agent.reading_card_applied",
            "paper",
            paper_id,
            {"run_id": payload.get("run_id", "")},
        )
    return {"paper_id": paper_id, "reading_card": result, "applied": True}


def _tool_export_report(payload: dict) -> dict:
    content = _services().clean_text(payload.get("content", ""), 200000)
    if not content:
        raise ValueError("没有可导出的研究报告内容")
    filename = (
        "research-report-"
        + _services().datetime.now().strftime("%Y%m%d-%H%M%S")
        + ".md"
    )
    return {
        "filename": filename,
        "content": content,
        "content_sha256": _services()
        .hashlib.sha256(content.encode("utf-8"))
        .hexdigest(),
        "created_at": _services().now_iso(),
    }


__all__ = ['_tool_draft_reading_card', '_tool_extract_claims', '_tool_resolve_evidence_gaps', '_tool_verify_evidence', '_tool_critique_claims', '_tool_build_paper_analysis_overview', '_tool_check_citation_completeness', '_tool_evaluate_stop_conditions', '_tool_save_reading_card', '_tool_export_report']
