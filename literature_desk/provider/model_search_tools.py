"""Model search connector schemas, tool calls, and source execution."""

from __future__ import annotations

from ..services import app_services as _services
from .public_sources import _deepseek_search_endpoint

def _model_search_source_cooldown(source: str) -> float:
    """Return remaining cooldown for a model-search connector, if any."""
    source = str(source or "").strip().casefold()
    probe = (
        _deepseek_search_endpoint("search")
        if source == "deepseek_search"
        else _services().MODEL_SEARCH_SOURCE_PROBE_URLS.get(source)
    )
    return _services()._public_source_cooldown_remaining(probe) if probe else 0.0


def _model_search_source_error(source: str) -> str:
    """Describe a circuit-open source without issuing another network call."""
    remaining = _services()._model_search_source_cooldown(source)
    if not remaining:
        return ""
    probe = (
        _deepseek_search_endpoint("search")
        if source == "deepseek_search"
        else _services().MODEL_SEARCH_SOURCE_PROBE_URLS.get(source, source)
    )
    host = _services()._public_source_key(probe)
    return f"{host} 暂时限流，约 {max(1, round(remaining))} 秒后重试；本轮已跳过"


def _model_search_tool_schema(protocol: str) -> dict:
    """Build standard OpenAI-compatible function schemas for both protocols."""
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "本轮独立的学术检索式，优先使用英文关键词和同义表达。",
            },
            "sources": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": list(_services().model_search_available_connectors()),
                },
                "minItems": 1,
                "maxItems": _services().MODEL_SEARCH_TOOL_MAX_SOURCES,
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": _services().MODEL_SEARCH_TOOL_MAX_LIMIT,
            },
        },
        "required": ["query", "sources", "limit"],
        "additionalProperties": False,
    }
    if protocol == "responses":
        return {
            "type": "function",
            "name": _services().MODEL_SEARCH_TOOL_NAME,
            "description": "在可用学术论文数据库中检索真实论文元数据和来源链接。模型负责决定查询角度与来源。",
            "parameters": parameters,
        }
    return {
        "type": "function",
        "function": {
            "name": _services().MODEL_SEARCH_TOOL_NAME,
            "description": "在可用学术论文数据库中检索真实论文元数据和来源链接。模型负责决定查询角度与来源。",
            "parameters": parameters,
        },
    }


def _model_search_payload(
    settings: dict, conversation: list, final_only: bool = False
) -> dict:
    payload = {"model": settings["model"], "temperature": 0, "stream": False}
    if settings.get("protocol") == "responses":
        payload.update(
            {
                "instructions": _services().MODEL_WEB_SEARCH_PROMPT,
                "input": conversation,
                "store": False,
            }
        )
        if _services().MODEL_SEARCH_MAX_OUTPUT_TOKENS:
            payload["max_output_tokens"] = _services().MODEL_SEARCH_MAX_OUTPUT_TOKENS
    else:
        payload.update(
            {
                "messages": [
                    {"role": "system", "content": _services().MODEL_WEB_SEARCH_PROMPT},
                    *conversation,
                ]
            }
        )
        if _services().MODEL_SEARCH_MAX_OUTPUT_TOKENS:
            payload["max_tokens"] = _services().MODEL_SEARCH_MAX_OUTPUT_TOKENS
    # The final turn is a machine-readable ranking response.  Ask compatible
    # OpenAI-style relays for JSON explicitly so a prose wrapper or Markdown
    # fence does not trigger an avoidable repair request.  Tool turns keep the
    # normal function-calling payload; some gateways reject combining tools
    # and JSON mode even though they support each feature independently.
    if final_only:
        _services().add_json_output_format(payload, settings)
    if _services().MODEL_SEARCH_REASONING_EFFORT:
        if settings.get("protocol") == "responses":
            payload["reasoning"] = {"effort": _services().MODEL_SEARCH_REASONING_EFFORT}
        else:
            payload["reasoning_effort"] = _services().MODEL_SEARCH_REASONING_EFFORT
    if not final_only:
        payload["tools"] = [
            _services()._model_search_tool_schema(
                settings.get("protocol", "chat_completions")
            )
        ]
        # Require the first turn to actually use the retrieval tool. Later
        # turns can choose whether another angle is useful; an initial prose
        # answer only adds a round trip and cannot contain source-backed IDs.
        payload["tool_choice"] = "required" if len(conversation) == 1 else "auto"
    return payload


def _model_search_tool_calls(result: dict, protocol: str) -> list[dict]:
    calls = []
    if protocol == "responses":
        for item in (
            result.get("output", []) if isinstance(result.get("output"), list) else []
        ):
            if isinstance(item, dict) and item.get("type") == "function_call":
                calls.append(
                    {
                        "id": item.get("call_id") or item.get("id") or "",
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments", ""),
                    }
                )
        return calls
    choices = result.get("choices") or []
    message = (
        choices[0].get("message", {})
        if choices and isinstance(choices[0], dict)
        else {}
    )
    for item in message.get("tool_calls", []) if isinstance(message, dict) else []:
        function = item.get("function") if isinstance(item, dict) else None
        if isinstance(function, dict):
            calls.append(
                {
                    "id": item.get("id") or "",
                    "name": function.get("name") or "",
                    "arguments": function.get("arguments", ""),
                }
            )
    return calls


def _model_search_append_assistant_turn(
    conversation: list, result: dict, protocol: str
) -> None:
    if protocol == "responses":
        output = result.get("output")
        if isinstance(output, list):
            conversation.extend(output)
        elif _services().model_response_content(result).strip():
            conversation.append(
                {
                    "role": "assistant",
                    "content": _services().model_response_content(result),
                }
            )
        return
    choices = result.get("choices") or []
    message = (
        choices[0].get("message", {})
        if choices and isinstance(choices[0], dict)
        else {}
    )
    if not isinstance(message, dict):
        return
    assistant = {"role": "assistant"}
    if message.get("content") is not None:
        assistant["content"] = message["content"]
    if isinstance(message.get("tool_calls"), list):
        assistant["tool_calls"] = message["tool_calls"]
    conversation.append(assistant)


def _model_search_selection_rows(raw: dict) -> list[dict] | None:
    """Normalize common final-selection shapes without trusting model metadata."""
    if not isinstance(raw, dict):
        return None
    aliases = (
        "results",
        "selected_results",
        "relevant_results",
        "screened_results",
        "selected_papers",
        "relevant_papers",
        "ranked_results",
        "papers_to_keep",
        "selection",
        "selected",
        "papers",
        "items",
        "judgments",
        "筛选结果",
        "相关论文",
        "论文列表",
        "结果",
    )
    values = next(
        (
            raw.get(key)
            for key in aliases
            if isinstance(raw.get(key), (list, dict, str))
        ),
        None,
    )
    if isinstance(values, str):
        # Some OpenAI-compatible relays JSON-encode a nested answer as a
        # string even when the outer response is in JSON mode.  Decode only
        # that selected field; the caller still validates every candidate ID.
        try:
            nested = _services().parse_model_json(values)
        except ValueError:
            nested = None
        if isinstance(nested, dict):
            nested_rows = _services()._model_search_selection_rows(nested)
            if nested_rows is not None:
                return nested_rows
        return None
    if isinstance(values, dict):
        rows = []
        for result_id, value in values.items():
            item = (
                dict(value) if isinstance(value, dict) else {"relevance_label": value}
            )
            item.setdefault("result_id", result_id)
            rows.append(item)
        return rows
    if isinstance(values, list):
        rows = []
        for value in values:
            if isinstance(value, dict):
                item = dict(value)
                item.setdefault(
                    "result_id",
                    item.get("candidate_id")
                    or item.get("paper_id")
                    or item.get("candidate")
                    or item.get("文献ID")
                    or "",
                )
                item.setdefault(
                    "relevance_label",
                    item.get("label")
                    or item.get("relevance")
                    or item.get("relation")
                    or item.get("判断")
                    or "uncertain",
                )
                item.setdefault(
                    "relevance_reason",
                    item.get("reason")
                    or item.get("explanation")
                    or item.get("判断依据")
                    or "",
                )
                rows.append(item)
            elif isinstance(value, (str, int)):
                rows.append(
                    {
                        "result_id": str(value),
                        "relevance_label": "uncertain",
                        "relevance_reason": "模型选择的候选 ID；相关性理由未返回。",
                    }
                )
        return rows
    id_aliases = (
        "result_ids",
        "selected_ids",
        "candidate_ids",
        "relevant_ids",
        "入选ID",
        "候选ID",
    )
    selected_ids = next(
        (raw.get(key) for key in id_aliases if isinstance(raw.get(key), list)), None
    )
    if selected_ids is not None:
        return [
            {
                "result_id": str(value),
                "relevance_label": "uncertain",
                "relevance_reason": "模型选择的候选 ID；相关性理由未返回。",
            }
            for value in selected_ids
            if isinstance(value, (str, int))
        ]
    # Some compatible endpoints collapse a one-item JSON array into the item
    # itself. Accept that shape, while the caller still validates the ID
    # against the candidates actually returned by the search tools.
    single_id = next(
        (
            raw.get(key)
            for key in ("result_id", "candidate_id", "paper_id", "candidate", "文献ID")
            if isinstance(raw.get(key), (str, int))
        ),
        None,
    )
    if single_id is not None:
        return [
            {
                "result_id": str(single_id),
                "relevance_label": raw.get("relevance_label")
                or raw.get("label")
                or raw.get("relevance")
                or raw.get("判断")
                or "uncertain",
                "relevance_reason": raw.get("relevance_reason")
                or raw.get("reason")
                or raw.get("explanation")
                or raw.get("判断依据")
                or "",
            }
        ]
    return None


def _model_search_tool_arguments(
    arguments: object, preferred_sources: list[str]
) -> tuple[dict | None, list[str], str]:
    """Decode a model tool call and resolve its bounded connector set."""
    if isinstance(arguments, str):
        try:
            arguments = _services().json.loads(arguments)
        except (TypeError, ValueError):
            return None, [], "工具参数不是有效 JSON 对象"
    if not isinstance(arguments, dict):
        return None, [], "工具参数必须是 JSON 对象"
    query = _services().clean_text(arguments.get("query", ""), 500)
    if not query:
        return arguments, [], "query 不能为空；请提供英文关键词或同义检索表达"
    requested_sources = arguments.get("sources", [])
    if isinstance(requested_sources, str):
        requested_sources = [requested_sources]
    selected_sources = (
        list(
            dict.fromkeys(
                source
                for source in requested_sources
                if source in _services().SOURCE_SEARCH
                and source in _services().model_search_available_connectors()
            )
        )
        if isinstance(requested_sources, list)
        else []
    )
    # A model may keep selecting a source after a previous tool call opened its
    # cooldown circuit.  Prefer the still-available sources from that request;
    # if every requested source is unavailable, retain the request so the tool
    # result can expose the exact cooldown diagnostic to the model.
    if selected_sources:
        available = [
            source
            for source in selected_sources
            if not _services()._model_search_source_cooldown(source)
        ]
        if available:
            selected_sources = available
    if not selected_sources:
        default_sources = [*preferred_sources, "openalex", "arxiv", "crossref"]
        if "tavily" in _services().model_search_available_connectors():
            default_sources.insert(0, "tavily")
        if "deepseek_search" in _services().model_search_available_connectors():
            default_sources.insert(0, "deepseek_search")
        deduped = list(dict.fromkeys(default_sources))
        available = [
            source
            for source in deduped
            if not _services()._model_search_source_cooldown(source)
        ]
        # Keep one unavailable source only when the whole connector set is in
        # cooldown; otherwise it would add latency without improving recall.
        selected_sources = (available or deduped)[
            : _services().MODEL_SEARCH_TOOL_MAX_SOURCES
        ]
    return arguments, selected_sources[: _services().MODEL_SEARCH_TOOL_MAX_SOURCES], ""


def _fast_model_search_source_call(
    source: str, query: str, limit: int, offset: int = 0
):
    """Call one model-search connector with the short source budget."""
    with _services().fast_model_search_source_budget():
        try:
            return _services().SOURCE_SEARCH[source](query, limit, offset)
        except TypeError:
            # Keep compatibility with simple custom/test connectors that only
            # implement the original two-argument signature.
            return _services().SOURCE_SEARCH[source](query, limit)


def _model_search_prefetch(
    arguments: object,
    preferred_sources: list[str],
    seen_titles: set[str] | None = None,
    seen_identities: set[str] | None = None,
    avoid_repeats: bool = True,
) -> list[tuple[str, list[dict], str]] | None:
    """Fetch one model-selected query's sources concurrently, before merging.

    A repeated topic often gets a connector page dominated by papers already
    seen in the same research question. Fetch one bounded continuation page
    only when at least half of that first page is historical. New searches
    therefore keep the original one-page latency, while repeat searches have
    enough recall to fill the requested result page.
    """
    decoded, selected_sources, error = _services()._model_search_tool_arguments(
        arguments, preferred_sources
    )
    if error or not decoded:
        return None
    try:
        requested_limit = int(
            decoded.get("limit", _services().MODEL_SEARCH_TOOL_MAX_LIMIT)
        )
    except (TypeError, ValueError):
        requested_limit = _services().MODEL_SEARCH_TOOL_MAX_LIMIT
    result_limit = min(_services().MODEL_SEARCH_TOOL_MAX_LIMIT, max(1, requested_limit))

    seen_titles = {
        _services().title_key(value)
        for value in (seen_titles or set())
        if _services().title_key(value)
    }
    seen_identities = {
        _services().clean_text(value, 700)
        for value in (seen_identities or set())
        if _services().clean_text(value, 700)
    }

    def fetch(source: str):
        query = _services().clean_text(decoded.get("query", ""), 500)
        cooldown_error = _services()._model_search_source_error(source)
        if cooldown_error:
            return source, [], cooldown_error
        try:
            records = _services()._fast_model_search_source_call(
                source, query, result_limit, 0
            )
            error = ""
        except Exception as exc:
            return source, [], _services().clean_text(str(exc), 300)
        records = records if isinstance(records, list) else []
        if (
            avoid_repeats
            and source != "tavily"
            and len(records) >= result_limit
            and (seen_titles or seen_identities)
        ):
            repeated = 0
            for record in records:
                if not isinstance(record, dict):
                    continue
                identities = _services().candidate_identity_variants(record)
                if (
                    _services().title_key(
                        record.get("title") or record.get("paper_title") or ""
                    )
                    in seen_titles
                    or identities & seen_identities
                ):
                    repeated += 1
            # Repeated model searches used to fetch only one continuation
            # page.  When an index puts several already-seen papers first,
            # that single page still leaves the model with too few real
            # choices.  Continue through a small bounded window until the
            # requested number of fresh identities is available.  This is
            # only enabled for repeat-heavy pages, and the no-progress guard
            # prevents connectors that ignore offsets from being queried in a
            # loop.
            if repeated >= max(1, (len(records) + 1) // 2):

                def fresh_count(items):
                    fresh = set()
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        identities = _services().candidate_identity_variants(item)
                        title = _services().title_key(
                            item.get("title") or item.get("paper_title") or ""
                        )
                        if title in seen_titles or identities & seen_identities:
                            continue
                        # Count papers, not identity aliases (DOI, arXiv and
                        # title aliases for one work must not satisfy the
                        # requested result count independently).
                        canonical = _services().candidate_identity(item)
                        if canonical:
                            fresh.add(canonical)
                        elif title:
                            fresh.add("title:" + title)
                    return len(fresh)

                known_keys = {
                    key
                    for item in records
                    if isinstance(item, dict)
                    for key in _services().candidate_identity_variants(item)
                }
                # Repeat-heavy pages need a refill, but a model search should
                # remain responsive even when a connector keeps returning the
                # same historical window. One continuation is enough for the
                # fast tool lane; the normal lane retains the wider recall
                # window for explicit repeat searches.
                continuation_pages = (
                    1
                    if getattr(_services().MODEL_SEARCH_FAST_SOURCE, "enabled", False)
                    else 4
                )
                for page in range(1, continuation_pages + 1):
                    if fresh_count(records) >= result_limit:
                        break
                    try:
                        with _services().fast_model_search_source_budget(
                            _services().MODEL_SEARCH_CONTINUATION_TIMEOUT
                        ):
                            continuation = _services()._fast_model_search_source_call(
                                source, query, result_limit, page * result_limit
                            )
                    except Exception:
                        continuation = []
                    if not isinstance(continuation, list) or not continuation:
                        break
                    continuation_keys = {
                        key
                        for item in continuation
                        if isinstance(item, dict)
                        for key in _services().candidate_identity_variants(item)
                    }
                    if continuation_keys and not (continuation_keys - known_keys):
                        break
                    records.extend(continuation)
                    known_keys.update(continuation_keys)
                    # A short page is the connector's end-of-results signal;
                    # do not issue another speculative request after it.
                    if len(continuation) < result_limit:
                        break
        return source, records, error

    # ``urlopen(timeout=...)`` limits socket inactivity, not the total time
    # spent waiting for a provider.  A slow relay or crawler can therefore
    # keep a worker alive well past the AI lane budget, and the executor
    # context manager would wait for that worker before returning.  Enforce a
    # real wall-clock deadline for this bounded, best-effort lane.  Timed-out
    # workers are allowed to finish in the background; their results are
    # intentionally discarded so the UI and the next model turn stay fast.
    pool = _services().ThreadPoolExecutor(
        max_workers=min(3, len(selected_sources)),
        thread_name_prefix="model-search-source",
    )
    futures = {
        pool.submit(fetch, source): source for source in selected_sources
    }
    completed = {}
    try:
        deadline = max(
            1.0,
            float(getattr(_services(), "MODEL_SEARCH_SOURCE_TIMEOUT", 4))
            + 0.15,
        )
        try:
            iterator = _services().as_completed(futures, timeout=deadline)
            for future in iterator:
                source = futures[future]
                try:
                    completed[source] = future.result()
                except Exception as exc:
                    completed[source] = (
                        source,
                        [],
                        _services().clean_text(str(exc), 300),
                    )
        except TimeoutError:
            pass
    finally:
        for future in futures:
            if not future.done():
                future.cancel()
        # Do not wait for a provider that ignored the socket deadline.  The
        # bounded worker can finish and release itself without holding up the
        # request thread; cancelled work never enters the pool if it has not
        # started yet.
        pool.shutdown(wait=False, cancel_futures=True)

    results = []
    for source in selected_sources:
        if source in completed:
            results.append(completed[source])
        else:
            probe_url = _services().MODEL_SEARCH_SOURCE_PROBE_URLS.get(source)
            if probe_url:
                # A source that ignored the wall-clock budget is unlikely to
                # become useful again in the next model angle. Open the same
                # short circuit used for 429/5xx responses so later turns do
                # not launch another doomed request while the first worker
                # winds down in the background.
                _services()._cooldown_public_source(
                    probe_url,
                    min(
                        float(getattr(_services(), "PUBLIC_SOURCE_COOLDOWN_SECONDS", 45)),
                        60.0,
                    ),
                )
            results.append(
                (
                    source,
                    [],
                    f"来源在 {round(deadline, 1)} 秒内未响应，已跳过本轮",
                )
            )
    return results


def _model_search_tool_result(
    arguments: object,
    inventory: dict,
    identity_index: dict,
    seen_titles: set[str],
    seen_identities: set[str],
    options: dict,
    preferred_sources: list[str],
    prefetched: list[tuple[str, list[dict], str]] | None = None,
) -> dict:
    """Execute a bounded model-selected search against existing public connectors."""
    arguments, selected_sources, argument_error = (
        _services()._model_search_tool_arguments(arguments, preferred_sources)
    )
    if argument_error:
        return {"ok": False, "error": argument_error}
    query = _services().clean_text(arguments.get("query", ""), 500)
    try:
        requested_limit = int(
            arguments.get("limit", _services().MODEL_SEARCH_TOOL_MAX_LIMIT)
        )
    except (TypeError, ValueError):
        requested_limit = _services().MODEL_SEARCH_TOOL_MAX_LIMIT
    result_limit = min(_services().MODEL_SEARCH_TOOL_MAX_LIMIT, max(1, requested_limit))

    fetched = (
        prefetched
        if prefetched is not None
        else _services()._model_search_prefetch(
            arguments,
            preferred_sources,
            seen_titles,
            seen_identities,
            options.get("avoid_repeats", True),
        )
        or []
    )

    # Keep fresh and historical candidates in separate lanes.  The model can
    # prefer fresh IDs, while the caller can still use the historical lane to
    # fill a requested page when a topic has too few unseen papers.
    returned_ids, repeat_returned_ids, source_summaries = [], [], []
    filtered_count = repeat_count = already_in_context = invalid_count = 0
    for source, records, error in fetched:
        source_new = 0
        for raw in records if isinstance(records, list) else []:
            if not isinstance(raw, dict):
                continue
            record = dict(raw)
            title = _services().clean_text(
                record.get("title") or record.get("paper_title"), 600
            )
            url = _services()._model_search_url(
                record.get("url") or record.get("source_url") or record.get("link")
            )
            discovery_url = _services()._model_search_url(
                record.get("discovery_url") or url
            )
            if not title or not url:
                invalid_count += 1
                continue
            record.update({"title": title, "url": url, "source": source})
            if not record.get("external_id"):
                record["external_id"] = (
                    record.get("doi") or record.get("arxiv_id") or url
                )
            if not _services().record_matches_search_options(record, options):
                filtered_count += 1
                continue
            identities = _services().candidate_identity_variants(record)
            previously_seen = bool(
                _services().title_key(title) in seen_titles
                or identities & seen_identities
            )
            if options.get("avoid_repeats", True) and previously_seen:
                # Repeats are retained as a clearly marked fallback.  The
                # strict ``suppress_seen`` option is applied by run_query,
                # after it has enough context to report the shortfall.
                repeat_count += 1
            candidate_id = next(
                (identity_index[key] for key in identities if key in identity_index), ""
            )
            if candidate_id:
                already_in_context += 1
                candidate = inventory[candidate_id]
                if len(_services().clean_text(record.get("abstract", ""), 4000)) > len(
                    candidate.get("abstract", "")
                ):
                    candidate["abstract"] = _services().clean_text(
                        record.get("abstract", ""), 4000
                    )
                for field in (
                    "authors",
                    "year",
                    "doi",
                    "arxiv_id",
                    "venue",
                    "pdf_url",
                    "citation_count",
                    "open_access",
                    "verification_status",
                    "verification_sources",
                    "discovery_only",
                    "tavily_score",
                    "academic_source",
                ):
                    if not candidate.get(field) and record.get(field):
                        candidate[field] = record[field]
            else:
                if len(inventory) >= _services().MODEL_SEARCH_TOOL_MAX_CANDIDATES:
                    continue
                # Reserve the C* namespace for fresh candidates so existing
                # model/tool fixtures and, more importantly, model reasoning
                # can continue to treat C1..Cn as the preferred new lane.
                # Historical fill candidates use R* and are visibly marked.
                prefix = "R" if previously_seen else "C"
                next_index = 1 + sum(
                    1 for key in inventory if str(key).startswith(prefix)
                )
                candidate_id = f"{prefix}{next_index}"
                authors = record.get("authors") or record.get("author") or ""
                if isinstance(authors, list):
                    authors = ", ".join(
                        _services().clean_text(
                            author.get("name") if isinstance(author, dict) else author,
                            160,
                        )
                        for author in authors[:20]
                    )
                year = _services()._model_search_year(
                    record.get("year") or record.get("publication_year")
                )
                inventory[candidate_id] = {
                    "result_id": candidate_id,
                    "title": title,
                    "authors": _services().clean_text(authors, 1200),
                    "year": year,
                    "abstract": _services().clean_text(
                        record.get("abstract") or record.get("summary"), 4000
                    ),
                    "doi": _services().norm_doi(record.get("doi") or ""),
                    "arxiv_id": _services().norm_arxiv(record.get("arxiv_id") or ""),
                    "venue": _services().clean_text(
                        record.get("venue") or record.get("journal") or "", 500
                    ),
                    "url": url,
                    "pdf_url": _services()._model_search_url(
                        record.get("pdf_url") or record.get("pdf")
                    ),
                    "external_id": _services().clean_text(
                        record.get("external_id") or "", 500
                    ),
                    "citation_count": record.get("citation_count") or 0,
                    "open_access": bool(record.get("open_access")),
                    "provider_sources": [],
                    "source_citations": [],
                    "search_paths": [],
                    "verification_status": _services().clean_text(
                        record.get("verification_status")
                        or ("verified" if source != "tavily" else "unverified"),
                        40,
                    ),
                    "verification_sources": list(
                        record.get("verification_sources") or []
                    ),
                    "discovery_only": bool(record.get("discovery_only")),
                    "tavily_score": record.get("tavily_score", 0),
                    "_previously_seen": previously_seen,
                }
            candidate = inventory[candidate_id]
            candidate["_previously_seen"] = bool(
                candidate.get("_previously_seen") or previously_seen
            )
            for identity in identities:
                identity_index.setdefault(identity, candidate_id)
            candidate = inventory[candidate_id]
            provider = {
                "source": source,
                "external_id": _services().clean_text(
                    record.get("external_id") or "", 500
                ),
                "url": discovery_url,
            }
            if not any(
                item.get("source") == source and item.get("url") == url
                for item in candidate["provider_sources"]
            ):
                candidate["provider_sources"].append(provider)
            if url not in candidate["source_citations"]:
                candidate["source_citations"].append(url)
            path = {"source": source, "query": query}
            if path not in candidate["search_paths"]:
                candidate["search_paths"].append(path)
            if (
                candidate_id not in returned_ids
                and candidate_id not in repeat_returned_ids
                and not candidate.get("_returned_to_model")
            ):
                (
                    repeat_returned_ids
                    if candidate.get("_previously_seen")
                    else returned_ids
                ).append(candidate_id)
                candidate["_returned_to_model"] = True
                source_new += 1
        source_summaries.append(
            {
                "source": source,
                "status": "error" if error else "ok",
                "fetched": len(records) if isinstance(records, list) else 0,
                "new": source_new,
                "error": error,
            }
        )

    # Keep the tool output compact; the model already received earlier result IDs in prior turns.
    visible = []
    ordered_ids = [*returned_ids, *repeat_returned_ids]
    visible_limit = min(
        _services().MODEL_SEARCH_TOOL_MAX_SOURCES * result_limit,
        max(_services().MODEL_SEARCH_VISIBLE_CANDIDATE_FLOOR, result_limit * 2),
    )
    for candidate_id in ordered_ids[:visible_limit]:
        candidate = inventory[candidate_id]
        abstract = _services().clean_text(candidate.get("abstract"), 4000)
        evidence = abstract[: _services().MODEL_SEARCH_EVIDENCE_ABSTRACT_CHARS]
        visible.append(
            {
                **{
                    key: candidate.get(key)
                    for key in (
                        "result_id",
                        "title",
                        "authors",
                        "year",
                        "doi",
                        "arxiv_id",
                        "venue",
                        "url",
                        "provider_sources",
                        "verification_status",
                        "verification_sources",
                        "discovery_only",
                        "tavily_score",
                    )
                },
                "abstract": evidence,
                "abstract_truncated": len(abstract) > len(evidence),
                "previously_seen": bool(candidate.get("_previously_seen")),
            }
        )
    return {
        "ok": bool(visible),
        "query": query,
        "source_results": source_summaries,
        "results": visible,
        "filtered_by_constraints": filtered_count,
        "excluded_previously_seen": repeat_count,
        "already_returned_in_this_search": already_in_context,
        "records_without_source_url": invalid_count,
        "message": (
            "没有新的候选；请换查询角度或来源。"
            if not visible
            else "候选来自所列论文数据库，可引用其 result_id。"
        ),
    }


__all__ = ['_model_search_source_cooldown', '_model_search_source_error', '_model_search_tool_schema', '_model_search_payload', '_model_search_tool_calls', '_model_search_append_assistant_turn', '_model_search_selection_rows', '_model_search_tool_arguments', '_fast_model_search_source_call', '_model_search_prefetch', '_model_search_tool_result']
