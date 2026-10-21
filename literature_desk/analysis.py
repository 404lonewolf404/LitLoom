"""Abstract-only analysis and evidence-bounded summary formatting."""

from __future__ import annotations

from .services import app_services as _services


def _abstract_brief_prompt_echo(value: str) -> bool:
    """Detect a provider echoing our instruction instead of answering."""
    text = _services().clean_text(value, 12000).casefold()
    if not text:
        return True
    fingerprints = (
        "你是严谨的研究生论文助读助手",
        "仅根据题名、摘要和元数据",
        "总长度控制在 600 字以内",
        "总长度控制在600字以内",
        "需要回答中文",
        "明确这是摘要初筛",
        "禁止编造页码、实验数值",
    )
    # One exact role/prompt sentence is enough; weaker phrases need a pair so
    # that a legitimate answer mentioning “摘要初筛” is not rejected.
    if fingerprints[0] in text or fingerprints[1] in text:
        return True
    return sum(marker in text for marker in fingerprints[2:]) >= 2


def _abstract_brief_value(
    value, fallback: str = "摘要无法确认", limit: int = 520
) -> str:
    def clip(value: str, max_chars: int) -> str:
        text = _services().clean_text(value)
        if len(text) <= max_chars:
            return text
        if max_chars <= 1:
            return text[:max_chars]
        # Prefer a complete clause, then use an ellipsis if no nearby boundary exists.
        start = max(0, int(max_chars * 0.55))
        boundary = max(
            text.rfind(mark, start, max_chars)
            for mark in ("。", "；", ";", ".", "!", "！", "?", "？")
        )
        if boundary >= start:
            return text[: boundary + 1]
        return text[: max_chars - 1].rstrip(" ，、;；") + "…"

    if isinstance(value, list):
        values = [
            _services()._abstract_brief_value(item, "", limit) for item in value[:6]
        ]
        return clip("；".join(item for item in values if item) or fallback, limit)
    if isinstance(value, dict):
        return (
            clip(_services().json.dumps(value, ensure_ascii=False), limit) or fallback
        )
    text = clip(value, limit)
    return text or fallback


def _normalize_abstract_brief(result: dict, title: str = "") -> str:
    """Validate the structured abstract brief and format one stable answer."""
    if not isinstance(result, dict):
        raise ValueError("摘要助读返回必须是 JSON 对象")
    source = result
    for key in ("brief", "summary", "analysis", "data"):
        if isinstance(source.get(key), dict):
            source = source[key]
            break
    aliases = {
        "research_question": ("research_question", "research_problem", "研究问题"),
        "method_clues": ("method_clues", "method", "方法线索"),
        "possible_contributions": (
            "possible_contributions",
            "contributions",
            "contribution",
            "可能贡献",
        ),
        "needs_verification": ("needs_verification", "verification", "需要核验的问题"),
        "recommended_sections": (
            "recommended_sections",
            "recommended_reading",
            "适合继续阅读的段落/实验",
            "适合继续阅读",
        ),
        "uncertainties": ("uncertainties", "limitations", "摘要无法确认"),
    }
    values = {}
    for field, keys in aliases.items():
        values[field] = next(
            (source.get(key) for key in keys if source.get(key) not in (None, "", [])),
            None,
        )
    raw_answer = source.get("answer") or source.get("text") or source.get("content")
    if not any(value not in (None, "", []) for value in values.values()):
        if (
            isinstance(raw_answer, str)
            and raw_answer.strip()
            and not _services()._abstract_brief_prompt_echo(raw_answer)
        ):
            answer = _services()._abstract_brief_value(raw_answer, "", 600)
            if answer:
                return answer
        raise ValueError("摘要助读返回缺少有效字段")
    if (
        raw_answer
        and isinstance(raw_answer, str)
        and _services()._abstract_brief_prompt_echo(raw_answer)
    ):
        raise ValueError("模型返回了提示词内容而不是摘要助读")
    lines = ["【摘要初筛】"]
    if title:
        lines.append(f"题名：{_services()._abstract_brief_value(title, '', 120)}")
    labels = (
        ("research_question", "研究问题"),
        ("method_clues", "方法线索"),
        ("possible_contributions", "可能贡献"),
        ("needs_verification", "需要核验"),
        ("recommended_sections", "适合继续阅读"),
        ("uncertainties", "摘要无法确认"),
    )
    section_values = [
        _services()._abstract_brief_value(values.get(field), limit=20000)
        for field, _ in labels
    ]
    prefixes = [f"{label}：" for _, label in labels]
    fixed_chars = (
        sum(map(len, lines)) + sum(map(len, prefixes)) + len(lines) + len(prefixes) - 1
    )
    remaining = max(0, 600 - fixed_chars)
    weights = (1.0, 1.2, 1.0, 0.95, 0.95, 0.9)
    budgets = [0] * len(labels)
    # Allocate the remaining budget across every section, weighted toward method details.
    for _ in range(remaining):
        eligible = [
            i for i, value in enumerate(section_values) if budgets[i] < len(value)
        ]
        if not eligible:
            break
        index = min(eligible, key=lambda i: budgets[i] / weights[i])
        budgets[index] += 1
    lines.extend(
        prefix + _services()._abstract_brief_value(value, limit=budget)
        for prefix, value, budget in zip(prefixes, section_values, budgets)
    )
    return "\n".join(lines)


def _abstract_brief_needs_chinese(result: dict) -> bool:
    """Detect a structurally valid answer that ignored the Chinese UI contract."""
    if not isinstance(result, dict):
        return False
    source = result
    for key in ("brief", "summary", "analysis", "data"):
        if isinstance(source.get(key), dict):
            source = source[key]
            break
    values = []
    for key in (
        "research_question",
        "research_problem",
        "method_clues",
        "method",
        "possible_contributions",
        "contributions",
        "needs_verification",
        "recommended_sections",
        "recommended_reading",
        "uncertainties",
        "limitations",
        "answer",
        "text",
        "content",
    ):
        value = source.get(key)
        if value not in (None, "", []):
            values.append(
                value
                if isinstance(value, str)
                else _services().json.dumps(value, ensure_ascii=False)
            )
    text = " ".join(values)
    chinese = len(_services().re.findall(r"[\u4e00-\u9fff]", text))
    latin_words = len(_services().re.findall(r"[A-Za-z]{3,}", text))
    # Technical acronyms and dataset names are expected. Trigger only when
    # prose is overwhelmingly English, so a short English title cannot cause
    # a needless second model request.
    return latin_words >= 30 and latin_words > max(12, chinese * 1.2)


def _local_abstract_brief(row, reason: str = "") -> str:
    """Build a transparent, evidence-only brief when the model is unavailable.

    This is deliberately an excerpt, not an inferred summary.  It gives a
    durable job something useful to display while keeping the provider error
    and a retry path visible to the user.
    """
    title = _services()._abstract_brief_value(row["title"] or "", "未提供题名", 120)
    abstract = _services().clean_text(row["abstract"] or "", 16000)
    sentences = [
        _services().clean_text(part, 320)
        for part in _services().re.split(r"(?<=[。！？.!?])\s+|\n+", abstract)
        if _services().clean_text(part, 20)
    ]
    excerpt = sentences[0] if sentences else "摘要无法确认"
    method_sentences = [
        sentence
        for sentence in sentences
        if _services().re.search(
            r"\b(method|model|network|dataset|experiment|propose|use|based|estimate|evaluate)\b|方法|模型|数据集|实验|提出|采用|基于|评估",
            sentence,
            _services().re.I,
        )
    ]
    method = (
        method_sentences[0]
        if method_sentences
        else ("摘要中未找到可直接摘录的方法线索" if abstract else "摘要无法确认")
    )
    verification = [
        "模型服务未完成推理，需在额度或连接恢复后重试",
        "请回到摘要/全文核验研究问题、贡献和实验结果",
    ]
    if reason:
        verification.append("本次模型错误已记录在任务状态中")
    return _services()._normalize_abstract_brief(
        {
            "research_question": (
                f"摘要原文摘录：{excerpt}" if abstract else "摘要无法确认"
            ),
            "method_clues": f"摘要原文摘录：{method}" if abstract else "摘要无法确认",
            "possible_contributions": "模型未完成归纳；摘要原文未被自动判定为贡献。",
            "needs_verification": verification,
            "recommended_sections": [
                "摘要与引言：核验研究问题和贡献",
                "方法与实验：核验方法线索和结果",
            ],
            "uncertainties": [
                _services().ABSTRACT_LOCAL_FALLBACK_MARKER,
                "这不是 AI 结论，仅保留可追溯的摘要摘录",
            ],
        },
        title,
    )


def generate_abstract_brief_analysis(paper_id: str, progress_callback=None) -> dict:
    """Generate or reuse a durable abstract-only reading brief."""
    settings = _services().resolve_model_route(_services().model_settings(), "fast")
    api_key = _services().model_api_key()
    model_available = bool(api_key and settings.get("enabled"))
    unavailable_reason = "模型未启用或未配置 API Key" if not model_available else ""
    with _services().DB_LOCK, _services().db_session() as db:
        row = db.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
    if not row:
        raise LookupError("论文不存在")

    prompt_version = "abstract-brief-v4-zh-600-char"
    prompt = (
        "你是严谨的研究生论文助读助手。只依据用户提供的题名、摘要和元数据，不能使用外部知识，不能编造页码、数值或引文。"
        "所有自然语言字段必须使用简体中文；英文题名、作者名、会议名、模型名和缩写可保留原文并在必要时补充中文解释。"
        "请只输出一个 JSON 对象，不要 Markdown、前后解释或提示词复述。字段必须完整："
        "research_question、method_clues、possible_contributions 为字符串；needs_verification、recommended_sections、uncertainties 为字符串数组。"
        "总回答控制在600个字符以内（包括题名和各栏目标题）；每个栏目最多两句，数组最多三项，表达短而具体。"
        "证据不足就写‘摘要无法确认’，并明确这是摘要初筛而非全文结论。"
    )
    user_payload = {
        "title": row["title"],
        "authors": row["authors"],
        "year": row["year"],
        "venue": row["venue"],
        "abstract": row["abstract"] or "",
    }
    user = _services().json.dumps(user_payload, ensure_ascii=False)
    input_hash = _services().hashlib.sha256(user.encode("utf-8")).hexdigest()
    abstract_contract = {
        "required": [
            "research_question",
            "method_clues",
            "possible_contributions",
            "needs_verification",
            "recommended_sections",
            "uncertainties",
        ],
        "nonempty": ["research_question", "method_clues", "possible_contributions"],
    }
    provider = settings["base_url"]
    model = settings["model"]
    with _services().DB_LOCK, _services().db_session() as db:
        cached = db.execute(
            "SELECT id,answer,created_at FROM ai_artifacts WHERE paper_id=? AND artifact_type='abstract_brief' AND provider=? AND model=? AND prompt_version=? AND input_sha256=? ORDER BY created_at DESC LIMIT 1",
            (paper_id, provider, model, prompt_version, input_hash),
        ).fetchone()
    # Do not reuse a local fallback after a later restart: once the provider
    # becomes available the next request should attempt the real model again.
    if cached and _services().ABSTRACT_LOCAL_FALLBACK_MARKER not in (
        cached["answer"] or ""
    ):
        return {
            "artifact_id": cached["id"],
            "model": model,
            "answer": cached["answer"],
            "basis": "标题、摘要和元数据；非全文证据",
            "created_at": cached["created_at"],
            "prompt_version": prompt_version,
            "input_sha256": input_hash,
            "duration_ms": 0,
            "reused": True,
        }

    started = _services().time.perf_counter()
    fallback = False
    fallback_reason = ""
    if not _services().clean_text(row["abstract"] or "", 16000):
        answer = _services()._normalize_abstract_brief(
            {
                "research_question": "摘要无法确认",
                "method_clues": "摘要无法确认",
                "possible_contributions": "摘要无法确认",
                "needs_verification": ["需要获取摘要或全文后核验"],
                "recommended_sections": ["摘要无法确认"],
                "uncertainties": ["来源未提供摘要，未调用模型"],
            },
            row["title"],
        )
    elif not model_available:
        answer = _services()._local_abstract_brief(row, unavailable_reason)
        fallback = True
        fallback_reason = unavailable_reason
    else:
        answer = ""
        last_error = None
        if progress_callback:
            progress_callback("model_request", 0, 1, "正在根据摘要生成结构化助读")
        for attempt in range(2):
            request_prompt = (
                prompt
                if attempt == 0
                else (
                    prompt
                    + "\n上一次响应未通过质量校验。请修正后重新输出完整 JSON；严禁复述系统指令，字段必须有实际内容或写摘要无法确认。所有说明必须改为简体中文，最终格式化回答仍须在600字以内。"
                )
            )
            try:
                # DeepSeek-compatible reasoning relays may spend the small
                # application cap on visible analysis before emitting JSON.
                # The contract selects the final object from any echoed
                # preamble, while an omitted cap prevents false truncation and
                # the structured abstract pass does not need extra reasoning.
                result = _services().model_json_request(
                    request_prompt,
                    user_payload,
                    timeout=120,
                    max_output_tokens=None,
                    json_contract=abstract_contract,
                    reasoning_effort="",
                )
                if _services()._abstract_brief_needs_chinese(result):
                    raise ValueError("摘要助读自然语言字段未按要求使用简体中文")
                candidate = _services()._normalize_abstract_brief(result, row["title"])
                if _services()._abstract_brief_prompt_echo(candidate):
                    raise ValueError("模型返回了提示词内容而不是摘要助读")
                answer = candidate
                break
            except Exception as exc:
                last_error = exc
                if attempt >= 1:
                    fallback_reason = (
                        _services().model_error_detail(exc, api_key)
                        or "摘要助读输出未通过校验"
                    )
                    answer = _services()._local_abstract_brief(row, fallback_reason)
                    fallback = True
                    break
        if not answer:
            fallback_reason = fallback_reason or (
                _services().model_error_detail(last_error, api_key)
                if last_error
                else "摘要助读未生成有效结果"
            )
            answer = _services()._local_abstract_brief(row, fallback_reason)
            fallback = True

    duration_ms = round((_services().time.perf_counter() - started) * 1000, 2)
    created = _services().now_iso()
    artifact_id = str(_services().uuid.uuid4())
    with _services().DB_LOCK, _services().db_session() as db:
        db.execute(
            "INSERT INTO ai_artifacts(id,paper_id,artifact_type,provider,model,prompt_version,input_sha256,answer,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                artifact_id,
                paper_id,
                "abstract_brief",
                provider,
                model,
                prompt_version,
                input_hash,
                answer,
                created,
            ),
        )
        _services().record_audit(
            db,
            "ai.abstract_brief.created",
            "paper",
            paper_id,
            {
                "model": model,
                "duration_ms": duration_ms,
                "input_chars": len(user),
                "output_chars": len(answer),
                "stream": False,
                "artifact_id": artifact_id,
            },
        )
    if progress_callback:
        progress_callback("saving", 1, 1, "摘要助读已保存到论文历史")
    return {
        "artifact_id": artifact_id,
        "model": model,
        "answer": answer,
        "basis": "标题、摘要和元数据；非全文证据",
        "created_at": created,
        "prompt_version": prompt_version,
        "input_sha256": input_hash,
        "duration_ms": duration_ms,
        "reused": False,
        "analysis_status": "partial" if fallback else "complete",
        "fallback": fallback,
        "fallback_reason": fallback_reason,
    }


__all__ = [
    "_abstract_brief_prompt_echo",
    "_abstract_brief_value",
    "_normalize_abstract_brief",
    "_abstract_brief_needs_chinese",
    "_local_abstract_brief",
    "generate_abstract_brief_analysis",
]
