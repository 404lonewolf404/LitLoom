"""Worker factories for page-aware full-text analysis."""

from __future__ import annotations

from ..services import app_services as _services

def _build_chunk_analyzer(context):
    chunks = context.chunks
    effective_visual_page_budget = context.effective_visual_page_budget
    fast_reading_card = context.fast_reading_card
    local_prompt = context.local_prompt
    mark_provider_failure = context.mark_provider_failure
    mode = context.mode
    model_call = context.model_call
    paper = context.paper
    progress_callback = context.progress_callback
    provider_failure_detail = context.provider_failure_detail
    provider_failure_event = context.provider_failure_event
    rows = context.rows
    separate_visual_pass = context.separate_visual_pass
    total_progress = context.total_progress
    visual_requested = context.visual_requested
    def analyze_chunk(
        index: int, chunk: dict, retry_depth: int = 0, allow_visual: bool = True
    ) -> tuple[int, dict, dict, float]:
        """Analyze one chunk; independent chunks are safe to run concurrently."""
        if provider_failure_event.is_set():
            detail = provider_failure_detail.get("value") or "模型服务拒绝了请求"
            return (
                index,
                {
                    "notes": [],
                    "visual_observations": [],
                    "evidence_spans": [],
                    "uncertainties": [
                        f"模型服务已被前一分块判定为不可用：{_services().clean_text(detail, 240)}"
                    ],
                    "chunk_pages": list(chunk.get("pages", [])),
                    "visual_pages": [],
                    "_chunk_errors": [
                        {"pages": list(chunk.get("pages", [])), "error": detail}
                    ],
                },
                {
                    "failures": [],
                    "rendered_pages": 0,
                    "requested_pages": 0,
                    "images": [],
                },
                0.0,
            )
        if progress_callback:
            progress_callback(
                "chunk_analysis",
                None,
                total_progress,
                f"已提交并行内容块 {index + 1}/{len(chunks)}，等待模型响应",
            )
        refs = {
            (str(item.get("attachment_id", "")), int(item.get("page") or 0))
            for item in chunk.get("page_refs", [])
        }
        chunk_rows = [
            row
            for row in rows
            if (str(row.get("attachment_id", "")), int(row.get("page_number") or 0))
            in refs
        ]
        # On long papers the visual pass is run separately with a page-sized
        # payload.  Short papers keep the compact inline multimodal path.
        visual_rows = (
            _services().select_visual_page_rows(
                chunk_rows, effective_visual_page_budget, mode
            )
            if allow_visual and not separate_visual_pass
            else []
        )
        visual = (
            _services().prepare_visual_pages(
                visual_rows, max_pages=effective_visual_page_budget
            )
            if visual_requested and visual_rows
            else {
                "images": [],
                "failures": [],
                "rendered_pages": 0,
                "requested_pages": 0,
            }
        )
        payload = {
            "paper": {"title": paper["title"], "mode": mode},
            "pages": chunk["text"],
            "visual_context": {
                "page_refs": chunk.get("page_refs", []),
                "rendered_pages": visual.get("rendered_pages", 0),
                "failures": visual.get("failures", []),
            },
        }
        request_started = _services().time.perf_counter()
        chunk_output_limit = (
            (
                _services().READING_CARD_SYNTHESIS_MAX_OUTPUT_TOKENS
                if fast_reading_card
                else _services().READING_CARD_CHUNK_MAX_OUTPUT_TOKENS
            )
            if mode == "reading_card"
            else _services().MODEL_CHUNK_MAX_OUTPUT_TOKENS
        )
        try:
            try:
                if visual.get("images"):
                    result = model_call(
                        "chunk_analysis",
                        "multimodal",
                        lambda: _services().limited_multimodal_model_request(
                            local_prompt,
                            payload,
                            visual["images"],
                            timeout=150,
                            output_tokens=chunk_output_limit,
                            reasoning_effort=_services().MODEL_ANALYSIS_REASONING_EFFORT,
                        ),
                        len(chunk["text"]),
                        len(visual["images"]),
                        page_numbers=chunk.get("pages", []),
                        progress_message=f"并行分析内容块 {index + 1}/{len(chunks)}（文字与页面图像）",
                    )
                else:
                    result = model_call(
                        "chunk_analysis",
                        "text",
                        lambda: _services().limited_text_model_request(
                            local_prompt,
                            payload,
                            timeout=120,
                            output_tokens=chunk_output_limit,
                            reasoning_effort=_services().MODEL_ANALYSIS_REASONING_EFFORT,
                        ),
                        len(chunk["text"]),
                        page_numbers=chunk.get("pages", []),
                        progress_message=f"并行分析内容块 {index + 1}/{len(chunks)}（PDF 文字）",
                    )
            except _services().HTTPError as exc:
                # Some local OpenAI-compatible servers are text-only. Preserve the
                # analysis and expose this limitation instead of failing the run.
                if visual.get("images") and exc.code in {
                    400,
                    408,
                    413,
                    415,
                    422,
                    429,
                    500,
                    502,
                    503,
                    504,
                    524,
                }:
                    reason = f"多模态请求失败（HTTP {exc.code}），页面图像未获模型核验；本块已降级为文本分析"
                    visual.setdefault("failures", []).append(
                        {"pages": chunk.get("pages", []), "error": reason}
                    )
                    result = model_call(
                        "chunk_analysis_fallback",
                        "text",
                        lambda: _services().limited_text_model_request(
                            local_prompt,
                            payload,
                            timeout=120,
                            output_tokens=chunk_output_limit,
                            reasoning_effort=_services().MODEL_ANALYSIS_REASONING_EFFORT,
                        ),
                        len(chunk["text"]),
                        page_numbers=chunk.get("pages", []),
                        progress_message=f"视觉请求降级，并行分析内容块 {index + 1}/{len(chunks)}（PDF 文字）",
                    )
                    visual["images"] = []
                else:
                    raise
        except Exception as exc:
            mark_provider_failure(exc)
            truncated = any(
                marker in _services().clean_text(str(exc), 500)
                for marker in ("输出上限", "输出因", "截断")
            )
            if mode not in {"deep_read", "reading_card"}:
                raise
            if truncated:
                text = chunk["text"]
                midpoint = len(text) // 2
                boundaries = [
                    value
                    for value in (
                        text.rfind("\n\n", 0, midpoint),
                        text.find("\n\n", midpoint),
                    )
                    if value > 0
                ]
                balanced_boundaries = [
                    value
                    for value in boundaries
                    if len(text) // 4 <= value <= len(text) * 3 // 4
                ]
                split_at = (
                    min(balanced_boundaries, key=lambda value: abs(value - midpoint))
                    if balanced_boundaries
                    else midpoint
                )
            else:
                split_at = 0
            if (
                truncated
                and retry_depth < 2
                and split_at >= 1600
                and len(text) - split_at >= 1600
            ):
                if progress_callback:
                    progress_callback(
                        "chunk_retry",
                        None,
                        total_progress,
                        f"内容块 {index + 1} 输出被截断，正在缩小后重试",
                    )
                children = []
                for child_index, child_text in enumerate(
                    (text[:split_at].strip(), text[split_at:].strip())
                ):
                    child_pages = sorted(
                        {
                            int(value)
                            for value in _services().re.findall(
                                r"第\s*(\d+)\s*页", child_text
                            )
                        }
                    )
                    child = {
                        "text": child_text,
                        "pages": child_pages or list(chunk.get("pages", [])),
                        "page_refs": [
                            ref
                            for ref in chunk.get("page_refs", [])
                            if not child_pages
                            or int(ref.get("page") or 0) in child_pages
                        ],
                    }
                    children.append(
                        analyze_chunk(
                            index,
                            child,
                            retry_depth + 1,
                            allow_visual and child_index == 0,
                        )
                    )
                merged = {
                    "notes": [],
                    "visual_observations": [],
                    "evidence_spans": [],
                    "uncertainties": [],
                    "chunk_pages": list(chunk.get("pages", [])),
                    "visual_pages": [],
                }
                merged_visual = {
                    "failures": [],
                    "rendered_pages": 0,
                    "requested_pages": 0,
                    "images": [],
                }
                seen_images = set()
                duration_ms = 0.0
                for _, child_result, child_visual, child_duration in children:
                    duration_ms += child_duration
                    for key in (
                        "notes",
                        "visual_observations",
                        "evidence_spans",
                        "uncertainties",
                    ):
                        values = (
                            child_result.get(key)
                            if isinstance(child_result.get(key), list)
                            else []
                        )
                        merged[key].extend(values)
                    merged["visual_pages"].extend(child_result.get("visual_pages", []))
                    merged.setdefault("_chunk_errors", []).extend(
                        child_result.get("_chunk_errors", [])
                    )
                    merged_visual["failures"].extend(child_visual.get("failures", []))
                    merged_visual["rendered_pages"] += int(
                        child_visual.get("rendered_pages") or 0
                    )
                    merged_visual["requested_pages"] += int(
                        child_visual.get("requested_pages") or 0
                    )
                    for image in child_visual.get("images", []):
                        key = (image.get("attachment_id"), image.get("page"))
                        if key not in seen_images:
                            seen_images.add(key)
                            merged_visual["images"].append(image)
                return index, merged, merged_visual, duration_ms
            failure_detail = (
                _services().model_error_detail(exc, _services().model_api_key())
                if isinstance(exc, _services().HTTPError)
                else _services().clean_text(str(exc), 300)
            )
            failure_message = (
                f"内容块输出被截断：{failure_detail}"
                if truncated
                else f"内容块分析失败：{failure_detail}"
            )
            failure = {"pages": list(chunk.get("pages", [])), "error": failure_detail}
            uncertainty = f"本块未能完成分析，其他页面的可核验证据仍会保留：{_services().clean_text(failure_detail, 180)}"
            return (
                index,
                {
                    "notes": [],
                    "visual_observations": [],
                    "evidence_spans": [],
                    "uncertainties": [uncertainty],
                    "chunk_pages": list(chunk.get("pages", [])),
                    "visual_pages": [],
                    "_chunk_errors": [failure],
                },
                {
                    "failures": visual.get("failures", []),
                    "rendered_pages": visual.get("rendered_pages", 0),
                    "requested_pages": visual.get("requested_pages", 0),
                    "images": [],
                },
                round((_services().time.perf_counter() - request_started) * 1000, 2),
            )
        result["chunk_pages"] = chunk["pages"]
        result["visual_pages"] = [
            {"attachment_id": item.get("attachment_id"), "page": item.get("page")}
            for item in visual.get("images", [])
        ]
        result["_chunk_evidence_audit"] = _services().validate_ai_evidence(
            result,
            chunk_rows,
            {
                (str(item.get("attachment_id", "")), int(item.get("page") or 0))
                for item in visual.get("images", [])
            },
        )
        compact_visual = {
            "failures": visual.get("failures", []),
            "rendered_pages": visual.get("rendered_pages", 0),
            "requested_pages": visual.get("requested_pages", 0),
            "images": [
                {key: value for key, value in item.items() if key != "data_url"}
                for item in visual.get("images", [])
            ],
        }
        return (
            index,
            result,
            compact_visual,
            round((_services().time.perf_counter() - request_started) * 1000, 2),
        )
    return analyze_chunk

def _build_visual_page_analyzer(context):
    mark_provider_failure = context.mark_provider_failure
    model_call = context.model_call
    paper = context.paper
    provider_failure_event = context.provider_failure_event
    visual_page_prompt = context.visual_page_prompt
    def analyze_visual_page(item: tuple[int, dict]):
        index, row = item
        if provider_failure_event.is_set():
            return (
                index,
                row,
                {
                    "visual_observations": [],
                    "uncertainties": ["模型服务已被前一请求判定为不可用，跳过视觉分析"],
                },
                {
                    "failures": [],
                    "images": [],
                    "rendered_pages": 0,
                    "requested_pages": 0,
                },
            )
        visual = _services().prepare_visual_pages([row], max_pages=1)
        images = visual.get("images", []) if isinstance(visual, dict) else []
        if not images:
            return (
                index,
                row,
                {
                    "visual_observations": [],
                    "uncertainties": ["页面未能渲染为视觉输入"],
                },
                visual,
            )
        page = int(row.get("page_number") or 0)
        payload = {
            "paper": {"title": paper.get("title", ""), "mode": "deep_read_visual_page"},
            "pages": f"[附件 {str(row.get('attachment_id', ''))[:8]} 第 {page} 页]\n{_services().clean_text(row.get('text', ''), 12000)}",
            "visual_context": {
                "page_refs": [
                    {"attachment_id": row.get("attachment_id", ""), "page": page}
                ],
                "rendered_pages": 1,
            },
        }
        try:
            result = model_call(
                "visual_page_analysis",
                "multimodal",
                lambda: _services().limited_multimodal_model_request(
                    visual_page_prompt,
                    payload,
                    images,
                    timeout=150,
                    output_tokens=_services().MODEL_CHUNK_MAX_OUTPUT_TOKENS,
                    reasoning_effort=_services().MODEL_ANALYSIS_REASONING_EFFORT,
                ),
                len(payload["pages"]),
                len(images),
                page_numbers=[page],
                progress_message=f"正在识别 PDF 第 {page} 页的公式、图表和图片",
            )
            result = _services().unwrap_model_json_layers(result)
            if not isinstance(result, dict):
                result = {
                    "visual_observations": [],
                    "uncertainties": ["视觉响应不是 JSON 对象"],
                }
            result["visual_observations"] = _services().normalize_visual_observations(
                result, row
            )
            return index, row, result, visual
        except Exception as exc:
            mark_provider_failure(exc)
            detail = (
                _services().model_error_detail(exc, _services().model_api_key())
                if isinstance(exc, _services().HTTPError)
                else _services().clean_text(str(exc), 300)
            )
            visual.setdefault("failures", []).append(
                {
                    "attachment_id": row.get("attachment_id", ""),
                    "page": page,
                    "error": f"视觉页面分析失败：{detail}",
                }
            )
            # A rendered image is not visual coverage when the model never
            # accepted the request. Do not claim a failed page as analysed.
            visual["images"] = []
            return index, row, {"visual_observations": [], "uncertainties": []}, visual
    return analyze_visual_page

