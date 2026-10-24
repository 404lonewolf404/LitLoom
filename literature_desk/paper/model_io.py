"""Model response parsing and structured request helpers."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import threading

from ..services import app_services as _services


def _balanced_json_suffix(text: str) -> str:
    """Close a truncated JSON object without changing completed values."""
    stack = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]" and stack and char == stack[-1]:
            stack.pop()
    # A response cut in the middle of a string is common when an output token
    # budget is reached. Closing it lets the caller retain the completed keys;
    # the caller still validates the resulting structured content.
    suffix = '"' if in_string else ""
    return text + suffix + "".join(reversed(stack))


def parse_model_json(
    content, allow_repair: bool = True, json_contract: dict | None = None
) -> dict:
    """Parse a model response that is expected to contain one JSON object.

    Providers sometimes wrap JSON in markdown fences or add a short sentence
    before it.  We accept those harmless wrappers while still requiring an
    object so callers never persist an opaque, untraceable answer as a
    structured analysis.
    """
    if isinstance(content, dict):
        return content
    text = str(content or "").strip()
    # Some reasoning relays put their private chain of thought in a visible
    # <think> block. It is not part of the requested JSON document.
    text = (
        _services()
        .re.sub(
            r"<think(?:ing)?\b[^>]*>.*?</think(?:ing)?>",
            "",
            text,
            flags=_services().re.I | _services().re.S,
        )
        .strip()
    )
    text = (
        _services()
        .re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=_services().re.I)
        .strip()
    )
    decoder = _services().json.JSONDecoder(strict=False)
    candidates = [text]
    # Find a complete JSON value after a short apology/preamble. raw_decode
    # avoids using the last `}` which may belong to prose or a second answer.
    candidates.extend(text[index:] for index, char in enumerate(text) if char == "{")
    candidates.extend(text[index:] for index, char in enumerate(text) if char == "[")
    value = None
    candidates_found = []
    qualified = []
    for candidate in candidates:
        attempts = [candidate]
        if allow_repair:
            attempts.extend(
                (
                    _services().re.sub(r",\s*([}\]])", r"\1", candidate),
                    _services()._balanced_json_suffix(candidate),
                )
            )
        for attempt in attempts:
            try:
                parsed, end = decoder.raw_decode(attempt.lstrip())
            except (TypeError, ValueError, _services().json.JSONDecodeError):
                continue
            if isinstance(parsed, dict):
                candidates_found.append((len(parsed), end, parsed))
                if not json_contract:
                    # Reasoning-capable relays often echo the requested
                    # response format before the actual answer. Prefer a
                    # substantive/larger object over a small schema fragment.
                    if set(parsed) <= {
                        "type",
                        "format",
                        "schema",
                        "properties",
                        "required",
                    }:
                        continue
                    value = max(
                        candidates_found, key=lambda item: (len(item[2]), item[1])
                    )[2]
                    continue
                if _services()._model_json_matches_contract(parsed, json_contract):
                    qualified.append((len(parsed), end, parsed))
    if json_contract and qualified:
        value = max(qualified, key=lambda item: (item[0], item[1]))[2]
    if value is None:
        if json_contract:
            raise ValueError("模型输出 JSON 缺少必需字段或核心内容")
        raise ValueError("模型输出不是有效 JSON")
    if not isinstance(value, dict):
        raise ValueError("模型输出必须是 JSON 对象")
    return value


def _model_json_matches_contract(value: dict, contract: dict) -> bool:
    required = contract.get("required", []) if isinstance(contract, dict) else []
    if any(key not in value for key in required):
        return False

    def resolve(path: str):
        current = value
        for part in str(path).split("."):
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return current

    nonempty = contract.get("nonempty", []) if isinstance(contract, dict) else []
    if any(not _services()._meaningful_model_value(resolve(path)) for path in nonempty):
        return False
    any_nonempty = (
        contract.get("any_nonempty", []) if isinstance(contract, dict) else []
    )
    for alternatives in any_nonempty:
        if not any(
            _services()._meaningful_model_value(resolve(path)) for path in alternatives
        ):
            return False
    return True


def _read_model_response_payload(response) -> dict:
    """Read either a normal JSON response or an OpenAI-style SSE stream."""
    content_type = (
        response.headers.get("Content-Type", "")
        if getattr(response, "headers", None)
        else ""
    ).lower()
    if "event-stream" not in content_type:
        result = _services().json.loads(response.read().decode("utf-8", "replace"))
        if isinstance(result, dict):
            finish_reason = _services().model_response_finish_reason(result)
            if finish_reason:
                result["_litloom_finish_reason"] = finish_reason
        return result
    text_parts = []
    reasoning_parts = []
    completed_parts = []
    finish_reason = ""
    usage = {}
    for raw_line in response:
        line = (
            raw_line.decode("utf-8", "replace")
            if isinstance(raw_line, bytes)
            else str(raw_line)
        )
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = _services().json.loads(data)
        except (TypeError, ValueError):
            continue
        if isinstance(event.get("usage"), dict):
            usage = event["usage"]
        event_response = event.get("response")
        if isinstance(event_response, dict) and isinstance(
            event_response.get("usage"), dict
        ):
            usage = event_response["usage"]
        if event.get("type") == "response.failed":
            raise ValueError("模型流式响应失败")
        if event.get("type") == "response.incomplete":
            response_data = event.get("response") or {}
            details = response_data.get("incomplete_details") or {}
            finish_reason = details.get("reason") or finish_reason
        delta = event.get("delta", "")
        event_type = str(event.get("type") or "")
        if (
            event_type.startswith("response.reasoning")
            or "reasoning_content" in event_type
        ):
            if isinstance(delta, str):
                reasoning_parts.append(delta)
            elif isinstance(delta, dict) and isinstance(delta.get("text"), str):
                reasoning_parts.append(delta["text"])
        elif isinstance(delta, str):
            text_parts.append(delta)
        elif isinstance(delta, dict) and isinstance(delta.get("text"), str):
            text_parts.append(delta["text"])
        if event_type == "response.completed":
            completed = event.get("response") or {}
            if isinstance(completed, dict):
                completed_text = _services().model_response_content(completed)
                if completed_text:
                    completed_parts.append(completed_text)
        choices = event.get("choices") or []
        if choices and isinstance(choices[0], dict):
            finish_reason = choices[0].get("finish_reason") or finish_reason
            choice_delta = choices[0].get("delta") or {}
            if isinstance(choice_delta, dict):
                value = choice_delta.get("content") or choice_delta.get("text", "")
                reasoning = choice_delta.get("reasoning_content") or choice_delta.get(
                    "reasoning", ""
                )
                if isinstance(value, str):
                    text_parts.append(value)
                if isinstance(reasoning, str):
                    reasoning_parts.append(reasoning)
    # Reasoning is a compatibility fallback for probes/providers that never
    # emit a normal answer. Never mix it into a JSON answer when both exist.
    # A few Responses relays emit partial deltas and then include the
    # authoritative complete answer in response.completed. Prefer that final
    # snapshot when available so a partial delta is never mistaken for a
    # token-truncated JSON document.
    result = {
        "output_text": "".join(completed_parts)
        or "".join(text_parts)
        or "".join(reasoning_parts),
        "_litloom_finish_reason": finish_reason,
    }
    if usage:
        result["_litloom_usage"] = usage
    return result


def read_model_response(response) -> dict:
    """Read a provider response and persist only its safe usage metadata."""
    result = None
    error = None
    try:
        result = _services()._read_model_response_payload(response)
        return result
    except Exception as exc:
        error = exc
        raise
    finally:
        metadata = getattr(response, "_litloom_model_usage", None)
        if metadata:
            _services().record_model_usage(metadata, result=result, error=error)


def model_response_finish_reason(result: dict) -> str:
    choices = result.get("choices") or []
    if choices and isinstance(choices[0], dict) and choices[0].get("finish_reason"):
        return str(choices[0]["finish_reason"])
    details = result.get("incomplete_details") or {}
    if result.get("status") == "incomplete" or details.get("reason"):
        return str(details.get("reason") or "incomplete")
    return str(result.get("_litloom_finish_reason") or "")


def _is_truncated_model_response(result: dict) -> bool:
    reason = _services().model_response_finish_reason(result).casefold()
    return reason in {
        "length",
        "max_output_tokens",
        "max_tokens",
        "token_limit",
        "incomplete",
    }


def _model_json_request_once(
    system_prompt: str,
    user_payload: dict,
    timeout: int,
    max_output_tokens: int | None,
    json_contract: dict | None = None,
    reasoning_effort: str | None = None,
    task: str = "analysis",
) -> dict:
    settings = _services().resolve_model_route(_services().model_settings(), task)
    api_key = _services().model_api_key()
    if not api_key or not settings["enabled"]:
        raise ValueError("尚未启用模型服务。请先在模型配置中填写 API Key 并启用")
    if settings.get("protocol") == "responses":
        payload = {
            "model": settings["model"],
            "instructions": system_prompt,
            "input": _services().json.dumps(user_payload, ensure_ascii=False),
            "temperature": min(0.35, settings["temperature"]),
            "stream": _services().MODEL_STREAMING,
            "store": False,
        }
        output_limit = (
            _services().MODEL_MAX_OUTPUT_TOKENS
            if max_output_tokens is None
            else max_output_tokens
        )
        if output_limit:
            payload["max_output_tokens"] = output_limit
    else:
        payload = {
            "model": settings["model"],
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": _services().json.dumps(user_payload, ensure_ascii=False),
                },
            ],
            "temperature": min(0.35, settings["temperature"]),
            "stream": _services().MODEL_STREAMING,
        }
        output_limit = (
            _services().MODEL_MAX_OUTPUT_TOKENS
            if max_output_tokens is None
            else max_output_tokens
        )
        if output_limit:
            payload["max_tokens"] = output_limit
    _services().add_model_reasoning(payload, settings, reasoning_effort)
    _services().add_json_output_format(payload, settings)
    with _services().model_usage_context(task, "text"):
        with _services().open_compatible_model_request(
            _services().model_endpoint(settings), payload, api_key, timeout
        ) as response:
            result = _services().read_model_response(response)
    return _services()._parse_model_json_result(result, json_contract=json_contract)


def _json_retry_instruction(system_prompt: str) -> str:
    return (
        system_prompt.rstrip()
        + "\n\n上一次输出未能被系统解析。请重新输出完整、紧凑、可直接解析的 JSON 对象；"
        "禁止 Markdown 代码围栏、解释文字、注释和思考过程；不要省略必需字段；"
        "尽量用短句和短数组，不要输出额外字段；在输出前检查所有字符串已正确转义并闭合。"
    )


def _json_output_limit(base_limit: int | None, attempt: int) -> int | None:
    """Increase the JSON budget only after a real parse/truncation failure.

    A missing per-call limit used to mean every retry silently reused the
    normal requests small and predictable.
    """
    if not base_limit or attempt <= 0:
        return base_limit
    base = max(1, int(base_limit))
    if attempt == 1:
        return min(16000, max(base + 800, base * 2))
    return min(16000, max(base + 1600, base * 3))


def _parse_model_json_result(
    result: dict, allow_empty: bool = False, json_contract: dict | None = None
) -> dict:
    """Parse one response, tolerating inaccurate provider length metadata.

    Some relays mark a response as ``length`` after emitting a complete JSON
    document (often because hidden reasoning used the same completion budget).
    Strict parsing is attempted before treating that metadata as truncation.
    A repaired/closed JSON document is never accepted when the provider says
    it was cut off; that case must go through the larger-budget retry.
    """
    truncated = _services()._is_truncated_model_response(result)
    content = _services().model_response_content(result)
    if not content.strip():
        if truncated:
            raise ValueError("模型输出因达到输出上限而被截断")
        raise ValueError("模型输出为空，未生成结构化结果")
    try:
        parsed = _services().parse_model_json(
            content, allow_repair=not truncated, json_contract=json_contract
        )
    except ValueError:
        if truncated:
            raise ValueError("模型输出因达到输出上限而被截断")
        raise
    if not allow_empty and not _services()._model_result_has_content(parsed):
        if truncated:
            raise ValueError("模型输出因达到输出上限而被截断")
        raise ValueError("模型输出为空，未生成可用的结构化结果")
    return parsed


def _is_model_placeholder(value) -> bool:
    """Return whether a model field contains a template/empty placeholder.

    Providers often satisfy a JSON schema by returning ``"..."`` or a label
    such as ``"原文未说明。"``. Treat punctuation-only variants as empty too,
    while leaving real prose that merely mentions uncertainty untouched.
    """
    if not isinstance(value, str):
        return False
    normalized = " ".join(value.strip().casefold().split())
    if not normalized:
        return True
    # Strip presentation punctuation that providers append to a marker.
    marker_key = normalized.strip("`*_ \t\r\n.,;:!?。；：！？、")
    if marker_key in _services()._MODEL_EMPTY_MARKERS:
        return True
    if _services().re.fullmatch(r"[.。…·\-_=~\s]+", normalized):
        return True
    if normalized.startswith("<") and normalized.endswith(">"):
        inner = normalized[1:-1].strip(" `*_.,;:!?。；：！？")
        if (
            not inner
            or inner in _services()._MODEL_EMPTY_MARKERS
            or _services().re.fullmatch(r"[.。…\-_=~\s]+", inner)
        ):
            return True
    # Common schema prose must not count as a completed field. Keep this
    # conservative so a substantive sentence containing these words survives.
    if _services().re.fullmatch(
        r"(?:原文|本文)?(?:未说明|没有说明|未提及|未报告|无法确认)(?:，|,|。|\.)?(?:需|请)?(?:回到原文)?(?:核验|确认)?",
        normalized,
    ):
        return True
    return False


def _meaningful_model_value(value) -> bool:
    if isinstance(value, str):
        return not _services()._is_model_placeholder(value)
    if isinstance(value, list):
        return any(_services()._meaningful_model_value(item) for item in value)
    if isinstance(value, dict):
        return any(
            _services()._meaningful_model_value(item)
            for key, item in value.items()
            if not str(key).startswith("_")
        )
    return value is not None


def _model_result_has_content(result: dict) -> bool:
    """Reject a syntactically valid but empty structured answer."""
    if not isinstance(result, dict):
        return False
    if set(result) <= {"type", "format", "schema", "properties", "required"}:
        return False
    card = result.get("reading_card")
    if isinstance(card, dict):
        fields = (
            set(_services().READING_CARD_FIELDS)
            if "READING_CARD_FIELDS" in globals()
            else set()
        )
        if fields and any(
            _services()._meaningful_model_value(card.get(field)) for field in fields
        ):
            return True
        if _services()._meaningful_model_value(
            result.get("evidence")
        ) or _services()._meaningful_model_value(result.get("visual_evidence")):
            return True
        return any(
            _services()._meaningful_model_value(value)
            for key, value in result.items()
            if key
            not in {
                "reading_card",
                "evidence",
                "visual_evidence",
                "visual_analysis",
                "uncertainties",
            }
            and not str(key).startswith("_")
        )
    fields = (
        set(_services().READING_CARD_FIELDS)
        if "READING_CARD_FIELDS" in globals()
        else set()
    )
    if fields.intersection(result):
        return any(
            _services()._meaningful_model_value(result.get(field)) for field in fields
        )
    if "notes" in result or "visual_observations" in result:
        return _services()._meaningful_model_value(
            result.get("notes")
        ) or _services()._meaningful_model_value(result.get("visual_observations"))
    return any(
        _services()._meaningful_model_value(value)
        for key, value in result.items()
        if not str(key).startswith("_")
    )


def _model_json_request_unbounded(
    system_prompt: str,
    user_payload: dict,
    timeout: int = 120,
    max_output_tokens: int | None = None,
    json_contract: dict | None = None,
    reasoning_effort: str | None = None,
    task: str = "analysis",
) -> dict:
    """Call an OpenAI-compatible endpoint with bounded adaptive JSON retries."""
    last_error = None
    base_limit = (
        _services().MODEL_MAX_OUTPUT_TOKENS
        if max_output_tokens is None
        else max_output_tokens
    )
    for attempt in range(3):
        try:
            prompt = (
                system_prompt
                if attempt == 0
                else _services()._json_retry_instruction(system_prompt)
            )
            output_limit = _services()._json_output_limit(base_limit, attempt)
            request_kwargs = (
                {"reasoning_effort": reasoning_effort}
                if reasoning_effort is not None
                else {}
            )
            once_parameters = (
                _services()
                .inspect.signature(_services()._model_json_request_once)
                .parameters
            )
            if "task" in once_parameters or any(
                item.kind == _services().inspect.Parameter.VAR_KEYWORD
                for item in once_parameters.values()
            ):
                request_kwargs["task"] = task
            if json_contract is None:
                return _services()._model_json_request_once(
                    prompt, user_payload, timeout, output_limit, **request_kwargs
                )
            if "json_contract" in once_parameters or any(
                item.kind == _services().inspect.Parameter.VAR_KEYWORD
                for item in once_parameters.values()
            ):
                request_kwargs["json_contract"] = json_contract
            return _services()._model_json_request_once(
                prompt, user_payload, timeout, output_limit, **request_kwargs
            )
        except ValueError as exc:
            if (
                not any(marker in str(exc) for marker in ("模型输出", "JSON", "截断"))
                or attempt >= 2
            ):
                raise
            last_error = exc
    raise last_error or ValueError("模型输出不是有效 JSON")


def model_json_request(
    system_prompt: str,
    user_payload: dict,
    timeout: int = 120,
    max_output_tokens: int | None = None,
    json_contract: dict | None = None,
    reasoning_effort: str | None = None,
    task: str = "analysis",
) -> dict:
    """Bound normal model calls so concurrent browser actions cannot exhaust RAM."""
    return _services()._with_model_request_budget(
        lambda: _services()._model_json_request_unbounded(
            system_prompt,
            user_payload,
            timeout,
            max_output_tokens,
            json_contract,
            reasoning_effort,
            task,
        )
    )


def model_json_request_for_task(
    system_prompt: str, user_payload: dict, task: str, **kwargs
) -> dict:
    """Pass task routing when the configured request adapter supports it."""
    target = _services().model_json_request
    parameters = _services().inspect.signature(target).parameters
    if "task" in parameters or any(
        item.kind == _services().inspect.Parameter.VAR_KEYWORD
        for item in parameters.values()
    ):
        kwargs["task"] = task
    return target(system_prompt, user_payload, **kwargs)


def _model_request_payload(
    settings: dict,
    system_prompt: str,
    user_content,
    max_output_tokens: int | None = None,
    reasoning_effort: str | None = None,
) -> dict:
    """Build one OpenAI-compatible request payload for text or vision messages."""
    if settings.get("protocol") == "responses":
        if isinstance(user_content, list):
            # Responses uses input_text/input_image content types, while
            # Chat Completions uses text/image_url. Convert at the boundary
            # so the same analysis code works with both protocols.
            response_parts = []
            for part in user_content:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type")
                if part_type in {"text", "input_text"}:
                    response_parts.append(
                        {"type": "input_text", "text": str(part.get("text", ""))}
                    )
                elif part_type in {"image_url", "input_image"}:
                    image = (
                        part.get("image_url")
                        if isinstance(part.get("image_url"), dict)
                        else part
                    )
                    image_url = image.get("url") or image.get("image_url")
                    if image_url:
                        response_parts.append(
                            {
                                "type": "input_image",
                                "image_url": image_url,
                                "detail": image.get("detail")
                                or _services().MODEL_IMAGE_DETAIL,
                            }
                        )
            user_content = [{"role": "user", "content": response_parts}]
        body = {
            "model": settings["model"],
            "instructions": system_prompt,
            "input": user_content,
            "temperature": min(0.35, settings["temperature"]),
            "stream": _services().MODEL_STREAMING,
            "store": False,
        }
        output_limit = (
            _services().MODEL_MAX_OUTPUT_TOKENS
            if max_output_tokens is None
            else max_output_tokens
        )
        if output_limit:
            body["max_output_tokens"] = output_limit
    else:
        body = {
            "model": settings["model"],
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": min(0.35, settings["temperature"]),
            "stream": _services().MODEL_STREAMING,
        }
        output_limit = (
            _services().MODEL_MAX_OUTPUT_TOKENS
            if max_output_tokens is None
            else max_output_tokens
        )
        if output_limit:
            body["max_tokens"] = output_limit
    _services().add_model_reasoning(body, settings, reasoning_effort)
    return _services().add_json_output_format(body, settings)


def _model_request_body(
    settings: dict,
    system_prompt: str,
    user_content,
    api_key: str,
    max_output_tokens: int | None = None,
) -> bytes:
    return (
        _services()
        .json.dumps(
            _services()._model_request_payload(
                settings, system_prompt, user_content, max_output_tokens
            ),
            ensure_ascii=False,
        )
        .encode("utf-8")
    )


def _model_multimodal_json_request_unbounded(
    system_prompt: str,
    user_payload: dict,
    images: list[dict],
    timeout: int = 150,
    max_output_tokens: int | None = None,
    reasoning_effort: str | None = None,
) -> dict:
    """Call an OpenAI-compatible vision model with page images plus page text.

    ``images`` contains data URLs and page metadata.  The metadata is kept in a
    text part so providers that do not preserve image ordering still receive
    explicit page anchors.  The endpoint must support the standard
    ``image_url`` content part; callers can fall back to the text-only path
    when no rendered page is available.
    """
    settings = _services().resolve_model_route(_services().model_settings(), "vision")
    api_key = _services().model_api_key()
    if not api_key or not settings["enabled"]:
        raise ValueError("尚未启用模型服务。请先在模型配置中填写 API Key 并启用")
    content = [
        {
            "type": "text",
            "text": _services().json.dumps(user_payload, ensure_ascii=False),
        }
    ]
    for image in images:
        data_url = image.get("data_url", "")
        if not data_url:
            continue
        content.append(
            {
                "type": "text",
                "text": f"[视觉页面锚点：附件 {image.get('attachment_id', '')} 第 {image.get('page', '?')} 页]",
            }
        )
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": data_url,
                    "detail": image.get("detail") or _services().MODEL_IMAGE_DETAIL,
                },
            }
        )
    last_error = None
    base_limit = (
        _services().MODEL_MAX_OUTPUT_TOKENS
        if max_output_tokens is None
        else max_output_tokens
    )
    for attempt in range(3):
        prompt = (
            system_prompt
            if attempt == 0
            else _services()._json_retry_instruction(system_prompt)
        )
        output_limit = _services()._json_output_limit(base_limit, attempt)
        payload = _services()._model_request_payload(
            settings, prompt, content, output_limit, reasoning_effort
        )
        try:
            with _services().model_usage_context("vision", "vision"):
                with _services().open_compatible_model_request(
                    _services().model_endpoint(settings),
                    payload,
                    api_key,
                    timeout,
                    "text/event-stream, application/json",
                ) as response:
                    result = _services().read_model_response(response)
            return _services()._parse_model_json_result(result)
        except ValueError as exc:
            if (
                not any(marker in str(exc) for marker in ("模型输出", "JSON", "截断"))
                or attempt >= 2
            ):
                raise
            last_error = exc
    raise last_error or ValueError("模型输出不是有效 JSON")


def model_multimodal_json_request(
    system_prompt: str,
    user_payload: dict,
    images: list[dict],
    timeout: int = 150,
    max_output_tokens: int | None = None,
    reasoning_effort: str | None = None,
) -> dict:
    """Bound vision calls using the same process-wide request budget."""
    return _services()._with_model_request_budget(
        lambda: _services()._model_multimodal_json_request_unbounded(
            system_prompt,
            user_payload,
            images,
            timeout,
            max_output_tokens,
            reasoning_effort,
        )
    )


def _visual_cache_path(attachment: dict, page_number: int) -> Path:
    digest = _services().clean_text(
        attachment.get("sha256", ""), 64
    ) or _services().clean_text(attachment.get("id", ""), 100)
    return _services().active_visuals_dir() / f"{digest}-{int(page_number)}.jpg"


__all__ = [
    "_balanced_json_suffix",
    "parse_model_json",
    "_model_json_matches_contract",
    "_read_model_response_payload",
    "read_model_response",
    "model_response_finish_reason",
    "_is_truncated_model_response",
    "_model_json_request_once",
    "_json_retry_instruction",
    "_json_output_limit",
    "_parse_model_json_result",
    "_is_model_placeholder",
    "_meaningful_model_value",
    "_model_result_has_content",
    "_model_json_request_unbounded",
    "model_json_request",
    "model_json_request_for_task",
    "_model_request_payload",
    "_model_request_body",
    "_model_multimodal_json_request_unbounded",
    "model_multimodal_json_request",
    "_visual_cache_path",
]
