"""Coordinate model driven web search requests and selection rounds."""

from __future__ import annotations

from ..services import app_services as _services

def _model_web_search_unbounded(
    query: str, limit: int = 20, options: dict | None = None
) -> dict:
    search_started = _services().time.perf_counter()
    settings = _services().resolve_model_route(_services().model_settings(), "fast")
    api_key = _services().model_api_key()
    if not api_key or not settings.get("enabled"):
        raise ValueError("模型搜索需要先启用模型并配置 API Key")
    options = options or {}
    try:
        target_limit = max(1, min(int(limit), 40))
    except (TypeError, ValueError):
        target_limit = 20
    protocol = settings.get("protocol", "chat_completions")
    seen_titles = {
        _services().title_key(value)
        for value in options.get("_seen_titles", [])
        if _services().title_key(value)
    }
    seen_identities = {
        _services().clean_text(value, 700)
        for value in options.get("_seen_identities", [])
        if _services().clean_text(value, 700)
    }
    preferred_sources = [
        source
        for source in options.get("_preferred_sources", [])
        if source in _services().SOURCE_SEARCH
    ]
    initial = {
        "research_question": _services().clean_text(query, 1000),
        "requested_limit": max(1, min(int(limit), 40)),
        "preferred_sources": preferred_sources,
        "available_sources": [
            {
                "id": source,
                "coverage": description,
                "status": (
                    "cooldown"
                    if _services()._model_search_source_cooldown(source)
                    else "available"
                ),
                **(
                    {
                        "retry_after_seconds": max(
                            1, round(_services()._model_search_source_cooldown(source))
                        )
                    }
                    if _services()._model_search_source_cooldown(source)
                    else {}
                ),
            }
            for source, description in _services().model_search_available_connectors().items()
        ],
        "web_discovery_available": bool(_services()._tavily_api_key()),
        "previously_seen_titles": [
            _services().clean_text(value, 300)
            for value in options.get("_seen_titles", [])
            if _services().clean_text(value, 300)
        ][:80],
        "filters": {
            key: options.get(key)
            for key in (
                "author",
                "venue",
                "year_from",
                "year_to",
                "work_type",
                "exclude_terms",
            )
            if options.get(key) not in (None, "", [])
        },
    }
    user_message = {
        "role": "user",
        "content": _services().json.dumps(initial, ensure_ascii=False),
    }
    conversation = [user_message]
    inventory, identity_index, executed_searches = {}, {}, []
    tool_call_count = model_request_count = repair_count = repeats_filtered = (
        no_tool_responses
    ) = 0
    model_retry_count = 0
    model_request_durations = []
    tool_call_durations = []
    # A compatible endpoint may stop after one tool call even when the user
    # requested a full page.  Give it one bounded opportunity to search a
    # different angle before accepting a short page; this avoids confusing
    # duplicate avoidance with a shortage of retrieval effort.
    short_selection_retries = 0
    # An otherwise valid ``{"results": []}`` is not a successful search when
    # every candidate was filtered out or the model marked every candidate as
    # unrelated. Give the agent one bounded opportunity to choose a genuinely
    # different query/source lane before surfacing an empty result to the UI.
    empty_selection_retries = 0
    # A first tool round often returns enough real candidates for the
    # requested page. In that case the next model request only needs to rank
    # the inventory; forcing another planning/tool round adds latency without
    # improving recall. Short/empty selections still use the existing bounded
    # retry path below.
    force_final_next = False
    last_error = None
    progress = options.get("_progress_callback")

    def report_progress(message: str):
        if callable(progress):
            try:
                progress(message)
            except Exception:
                pass

    def timing_payload() -> dict:
        return {
            "duration_ms": round(
                (_services().time.perf_counter() - search_started) * 1000, 2
            ),
            "model_request_durations_ms": list(model_request_durations),
            "tool_call_durations_ms": list(tool_call_durations),
            "model_request_total_ms": round(sum(model_request_durations), 2),
            "tool_call_total_ms": round(sum(tool_call_durations), 2),
        }

    def recover_from_inventory(reason: str) -> dict | None:
        """Keep real tool candidates when the final model JSON is unusable.

        Tool calls have already produced authoritative metadata at this point;
        losing it because a relay omitted ``results`` or emitted malformed
        JSON makes a repeat search look empty and incorrectly blames dedupe.
        The bounded local ranker is deliberately used only as recovery and
        marks every selected row as a local fallback for human review.
        """
        if not inventory:
            return None
        recovered, recovered_count = _services()._model_search_backfill_selection(
            [], inventory, set(), query, target_limit, options
        )
        if not recovered:
            return None
        normalized = _services().normalize_model_search_results(
            {"results": recovered},
            query,
            settings.get("provider", ""),
            settings.get("model", ""),
        )
        result_by_identity = {
            _services().candidate_identity(item): item for item in recovered
        }
        for item in normalized["results"]:
            original = result_by_identity.get(_services().candidate_identity(item), {})
            item["provider_sources"] = original.get("provider_sources", [])
            item["search_paths"] = original.get("search_paths", [])
            item["model_relevance_label"] = item.get("relevance_label", "uncertain")
            item["selection_backfilled"] = True
        warning = "模型最终筛选 JSON 无效，已使用本轮真实候选按本地相关性补位"
        if reason:
            warning += "：" + _services().clean_text(reason, 220)
        normalized.update(
            {
                "searches": executed_searches,
                "gaps": [warning],
                "next_queries": [],
                "provider": settings.get("provider", ""),
                "model": settings.get("model", ""),
                "attempts": model_request_count,
                "tool_calls": tool_call_count,
                "repair_count": repair_count,
                "retry_count": model_retry_count,
                "repeat_results_filtered": repeats_filtered,
                "backfill_count": recovered_count,
                "local_fill_count": 0,
                "recovered_from_model_error": True,
                "warnings": [
                    f"{summary['source']} 检索失败：{summary['error']}"
                    for search in executed_searches
                    for summary in search.get("sources", [])
                    if summary.get("error")
                ],
                "timing": timing_payload(),
            }
        )
        normalized["results"] = normalized["results"][:target_limit]
        normalized["ok"] = bool(normalized["results"])
        return normalized

    for turn in range(_services().MODEL_SEARCH_TOOL_MAX_TURNS):
        final_only = (
            force_final_next
            or turn == _services().MODEL_SEARCH_TOOL_MAX_TURNS - 1
            or tool_call_count >= _services().MODEL_SEARCH_TOOL_MAX_CALLS
        )
        force_final_next = False
        report_progress(
            "正在理解研究问题并规划搜索"
            if turn == 0
            else (
                "正在筛选论文并规划下一轮"
                if final_only
                else f"正在调用论文检索工具 · 第 {turn} 轮"
            )
        )
        payload = _services()._model_search_payload(
            settings, conversation, final_only=final_only
        )
        result = None
        for request_attempt in range(2):
            request_started = _services().time.perf_counter()
            try:
                model_request_count += 1
                with _services().model_usage_context("fast", "tools"):
                    with _services().open_compatible_model_request(
                        _services().model_endpoint(settings),
                        payload,
                        api_key,
                        timeout=75,
                    ) as response:
                        result = _services().read_model_response(response)
                break
            except _services().HTTPError as exc:
                detail = _services().model_error_detail(exc, api_key)
                folded = detail.casefold()
                unsupported = exc.code in {400, 404, 405, 422, 501} and any(
                    term in folded
                    for term in (
                        "tool",
                        "function",
                        "unsupported",
                        "unknown",
                        "not found",
                        "extra",
                    )
                )
                if unsupported:
                    raise _services().ModelWebSearchUnsupported(
                        "模型 API 已连接，但当前模型/中转端点不支持标准 function/tool calling，无法让模型调用论文检索工具。请切换到支持工具调用的模型或端点。"
                    ) from exc
                raise ValueError("模型论文检索请求失败：" + detail) from exc
            except (
                _services().URLError,
                TimeoutError,
                OSError,
                ValueError,
                _services().json.JSONDecodeError,
            ) as exc:
                last_error = exc
                if request_attempt == 0:
                    model_retry_count += 1
                    _services().time.sleep(0.25)
                    continue
                raise ValueError(
                    "模型论文检索请求失败：" + _services().clean_text(str(exc), 300)
                ) from exc
            finally:
                model_request_durations.append(
                    round((_services().time.perf_counter() - request_started) * 1000, 2)
                )
        if not isinstance(result, dict):
            raise ValueError("模型论文检索没有返回有效响应")

        calls = _services()._model_search_tool_calls(result, protocol)
        if calls and not final_only:
            _services()._model_search_append_assistant_turn(
                conversation, result, protocol
            )
            call_rows = []
            for call in calls:
                call_id = (
                    _services().clean_text(call.get("id"), 160)
                    or f"litloom-call-{tool_call_count + 1}"
                )
                if call.get("name") != _services().MODEL_SEARCH_TOOL_NAME:
                    call_rows.append(
                        (
                            call,
                            call_id,
                            {
                                "ok": False,
                                "error": f"未知工具 {_services().clean_text(call.get('name'), 100)}；请使用 {_services().MODEL_SEARCH_TOOL_NAME}",
                            },
                            None,
                        )
                    )
                elif tool_call_count >= _services().MODEL_SEARCH_TOOL_MAX_CALLS:
                    call_rows.append(
                        (
                            call,
                            call_id,
                            {
                                "ok": False,
                                "error": "已达到本次检索的工具调用上限；请根据已有候选输出最终 JSON",
                            },
                            None,
                        )
                    )
                else:
                    tool_call_count += 1
                    call_rows.append((call, call_id, None, None))
            pending = [
                (index, row[0].get("arguments"))
                for index, row in enumerate(call_rows)
                if row[2] is None
            ]
            if len(pending) > 1:
                report_progress(f"正在并行检索 {len(pending)} 个研究角度")
                with _services().ThreadPoolExecutor(
                    max_workers=min(3, len(pending)),
                    thread_name_prefix="model-search-angle",
                ) as pool:
                    future_started = {}
                    futures = {}
                    for index, arguments in pending:
                        future_started[index] = _services().time.perf_counter()
                        futures[index] = pool.submit(
                            _services()._model_search_prefetch,
                            arguments,
                            preferred_sources,
                            seen_titles,
                            seen_identities,
                            options.get("avoid_repeats", True),
                        )
                    for index, future in futures.items():
                        call, call_id, _, _ = call_rows[index]
                        call_rows[index] = (call, call_id, None, future.result())
                        tool_call_durations.append(
                            round(
                                (
                                    _services().time.perf_counter()
                                    - future_started[index]
                                )
                                * 1000,
                                2,
                            )
                        )
            for call, call_id, preset_output, prefetched in call_rows:
                tool_started = _services().time.perf_counter()
                output = preset_output or _services()._model_search_tool_result(
                    call.get("arguments"),
                    inventory,
                    identity_index,
                    seen_titles,
                    seen_identities,
                    options,
                    preferred_sources,
                    prefetched,
                )
                tool_call_durations.append(
                    round((_services().time.perf_counter() - tool_started) * 1000, 2)
                )
                if output.get("query"):
                    executed_searches.append(
                        {
                            "query": output["query"],
                            "sources": output.get("source_results", []),
                            "result_count": len(output.get("results", [])),
                            "excluded_previously_seen": output.get(
                                "excluded_previously_seen", 0
                            ),
                        }
                    )
                    repeats_filtered += int(output.get("excluded_previously_seen") or 0)
                serialized = _services().json.dumps(output, ensure_ascii=False)
                if protocol == "responses":
                    conversation.append(
                        {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": serialized,
                        }
                    )
                else:
                    conversation.append(
                        {"role": "tool", "tool_call_id": call_id, "content": serialized}
                    )
            # The tool inventory is already the authoritative candidate pool;
            # once it is comfortably larger than the requested page, move to
            # one final ranking request instead of asking the model to invent
            # another query angle. This keeps deeper expansion available when
            # the first round genuinely returns too little.
            try:
                # Once the tool inventory can fill the requested page, the
                # next model turn only needs to rank it.  Requiring a large
                # extra buffer here used to force another expensive planning
                # round for ordinary 6-12 result searches, even though the
                # local object/task gate and history fill already provide
                # bounded recovery for any rows rejected later.
                early_stop_target = max(
                    6,
                    int(target_limit)
                    + _services().MODEL_SEARCH_EARLY_STOP_BUFFER,
                )
            except (TypeError, ValueError):
                early_stop_target = 10
            if len(inventory) >= early_stop_target:
                force_final_next = True
            continue

        if not executed_searches:
            if (
                no_tool_responses < 1
                and turn < _services().MODEL_SEARCH_TOOL_MAX_TURNS - 1
            ):
                no_tool_responses += 1
                _services()._model_search_append_assistant_turn(
                    conversation, result, protocol
                )
                conversation.append(
                    {
                        "role": "user",
                        "content": f"你还没有调用 {_services().MODEL_SEARCH_TOOL_NAME}，所以没有检索到任何论文。现在必须先调用该函数搜索真实学术来源，再输出结果。",
                    }
                )
                continue
            raise _services().ModelWebSearchUnsupported(
                "模型 API 已连接，但模型没有发起论文检索工具调用；请确认当前模型支持并启用了 function/tool calling。"
            )

        try:
            raw = _services()._parse_model_json_result(result, allow_empty=True)
        except ValueError as exc:
            last_error = exc
            if repair_count < 1 and turn < _services().MODEL_SEARCH_TOOL_MAX_TURNS - 1:
                repair_count += 1
                _services()._model_search_append_assistant_turn(
                    conversation, result, protocol
                )
                conversation.append(
                    {
                        "role": "user",
                        "content": "最终筛选结果不是有效 JSON。请只从已经返回的 result_id 中选择，按约定输出完整 JSON；不要再次搜索，也不要编造元数据。",
                    }
                )
                continue
            # A provider can truncate only the final ranking response after
            # all connector calls have already completed.  The real candidate
            # inventory is still valid, so keep the search useful by ranking
            # that inventory locally instead of discarding the whole run.
            # Fresh candidates are preferred and historical candidates remain
            # available as transparent fill unless suppress_seen was requested.
            if inventory and ("截断" in str(exc) or repair_count >= 1):
                recovered = recover_from_inventory(str(exc))
                if recovered:
                    return recovered
            raise ValueError(
                "论文已检索到，但模型最终相关性筛选输出无效："
                + _services().clean_text(str(exc), 250)
            ) from exc

        raw_results = _services()._model_search_selection_rows(raw)
        if raw_results is None:
            if repair_count < 1 and turn < _services().MODEL_SEARCH_TOOL_MAX_TURNS - 1:
                repair_count += 1
                _services()._model_search_append_assistant_turn(
                    conversation, result, protocol
                )
                conversation.append(
                    {
                        "role": "user",
                        "content": "JSON 缺少 results 数组。请根据已有论文候选 ID 输出 results、gaps、next_queries，不要重新搜索。",
                    }
                )
                continue
            recovered = recover_from_inventory("模型返回 JSON 缺少 results 数组")
            if recovered:
                return recovered
            fields = (
                ", ".join(sorted(_services().clean_text(key, 80) for key in raw)[:20])
                or "无"
            )
            raise ValueError(
                f"模型检索结果缺少可识别的论文候选列表（返回字段：{fields}）"
            )
        selected, selected_ids, invalid_ids = [], set(), 0
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            candidate_id = _services().clean_text(
                item.get("result_id") or item.get("id"), 40
            )
            candidate = inventory.get(candidate_id)
            if not candidate:
                invalid_ids += 1
                continue
            identity = _services().candidate_identity(candidate)
            if candidate_id in selected_ids or identity in selected_ids:
                continue
            selected_ids.add(candidate_id)
            label = _services().clean_text(item.get("relevance_label"), 30).casefold()
            label = (
                label
                if label in {"direct", "adjacent", "uncertain", "unrelated"}
                else "uncertain"
            )
            if label == "unrelated":
                continue
            if candidate.get("discovery_only"):
                label = "uncertain"
            selected.append(
                {
                    **candidate,
                    "relevance_label": label,
                    "relevance_reason": _services().clean_text(
                        item.get("relevance_reason"), 500
                    ),
                    "source_citations": candidate.get("source_citations")
                    or [candidate.get("url", "")],
                }
            )
        # A model may correctly choose only a small set even though the user
        # requested a full page.  Fill from candidates already returned by the
        # search tools, preferring fresh papers and then historical papers.
        # This runs whenever the model emits a final JSON, including an early
        # final response before the turn budget is exhausted.  Otherwise an
        # endpoint that stops after one ranking turn would make the visible
        # page look short simply because repeat avoidance influenced ranking.
        # Strict fresh-only mode still excludes historical candidates, while
        # the normal model-web quality gate validates object/task evidence
        # after this bounded refill.
        backfill_count = 0
        if len(selected) < target_limit and (selected or final_only):
            selected, backfill_count = _services()._model_search_backfill_selection(
                selected, inventory, selected_ids, query, target_limit, options
            )
        if (
            invalid_ids
            and not selected
            and repair_count < 1
            and turn < _services().MODEL_SEARCH_TOOL_MAX_TURNS - 1
        ):
            repair_count += 1
            _services()._model_search_append_assistant_turn(
                conversation, result, protocol
            )
            conversation.append(
                {
                    "role": "user",
                    "content": "你引用了不存在的 result_id。只能从工具结果中选择已出现的 ID；请重做筛选 JSON。",
                }
            )
            continue
        if (
            bool(selected)
            and len(selected) < target_limit
            and short_selection_retries < 1
            and empty_selection_retries == 0
            and not final_only
            and tool_call_count < _services().MODEL_SEARCH_TOOL_MAX_CALLS
            and turn < _services().MODEL_SEARCH_TOOL_MAX_TURNS - 1
        ):
            short_selection_retries += 1
            _services()._model_search_append_assistant_turn(
                conversation, result, protocol
            )
            conversation.append(
                {
                    "role": "user",
                    "content": (
                        f"当前只得到 {len(selected)} 篇候选，但用户请求 {target_limit} 篇。"
                        "不要因为已见论文或模型主筛选而缩小结果页；请换一个尚未使用的研究角度、英文同义词或学术来源，"
                        f"再次调用 {_services().MODEL_SEARCH_TOOL_NAME} 补充真实候选。优先返回此前没有出现的论文，仍需遵守所有过滤条件。"
                    ),
                }
            )
            continue
        if (
            not selected
            and empty_selection_retries < 1
            and not final_only
            and tool_call_count < _services().MODEL_SEARCH_TOOL_MAX_CALLS
            and turn < _services().MODEL_SEARCH_TOOL_MAX_TURNS - 1
        ):
            empty_selection_retries += 1
            _services()._model_search_append_assistant_turn(
                conversation, result, protocol
            )
            conversation.append(
                {
                    "role": "user",
                    "content": (
                        "当前候选没有一篇被选入 results。不要结束检索，也不要输出空 results。"
                        "请换一个明确的检索角度、英文同义词或学术来源，再次调用 "
                        f"{_services().MODEL_SEARCH_TOOL_NAME}；必须避开已经返回的候选和本课题已见论文。"
                    ),
                }
            )
            continue
        local_fill_count = 0
        # A short final answer is often caused by a model that ranked only a
        # few IDs, or by a repeated topic whose first tool pages overlap.  A
        # bounded connector-only refill keeps the requested page size stable
        # without asking the model for another expensive turn.  The helper
        # runs only after the model had a chance to search another angle or
        # when the turn budget is exhausted, so ordinary successful searches
        # keep their fast path.
        follow_up_hints = bool(
            (raw.get("next_queries") if isinstance(raw, dict) else None)
            or (raw.get("gaps") if isinstance(raw, dict) else None)
        )
        if len(selected) < target_limit and (
            final_only
            # A repeat-heavy tool page is a retrieval shortfall even when
            # the model omits ``gaps``/``next_queries`` in its final JSON.
            # Give the bounded connector refill a chance in that case;
            # otherwise deduplication can still make a requested page
            # look artificially small.
            or repeats_filtered > 0
            or (
                follow_up_hints
                and (short_selection_retries > 0 or empty_selection_retries > 0)
            )
        ):
            (
                selected,
                local_added,
                local_fill_count,
            ) = _services()._model_search_local_fill(
                selected,
                inventory,
                identity_index,
                selected_ids,
                query,
                target_limit,
                options,
                preferred_sources,
                seen_titles,
                seen_identities,
                executed_searches,
            )
            backfill_count += local_added
        normalized = _services().normalize_model_search_results(
            {"results": selected},
            query,
            settings.get("provider", ""),
            settings.get("model", ""),
        )
        result_by_identity = {
            _services().candidate_identity(item): item for item in selected
        }
        for item in normalized["results"]:
            original = result_by_identity.get(_services().candidate_identity(item), {})
            item["provider_sources"] = original.get("provider_sources", [])
            item["search_paths"] = original.get("search_paths", [])
            item["model_relevance_label"] = item.get("relevance_label", "uncertain")
            item["selection_backfilled"] = bool(
                original.get("_selection_backfilled")
                or original.get("selection_backfilled")
            )
        normalized.update(
            {
                "searches": executed_searches,
                "gaps": [
                    _services().clean_text(value, 500)
                    for value in (
                        raw.get("gaps") if isinstance(raw.get("gaps"), list) else []
                    )
                    if _services().clean_text(value, 500)
                ][:12],
                "next_queries": [
                    _services().clean_text(value, 500)
                    for value in (
                        raw.get("next_queries")
                        if isinstance(raw.get("next_queries"), list)
                        else []
                    )
                    if _services().clean_text(value, 500)
                ][:12],
                "provider": settings.get("provider", ""),
                "model": settings.get("model", ""),
                "attempts": model_request_count,
                "tool_calls": tool_call_count,
                "repair_count": repair_count,
                "retry_count": model_retry_count,
                "repeat_results_filtered": repeats_filtered,
                "backfill_count": backfill_count,
                "local_fill_count": local_fill_count,
                "warnings": [
                    f"{summary['source']} 检索失败：{summary['error']}"
                    for search in executed_searches
                    for summary in search.get("sources", [])
                    if summary.get("error")
                ],
                "timing": timing_payload(),
            }
        )
        normalized["results"] = normalized["results"][:target_limit]
        if not normalized["results"] and not inventory:
            failures = [
                summary
                for search in executed_searches
                for summary in search.get("sources", [])
                if summary.get("error")
            ]
            detail = "; ".join(
                f"{item['source']}: {item['error']}" for item in failures[:3]
            )
            if repeats_filtered:
                detail = (
                    detail + "; " if detail else ""
                ) + "来源命中均为本主题此前已见论文"
            if not detail:
                detail = "所选学术来源没有返回带有效题名和论文链接的新候选"
            raise ValueError("AI 工具检索没有获得可用的新论文：" + detail)
        normalized["ok"] = bool(normalized["results"])
        return normalized
    raise ValueError("模型论文检索超过工具调用轮数上限；请缩小研究问题后重试")


def model_web_search(query: str, limit: int = 20, options: dict | None = None) -> dict:
    return _services()._with_model_request_budget(
        lambda: _services()._model_web_search_unbounded(query, limit, options)
    )


__all__ = ['_model_web_search_unbounded', 'model_web_search']
