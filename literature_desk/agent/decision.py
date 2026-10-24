"""Model-facing Agent decisions and provider tool-call normalization."""

from __future__ import annotations

import json
from typing import Any

from ..services import app_services as _services
from .llm import create_agent_llm
from .prompts import SYSTEM_PROMPT, render_agent_turn


def _decision_from_text(content: str, *, allow_tools: bool) -> dict[str, Any] | None:
    """Accept an optional small action envelope from providers without tools.

    Native function calls remain the preferred route.  Some compatible models
    return a JSON decision in ``content`` instead; accepting only this bounded
    envelope lets the graph represent reflection while preserving ordinary
    natural-language final answers.
    """
    text = str(content or "").strip()
    if not text:
        return None
    candidate = text
    if candidate.startswith("```") and candidate.endswith("```"):
        lines = candidate.splitlines()
        candidate = "\n".join(lines[1:-1]).strip() if len(lines) > 2 else ""
    if candidate.casefold().startswith("reflect:"):
        return {"action": "reflect", "answer": candidate.split(":", 1)[1].strip()}
    if not candidate.startswith("{") or not candidate.endswith("}"):
        return None
    try:
        parsed = json.loads(candidate)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict) or "action" not in parsed:
        return None
    action = str(parsed.get("action") or "").casefold()
    if action in {"reflect", "reflection"}:
        return {
            "action": "reflect",
            "answer": str(parsed.get("answer") or parsed.get("reflection") or "正在复核当前证据")[:2000],
        }
    if action in {"finish", "done", "stop"}:
        return {"action": "finish", "answer": str(parsed.get("answer") or "已完成当前 Agent 任务")[:4000]}
    if action == "call_tool" and allow_tools:
        arguments = parsed.get("arguments")
        return {
            "action": "call_tool",
            "tool": str(parsed.get("tool") or ""),
            "arguments": arguments if isinstance(arguments, dict) else {},
        }
    return None

def _tool_json_schema(rule: Any) -> dict[str, Any]:
    """Translate LitLoom's compact persisted contracts into JSON Schema."""
    if isinstance(rule, str):
        return {"type": rule if rule in {"string", "number", "integer", "boolean", "object", "array"} else "string"}
    if isinstance(rule, list):
        return {"anyOf": [_tool_json_schema(item) for item in rule]}
    if not isinstance(rule, dict):
        return {}
    if "type" not in rule and "properties" not in rule:
        properties = {
            str(key): _tool_json_schema(value)
            for key, value in rule.items()
            if key not in {"required", "additional_properties"}
        }
        return {
            "type": "object",
            "properties": properties,
            "required": list(rule.get("required") or []),
            "additionalProperties": bool(rule.get("additional_properties", True)),
        }
    result = {
        key: value
        for key, value in rule.items()
        if key in {
            "type", "description", "enum", "minimum", "maximum", "minLength",
            "maxLength", "minItems", "maxItems", "additionalProperties",
        }
    }
    if isinstance(rule.get("properties"), dict):
        result["properties"] = {
            str(key): _tool_json_schema(value)
            for key, value in rule["properties"].items()
        }
    if isinstance(rule.get("items"), (dict, list, str)):
        result["items"] = _tool_json_schema(rule["items"])
    if isinstance(rule.get("required"), list):
        result["required"] = rule["required"]
    return result


def _model_tool_definitions(catalog: list[dict[str, Any]], protocol: str) -> list[dict[str, Any]]:
    definitions = []
    for item in catalog:
        description = str(item.get("description") or "").strip()
        if item.get("requires_confirmation"):
            description += " 执行前需要用户明确确认。"
        parameters = _tool_json_schema(item.get("input_schema") or {})
        if protocol == "responses":
            definitions.append({
                "type": "function",
                "name": item["name"],
                "description": description,
                "parameters": parameters,
                "strict": False,
            })
        else:
            definitions.append({
                "type": "function",
                "function": {
                    "name": item["name"],
                    "description": description,
                    "parameters": parameters,
                },
            })
    return definitions


def _model_tool_calls(result: dict[str, Any], protocol: str) -> list[dict[str, Any]]:
    calls = []
    if protocol == "responses":
        output = result.get("output")
        for item in output if isinstance(output, list) else []:
            if isinstance(item, dict) and item.get("type") == "function_call":
                calls.append({"name": item.get("name", ""), "arguments": item.get("arguments", "")})
        return calls
    choices = result.get("choices") or []
    message = choices[0].get("message", {}) if choices and isinstance(choices[0], dict) else {}
    for item in message.get("tool_calls", []) if isinstance(message, dict) else []:
        function = item.get("function") if isinstance(item, dict) else None
        if isinstance(function, dict):
            calls.append({"name": function.get("name", ""), "arguments": function.get("arguments", "")})
    return calls


def _model_action(
    state: dict[str, Any], catalog: list[dict[str, Any]], *, allow_tools: bool = True
) -> dict[str, Any]:
    settings = _services().resolve_model_route(_services().model_settings(), "analysis")
    api_key = _services().model_api_key()
    if not settings.get("enabled") or not api_key:
        raise ValueError("自主 Agent 需要已启用的模型 API；请先在设置中完成模型配置")
    protocol = settings.get("protocol", "chat_completions")
    turn = render_agent_turn(state, catalog, allow_tools=allow_tools)
    final_turn = not allow_tools
    instructions = SYSTEM_PROMPT + (" 工具预算已耗尽，只能总结已获得证据、缺口与下一步。" if final_turn else "")
    definitions = _model_tool_definitions(catalog, protocol) if allow_tools else []
    if protocol == "responses":
        payload = {
            "model": settings["model"],
            "instructions": instructions,
            "input": turn,
            "stream": False,
            "store": False,
        }
        if definitions:
            payload.update({"tools": definitions, "tool_choice": "auto"})
        limit = int(_services().MODEL_MAX_OUTPUT_TOKENS or 0)
        if limit:
            payload["max_output_tokens"] = limit
    else:
        payload = {
            "model": settings["model"],
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": turn},
            ],
            "stream": False,
            "temperature": min(0.3, float(settings.get("temperature", 0.2) or 0.2)),
        }
        if definitions:
            payload.update({"tools": definitions, "tool_choice": "auto"})
        limit = int(_services().MODEL_MAX_OUTPUT_TOKENS or 0)
        if limit:
            payload["max_tokens"] = limit
    _services().add_model_reasoning(payload, settings)
    endpoint = _services().model_endpoint(settings)
    llm = create_agent_llm()
    if llm is None:
        raise RuntimeError("LangChain Runnable 不可用；请重新安装项目依赖")
    result = llm.invoke({
        "endpoint": endpoint,
        "payload": payload,
        "api_key": api_key,
        "protocol": protocol,
        "task": "autonomous_agent",
    })["response"]
    calls = _model_tool_calls(result, protocol)
    if calls and allow_tools:
        call = calls[0]
        if str(call.get("name") or "").strip().casefold() in {"reflect", "reflection"}:
            arguments = call.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    arguments = {}
            answer = arguments.get("answer") if isinstance(arguments, dict) else ""
            return {"action": "reflect", "answer": str(answer or "正在复核当前证据")[:2000]}
        arguments = call.get("arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except (TypeError, ValueError) as exc:
                raise ValueError("模型返回的工具参数不是有效 JSON") from exc
        if not isinstance(arguments, dict):
            raise ValueError("模型工具参数必须为 JSON 对象")
        return {"action": "call_tool", "tool": str(call.get("name") or ""), "arguments": arguments}
    content = _services().model_response_content(result).strip()
    structured = _decision_from_text(content, allow_tools=allow_tools)
    if structured is not None:
        return structured
    answer = content
    if not answer:
        raise ValueError("模型既未调用工具，也未返回最终答复")
    return {"action": "finish", "answer": answer[:4000]}
