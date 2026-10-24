"""Concurrent text and visual page execution for full-text analysis."""

from __future__ import annotations

from types import SimpleNamespace

from ..services import app_services as _services


def _run_text_and_visual_workers(context):
    analyze_chunk = context.analyze_chunk
    analyze_visual_page = context.analyze_visual_page
    chunks = context.chunks
    mode = context.mode
    mark_provider_failure = context.mark_provider_failure
    model_call = context.model_call
    paper = context.paper
    progress_callback = context.progress_callback
    provider_failure_event = context.provider_failure_event
    rows = context.rows
    total_progress = context.total_progress
    visual_failures = context.visual_failures
    visual_page_items = context.visual_page_items
    visual_recheck_budget = context.visual_recheck_budget
    visual_requested = context.visual_requested

    # Text chunks and focused visual pages are independent evidence lanes.
    # Run both in one bounded pool so a slow text response does not hold the
    # visual lane idle (and vice versa). The model semaphore remains the final
    # provider-wide concurrency limit, and results are merged in source order.
    chunk_results = [None] * len(chunks)
    visual_additions = [[] for _ in chunks]
    worker_count = min(
        _services().FULLTEXT_CONCURRENCY,
        _services().MODEL_CONCURRENCY,
        max(1, len(chunks) + len(visual_page_items)),
    )
    completed_chunks = 0
    completed_visual_pages = 0
    if visual_page_items and progress_callback:
        progress_callback(
            "visual_analysis",
            0,
            total_progress,
            f"正文与 {len(visual_page_items)} 个重点页面将并行分析",
        )
    with _services().ThreadPoolExecutor(
        max_workers=worker_count, thread_name_prefix="fulltext-ai"
    ) as pool:
        # Interleave the two lanes in the FIFO queue. Queueing every visual
        # page first would starve text when a long paper has more visual pages
        # than worker slots; queueing every text chunk first has the opposite
        # problem. Interleaving guarantees both lanes get an initial slot.
        future_kinds = {}
        for order in range(max(len(chunks), len(visual_page_items))):
            if order < len(visual_page_items):
                future_kinds[
                    pool.submit(analyze_visual_page, visual_page_items[order])
                ] = ("visual", order)
            if order < len(chunks):
                future_kinds[pool.submit(analyze_chunk, order, chunks[order])] = (
                    "chunk",
                    order,
                )
        for future in _services().as_completed(future_kinds):
            kind, order = future_kinds[future]
            if kind == "chunk":
                index, result, visual, chunk_duration_ms = future.result()
                chunk_results[index] = (result, visual)
                completed_chunks += 1
                if progress_callback:
                    progress_callback(
                        "chunk_analysis",
                        completed_chunks,
                        total_progress,
                        f"已完成内容块 {completed_chunks}/{len(chunks)}；最近块响应 {chunk_duration_ms / 1000:.1f} 秒",
                    )
                continue

            index, row, visual_result, visual = future.result()
            compact_visual = {
                "failures": (
                    visual.get("failures", []) if isinstance(visual, dict) else []
                ),
                "rendered_pages": (
                    int(visual.get("rendered_pages") or 0)
                    if isinstance(visual, dict)
                    else 0
                ),
                "requested_pages": (
                    int(visual.get("requested_pages") or 0)
                    if isinstance(visual, dict)
                    else 0
                ),
                "images": [
                    {key: value for key, value in image.items() if key != "data_url"}
                    for image in (
                        visual.get("images", []) if isinstance(visual, dict) else []
                    )
                ],
            }
            visual_additions[index].append((order, visual_result, compact_visual))
            completed_visual_pages += 1
            if progress_callback:
                progress_callback(
                    "visual_analysis",
                    completed_visual_pages,
                    total_progress,
                    f"已完成视觉页面 {completed_visual_pages}/{len(visual_page_items)}（第 {int(row.get('page_number') or 0)} 页）",
                )

    for index, additions in enumerate(visual_additions):
        if not additions:
            continue
        source_result, source_visual = chunk_results[index]
        for _order, visual_result, visual in sorted(
            additions, key=lambda item: item[0]
        ):
            observations = (
                visual_result.get("visual_observations", [])
                if isinstance(visual_result, dict)
                else []
            )
            if observations:
                source_result.setdefault("visual_observations", []).extend(observations)
            uncertainties = (
                visual_result.get("uncertainties", [])
                if isinstance(visual_result, dict)
                else []
            )
            if uncertainties:
                source_result.setdefault("uncertainties", []).extend(uncertainties)
            source_visual.setdefault("failures", []).extend(visual["failures"])
            source_visual["rendered_pages"] = (
                int(source_visual.get("rendered_pages") or 0) + visual["rendered_pages"]
            )
            source_visual["requested_pages"] = (
                int(source_visual.get("requested_pages") or 0)
                + visual["requested_pages"]
            )
            source_visual.setdefault("images", []).extend(visual["images"])
    visual_recheck_count = 0
    visual_recheck_attempt_count = 0
    if mode == "deep_read" and visual_requested and visual_recheck_budget > 0:
        # A provider can successfully receive an image yet return only text
        # evidence. Recheck only pages with explicit figure/table/equation
        # cues; this keeps the normal path unchanged for prose-only pages.
        recheck_candidates = []
        recheck_seen = set()
        for index, chunk in enumerate(chunks):
            result, visual = chunk_results[index]
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
            for row in _services().select_visual_recheck_rows(
                chunk_rows, result, visual, visual_recheck_budget
            ):
                key = (
                    str(row.get("attachment_id", "")),
                    int(row.get("page_number") or 0),
                )
                if key not in recheck_seen:
                    recheck_seen.add(key)
                    recheck_candidates.append((index, row))
        recheck_candidates = recheck_candidates[:visual_recheck_budget]
        visual_recheck_attempt_count = len(recheck_candidates)
        if recheck_candidates and progress_callback:
            progress_callback(
                "visual_recheck",
                0,
                total_progress,
                f"发现 {len(recheck_candidates)} 页视觉证据缺口，正在有界补查",
            )

        recheck_prompt = (
            "你是论文页面视觉证据补查器。初次分析没有返回该页的视觉观察；现在只检查给定 PDF 页面图像，"
            "不要复述整篇论文，也不要根据常识补全。重点确认公式、图、表、流程图、坐标轴、图例和可读数字；看不清就明确写无法从图像确认。"
            "只输出严格 JSON 对象：{visual_observations:[{type:'formula'|'figure'|'table'|'diagram'|'image',"
            "attachment_id:string,page:number,title:string,description:string,transcription_latex:string,data_relationships:string,confidence:'high'|'medium'|'low'}],"
            "uncertainties:[string]}。观察必须对应输入页面，禁止编造页码、数值或公式。"
        )

        def recheck_visual_page(item: tuple[int, dict]):
            index, row = item
            if provider_failure_event.is_set():
                return (
                    index,
                    row,
                    {
                        "visual_observations": [],
                        "uncertainties": [
                            "模型服务已被前一请求判定为不可用，跳过视觉补查"
                        ],
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
                        "uncertainties": ["页面补查未能渲染图像"],
                    },
                    visual,
                )
            payload = {
                "paper": {
                    "title": paper.get("title", ""),
                    "mode": "deep_read_visual_recheck",
                },
                "pages": f"[附件 {str(row.get('attachment_id', ''))[:8]} 第 {int(row.get('page_number') or 0)} 页]\n{_services().clean_text(row.get('text', ''), 12000)}",
                "visual_context": {
                    "page_refs": [
                        {
                            "attachment_id": row.get("attachment_id", ""),
                            "page": int(row.get("page_number") or 0),
                        }
                    ],
                    "rendered_pages": 1,
                },
            }
            try:
                checked = model_call(
                    "visual_recheck",
                    "multimodal",
                    lambda: _services().limited_multimodal_model_request(
                        recheck_prompt,
                        payload,
                        images,
                        timeout=150,
                        output_tokens=_services().MODEL_CHUNK_MAX_OUTPUT_TOKENS,
                        reasoning_effort=_services().MODEL_ANALYSIS_REASONING_EFFORT,
                    ),
                    len(payload["pages"]),
                    len(images),
                    page_numbers=[int(row.get("page_number") or 0)],
                    progress_message=f"正在核验 PDF 第 {int(row.get('page_number') or 0)} 页的公式、图表和图片",
                )
                checked = _services().unwrap_model_json_layers(checked)
                if not isinstance(checked, dict):
                    checked = {
                        "visual_observations": [],
                        "uncertainties": ["视觉补查响应不是 JSON 对象"],
                    }
                normalized = _services().normalize_visual_observations(checked, row)
                checked["visual_observations"] = normalized
                return index, row, checked, visual
            except Exception as exc:
                mark_provider_failure(exc)
                detail = (
                    _services().model_error_detail(exc, _services().model_api_key())
                    if isinstance(exc, _services().HTTPError)
                    else _services().clean_text(str(exc), 240)
                )
                visual.setdefault("failures", []).append(
                    {
                        "attachment_id": row.get("attachment_id", ""),
                        "page": int(row.get("page_number") or 0),
                        "error": f"视觉补查失败：{detail}",
                    }
                )
                visual["images"] = []
                return (
                    index,
                    row,
                    {"visual_observations": [], "uncertainties": []},
                    visual,
                )

        if recheck_candidates:
            with _services().ThreadPoolExecutor(
                max_workers=min(
                    _services().FULLTEXT_CONCURRENCY,
                    _services().MODEL_CONCURRENCY,
                    len(recheck_candidates),
                ),
                thread_name_prefix="visual-recheck",
            ) as pool:
                futures = [
                    pool.submit(recheck_visual_page, item)
                    for item in recheck_candidates
                ]
                for completed, future in enumerate(
                    _services().as_completed(futures), 1
                ):
                    index, row, checked, visual = future.result()
                    source_result, source_visual = chunk_results[index]
                    observations = (
                        checked.get("visual_observations", [])
                        if isinstance(checked, dict)
                        else []
                    )
                    if observations:
                        source_result.setdefault("visual_observations", [])
                        source_result["visual_observations"].extend(observations)
                        visual_recheck_count += 1
                        # The first request may have failed transiently (for
                        # example an empty relay response).  Once the bounded
                        # recheck produced usable observations, remove that
                        # page's stale first-attempt failure so a recovered
                        # page is not reported as permanently unavailable.
                        recovered_key = (
                            str(row.get("attachment_id", "")),
                            int(row.get("page_number") or 0),
                        )
                        prior_failures = (
                            source_visual.get("failures")
                            if isinstance(source_visual.get("failures"), list)
                            else []
                        )
                        source_visual["failures"] = [
                            item
                            for item in prior_failures
                            if not (
                                isinstance(item, dict)
                                and (
                                    str(item.get("attachment_id", "")),
                                    int(item.get("page") or 0),
                                )
                                == recovered_key
                                and "视觉页面分析失败"
                                in _services().clean_text(item.get("error", ""), 500)
                            )
                        ]
                    visual_failures.extend(
                        visual.get("failures", []) if isinstance(visual, dict) else []
                    )
                    if progress_callback:
                        progress_callback(
                            "visual_recheck",
                            completed,
                            total_progress,
                            f"已完成视觉补查 {completed}/{len(recheck_candidates)}（第 {int(row.get('page_number') or 0)} 页）",
                        )

    return SimpleNamespace(
        chunk_results=chunk_results,
        visual_recheck_count=visual_recheck_count,
        visual_recheck_attempt_count=visual_recheck_attempt_count,
    )
