"""Agent step validation, execution, and run orchestration."""

from __future__ import annotations

import sqlite3

from ..services import app_services as _services


def _agent_update_step(run_id: str, step_id: str, **updates) -> None:
    allowed = {
        "status",
        "input_json",
        "output_json",
        "error",
        "attempts",
        "started_at",
        "updated_at",
        "completed_at",
        "progress_current",
        "progress_total",
        "message",
    }
    values = {key: value for key, value in updates.items() if key in allowed}
    values.setdefault("updated_at", _services().now_iso())
    if not values:
        return
    assignments = ",".join(f"{key}=?" for key in values)
    with _services().DB_LOCK, _services().db_session() as db:
        db.execute(
            f"UPDATE agent_steps SET {assignments} WHERE id=? AND run_id=?",
            (*values.values(), step_id, run_id),
        )


def _agent_update_run(run_id: str, **updates) -> None:
    allowed = {
        "status",
        "current_step_id",
        "error",
        "started_at",
        "updated_at",
        "completed_at",
    }
    values = {key: value for key, value in updates.items() if key in allowed}
    values.setdefault("updated_at", _services().now_iso())
    if not values:
        return
    assignments = ",".join(f"{key}=?" for key in values)
    with _services().DB_LOCK, _services().db_session() as db:
        db.execute(
            f"UPDATE agent_runs SET {assignments} WHERE id=?",
            (*values.values(), run_id),
        )


def _agent_schema_type_name(value: object) -> str:
    """Return a stable JSON-schema-like type name for diagnostics."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _agent_schema_type_matches(value: object, expected: str) -> bool:
    expected = str(expected or "").strip().casefold()
    if expected in {"", "any", "unknown"}:
        return True
    if expected in {"null", "none"}:
        return value is None
    if expected in {"object", "dict"}:
        return isinstance(value, dict)
    if expected in {"array", "list"}:
        return isinstance(value, list)
    if expected in {"string", "str"}:
        return isinstance(value, str)
    if expected in {"boolean", "bool"}:
        return isinstance(value, bool)
    if expected in {"integer", "int"}:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected in {"number", "float"}:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return True


def _agent_schema_path_value(value: object, path: str) -> object:
    """Read dotted and simple indexed paths used by completion rules."""
    if not path:
        return value
    current = value
    tokens = []
    for match in _services().re.finditer(r"([^.\[\]]+)|\[(\d+)\]", str(path)):
        tokens.append(match.group(1) if match.group(1) is not None else match.group(2))
    for key in tokens:
        if isinstance(current, dict) and isinstance(key, str):
            if key not in current:
                return _services()._AGENT_SCHEMA_MISSING
            current = current[key]
        elif isinstance(current, list):
            try:
                current = current[int(key)]
            except (TypeError, ValueError, IndexError):
                return _services()._AGENT_SCHEMA_MISSING
        else:
            return _services()._AGENT_SCHEMA_MISSING
    return current


def _agent_schema_errors(
    schema: object, value: object, path: str = "$", *, max_errors: int = 12
) -> list[str]:
    """Validate a compact JSON schema used by persisted Agent contracts.

    Existing contracts use the shorthand ``{"papers": "array"}``; newer
    contracts may use nested ``type/properties/items`` objects.  Optional
    fields are checked when present, while required fields are declared
    explicitly by a schema's ``required`` list or a step's completion rule.
    """
    errors: list[str] = []

    def add(message: str) -> None:
        if len(errors) < max_errors:
            errors.append(message)

    def visit(rule: object, current: object, current_path: str) -> None:
        if len(errors) >= max_errors or rule in (None, {}):
            return
        if isinstance(rule, str):
            if not _services()._agent_schema_type_matches(current, rule):
                add(
                    f"{current_path} 应为 {rule}，实际为 {_services()._agent_schema_type_name(current)}"
                )
            return
        if isinstance(rule, list):
            if not any(
                not _services()._agent_schema_errors(
                    item, current, current_path, max_errors=1
                )
                for item in rule
            ):
                add(f"{current_path} 不符合允许的结构")
            return
        if not isinstance(rule, dict):
            return
        if current is None and rule.get("nullable"):
            return
        expected = rule.get("type")
        if expected and not _services()._agent_schema_type_matches(current, expected):
            add(
                f"{current_path} 应为 {expected}，实际为 {_services()._agent_schema_type_name(current)}"
            )
            return
        enum = rule.get("enum") or rule.get("allowed_values")
        if isinstance(enum, list) and current not in enum:
            add(f"{current_path} 的值不在允许范围内：{current!r}")
        if isinstance(current, str):
            minimum = rule.get("min_length")
            maximum = rule.get("max_length")
            if minimum is not None and len(current) < int(minimum):
                add(f"{current_path} 长度 {len(current)} 小于 {minimum}")
            if maximum is not None and len(current) > int(maximum):
                add(f"{current_path} 长度 {len(current)} 超过 {maximum}")
        if isinstance(current, list):
            minimum = rule.get("min_items")
            maximum = rule.get("max_items")
            if minimum is not None and len(current) < int(minimum):
                add(f"{current_path} 数量 {len(current)} 小于 {minimum}")
            if maximum is not None and len(current) > int(maximum):
                add(f"{current_path} 数量 {len(current)} 超过 {maximum}")
            item_rule = rule.get("items")
            if item_rule is not None:
                for index, item in enumerate(current):
                    visit(item_rule, item, f"{current_path}[{index}]")
                    if len(errors) >= max_errors:
                        return
        if isinstance(current, dict):
            properties = rule.get("properties")
            if isinstance(properties, dict):
                required = rule.get("required") or []
                for field in required if isinstance(required, list) else []:
                    if (
                        _services()._agent_schema_path_value(current, str(field))
                        is _services()._AGENT_SCHEMA_MISSING
                    ):
                        add(f"{current_path}.{field} 为必填字段")
                for field, child_rule in properties.items():
                    if field in current:
                        visit(child_rule, current[field], f"{current_path}.{field}")

    # A flat mapping is the public shorthand used by the current contracts.
    if isinstance(schema, dict) and "type" not in schema and "properties" not in schema:
        if not isinstance(value, dict):
            return [
                f"{path} 应为 object，实际为 {_services()._agent_schema_type_name(value)}"
            ]
        for field, rule in schema.items():
            if field in {"required", "additional_properties"}:
                continue
            if field in value:
                visit(rule, value[field], f"{path}.{field}")
        required = schema.get("required")
        for field in required if isinstance(required, list) else []:
            if (
                _services()._agent_schema_path_value(value, str(field))
                is _services()._AGENT_SCHEMA_MISSING
            ):
                add(f"{path}.{field} 为必填字段")
        return errors
    visit(schema, value, path)
    return errors


def _agent_completion_path_required(result: dict, field: str) -> tuple[bool, object]:
    value = _services()._agent_schema_path_value(result, field)
    if (
        value is _services()._AGENT_SCHEMA_MISSING
        or value is None
        or value == ""
        or value == []
    ):
        return False, value
    return True, value


def _agent_record_validation_failure(
    run_id: str, step_id: str, detail: str, result: object = None
) -> None:
    """Turn a tool-call transport success into a visible contract failure."""
    with _services().DB_LOCK, _services().db_session() as db:
        call = db.execute(
            "SELECT id FROM agent_tool_calls WHERE run_id=? AND step_id=? ORDER BY started_at DESC LIMIT 1",
            (run_id, step_id),
        ).fetchone()
        if not call:
            return
        output = (
            _services()._bounded_agent_payload(result)
            if isinstance(result, dict)
            else None
        )
        db.execute(
            "UPDATE agent_tool_calls SET status='failed',error=?,output_json=COALESCE(?,output_json),completed_at=?,duration_ms=COALESCE(duration_ms,0) WHERE id=?",
            (
                _services().clean_text(detail, 1200),
                (
                    _services().json.dumps(output, ensure_ascii=False)
                    if output is not None
                    else None
                ),
                _services().now_iso(),
                call["id"],
            ),
        )


def _agent_execute_tool(
    run_id: str, step_id: str, tool_name: str, tool_class: str, payload: dict
) -> dict:
    started = _services().time.perf_counter()
    call_id = str(_services().uuid.uuid4())
    started_at = _services().now_iso()
    with _services().DB_LOCK, _services().db_session() as db:
        db.execute(
            "INSERT INTO agent_tool_calls(id,run_id,step_id,tool_name,tool_class,status,input_json,started_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                call_id,
                run_id,
                step_id,
                tool_name,
                tool_class,
                "running",
                _services().json.dumps(
                    _services()._bounded_agent_payload(payload, 80000),
                    ensure_ascii=False,
                ),
                started_at,
            ),
        )
    try:
        contract = _services().AGENT_TOOL_SPECS.get(tool_name, {})
        input_errors = _services()._agent_schema_errors(
            contract.get("input_schema") or {}, payload
        )
        if input_errors:
            raise ValueError("工具输入结构校验失败：" + "；".join(input_errors[:4]))
        if (
            contract.get("requires_confirmation") or tool_class in {"Apply", "External"}
        ) and payload.get("confirm") is not True:
            safe_local_preview = (
                tool_class not in {"Apply", "External"}
                and tool_name == "generate_research_brief"
            )
            if tool_name == "draft_reading_card":
                paper = (
                    payload.get("paper")
                    if isinstance(payload.get("paper"), dict)
                    else {}
                )
                paper_id = _services().clean_text(
                    paper.get("local_paper_id") or paper.get("id", ""), 200
                )
                with _services().DB_LOCK, _services().db_session() as db:
                    safe_local_preview = (
                        not paper_id
                        or paper_id.startswith("candidate:")
                        or not bool(
                            db.execute(
                                "SELECT 1 FROM attachments WHERE paper_id=? LIMIT 1",
                                (paper_id,),
                            ).fetchone()
                        )
                    )
            if not safe_local_preview:
                raise PermissionError(f"工具 {tool_name} 需要显式确认")
        # The DAG worker must remain responsive even when a connector or
        # compatible model endpoint stops responding.  Network functions have
        # their own bounded requests; this outer deadline also covers local
        # parsing and unexpected third-party code.
        with _services().AGENT_TOOL_EXECUTOR_LOCK:
            if _services().AGENT_TOOL_EXECUTOR is None:
                _services().AGENT_TOOL_EXECUTOR = _services().ThreadPoolExecutor(
                    max_workers=4, thread_name_prefix="research-agent-tool"
                )
            executor = _services().AGENT_TOOL_EXECUTOR
        timeout_seconds = max(
            1,
            int(
                _services()
                .AGENT_TOOL_SPECS.get(tool_name, {})
                .get("timeout_seconds", 120)
            ),
        )
        future = executor.submit(_services().call_agent_tool, tool_name, payload)
        try:
            result = future.result(timeout=timeout_seconds)
        except TimeoutError as exc:
            future.cancel()
            raise TimeoutError(
                f"工具 {tool_name} 超过 {timeout_seconds} 秒执行预算"
            ) from exc
    except Exception as exc:
        detail = _services().clean_text(str(exc), 1200)
        with _services().DB_LOCK, _services().db_session() as db:
            db.execute(
                "UPDATE agent_tool_calls SET status='failed',error=?,completed_at=?,duration_ms=? WHERE id=?",
                (
                    detail,
                    _services().now_iso(),
                    round((_services().time.perf_counter() - started) * 1000, 2),
                    call_id,
                ),
            )
        raise
    safe_result = _services()._bounded_agent_payload(result)
    with _services().DB_LOCK, _services().db_session() as db:
        db.execute(
            "UPDATE agent_tool_calls SET status='success',output_json=?,completed_at=?,duration_ms=? WHERE id=?",
            (
                _services().json.dumps(safe_result, ensure_ascii=False),
                _services().now_iso(),
                round((_services().time.perf_counter() - started) * 1000, 2),
                call_id,
            ),
        )
    return result


def _agent_step_needs_confirmation(
    step: dict, payload: dict, current: dict, prior: dict
) -> bool:
    """Resolve confirmation at the point where a step can actually act.

    A plan may contain optional model drafting. Local deterministic drafting is
    safe to run automatically; page-aware full-text analysis and mutations are
    paused only when the required data/action is really available.
    """
    consented = (current.get("budget") or {}).get("consent_external") is True
    if step.get("tool_class") in {"Apply", "External"}:
        return not consented
    if not step.get("requires_confirmation"):
        return False
    if consented:
        return False
    tool_name = step.get("tool_name")
    if tool_name == "generate_research_brief":
        # The tool produces a local, metadata-only draft without consent. The
        # optional remote model branch checks the run budget itself.
        return False
    if tool_name == "draft_reading_card":
        paper = payload.get("paper") if isinstance(payload.get("paper"), dict) else {}
        paper_id = _services().clean_text(
            paper.get("local_paper_id") or paper.get("id", ""), 200
        )
        if not paper_id or paper_id.startswith("candidate:"):
            return False
        prepared = prior.get("prepare_fulltext", {}) if isinstance(prior, dict) else {}
        if isinstance(prepared, dict) and int(prepared.get("ready_count") or 0) <= 0:
            return False
        with _services().DB_LOCK, _services().db_session() as db:
            has_pdf = bool(
                db.execute(
                    "SELECT 1 FROM attachments WHERE paper_id=? LIMIT 1", (paper_id,)
                ).fetchone()
            )
        return has_pdf and bool(
            _services().model_settings().get("enabled") and _services().model_api_key()
        )
    return True


def _agent_completion_ok(step: dict, result: dict) -> tuple[bool, str]:
    if not isinstance(result, dict):
        return False, "工具没有返回 JSON 对象"
    schema_errors = _services()._agent_schema_errors(
        step.get("output_schema") or {}, result
    )
    if schema_errors:
        return False, "输出结构校验失败：" + "；".join(schema_errors[:4])
    completion = step.get("completion") or {}
    for field in completion.get("required", []):
        present, _value = _services()._agent_completion_path_required(
            result, str(field)
        )
        if not present:
            return False, f"缺少完成条件字段：{field}"
    allowed_values = completion.get("allowed_values") or {}
    for field, allowed in allowed_values.items():
        value = _services()._agent_schema_path_value(result, str(field))
        if (
            value is not _services()._AGENT_SCHEMA_MISSING
            and isinstance(allowed, list)
            and value not in allowed
        ):
            return False, f"Output field {field} has a disallowed status: {value}"
    # A queued or failed asynchronous task is not a completed Agent step.
    if result.get("status") in {
        "queued",
        "running",
        "failed",
        "cancelled",
        "interrupted",
    }:
        return False, f"Async task is not complete: {result.get('status')}"
    min_items = completion.get("min_items")
    if min_items is not None:
        candidates = (
            result.get("papers")
            or result.get("candidates")
            or result.get("claims")
            or result.get("groups")
            or []
        )
        if len(candidates) < int(min_items):
            return False, f"结果数量 {len(candidates)} 小于完成条件 {min_items}"
    for field, minimum in (completion.get("min_items_by_field") or {}).items():
        value = _services()._agent_schema_path_value(result, str(field))
        if (
            value is _services()._AGENT_SCHEMA_MISSING
            or not isinstance(value, list)
            or len(value) < int(minimum)
        ):
            count = len(value) if isinstance(value, list) else 0
            return False, f"字段 {field} 数量 {count} 小于完成条件 {minimum}"
    return True, ""


def _agent_restart_refinement_cycle(run_id: str, current: dict) -> bool:
    """Reset one finite evidence-refinement suffix of the DAG.

    The planner may discover that the first synthesis still has open evidence
    gaps. Resetting only the suffix after `resolve_evidence_gaps` reuses the
    expensive search/PDF work, preserves all prior outputs for audit, and
    cannot loop indefinitely because the counter is persisted in the run
    budget.
    """
    budget = dict(current.get("budget") or {})
    round_no = int(budget.get("refinement_round") or 0)
    maximum = min(2, max(0, int(budget.get("max_refinement_rounds", 1))))
    if round_no >= maximum:
        return False
    stop_step = next(
        (
            step
            for step in current.get("steps", [])
            if step.get("step_key") == "evaluate_stop_conditions"
        ),
        None,
    )
    stop_output = stop_step.get("output") if isinstance(stop_step, dict) else {}
    if not isinstance(stop_output, dict) or stop_output.get("stop") is not False:
        return False
    suffix_keys = {
        "resolve_evidence_gaps",
        "verify_evidence_refined",
        "critique_claims_refined",
        "compare_candidates",
        "resolve_comparison_gaps",
        "refresh_comparison",
        "verify_comparison_evidence",
        "critique_claims_final",
        "reading_route",
        "synthesize_map",
        "draft_review",
        "check_citation_completeness",
        "evaluate_stop_conditions",
    }
    reset = [
        step for step in current.get("steps", []) if step.get("step_key") in suffix_keys
    ]
    if not reset:
        return False
    now = _services().now_iso()
    budget["refinement_round"] = round_no + 1
    with _services().DB_LOCK, _services().db_session() as db:
        db.execute(
            "UPDATE agent_runs SET budget_json=?,status='running',error='',current_step_id='',updated_at=? WHERE id=?",
            (_services().json.dumps(budget, ensure_ascii=False), now, run_id),
        )
        for step in reset:
            message = f"受限证据收敛轮次 {round_no + 1}"
            db.execute(
                "UPDATE agent_steps SET status='queued',output_json='',input_json='',error='',message=?,attempts=0,started_at='',completed_at='',updated_at=? WHERE id=? AND status NOT IN ('cancelled', 'skipped')",
                (message, now, step["id"]),
            )
    return True


def _agent_build_step_payload(
    step: dict, current: dict, goal: dict, prior: dict, run_id: str
) -> dict:
    """Build one step's immutable input snapshot from completed DAG ancestors."""
    payload = {"goal": goal, "plan": current.get("plan", {}), "previous": prior}
    payload["run_id"] = run_id
    payload["step_id"] = step["id"]
    payload["confirm"] = bool((current.get("budget") or {}).get("consent_external"))
    key = step["step_key"]
    if key == "search_candidates":
        payload["query_plan"] = prior.get("interpret_goal", {})
    if key == "resolve_identity":
        payload["papers"] = prior.get("search_candidates", {}).get("papers", [])
    if key == "triage_candidates" or step["tool_name"] == "rank_candidates":
        payload["papers"] = prior.get("search_candidates", {}).get("papers", [])
        payload["local_context"] = prior.get("search_local_context", {})
    if key == "merge_versions":
        payload["papers"] = prior.get("search_candidates", {}).get("papers", [])
        payload["identity"] = prior.get("resolve_identity", {})
    if key == "search_local_context":
        payload["query"] = goal.get("core_question") or goal.get("objective", "")
        payload["limit"] = 20
    if key == "evaluate_coverage":
        payload["triage"] = prior.get("triage_candidates", {})
    if key == "expand_search":
        payload["coverage"] = prior.get("evaluate_coverage", {})
    if key == "expand_citations":
        payload["papers"] = list(
            prior.get("triage_candidates", {}).get("candidates", [])
        )
        expanded = prior.get("expand_search", {}).get("papers", [])
        if isinstance(expanded, list):
            payload["papers"] = _services()._unique_agent_papers(
                payload["papers"], expanded, limit=50
            )
        payload["limit"] = 24
    if key == "extract_claims":
        payload["papers"] = prior.get("triage_candidates", {}).get("candidates", [])
        if prior.get("expand_search", {}).get("papers"):
            payload["papers"] = _services()._unique_agent_papers(
                payload["papers"], prior["expand_search"]["papers"], limit=50
            )
        if prior.get("expand_citations", {}).get("papers"):
            payload["papers"] = _services()._unique_agent_papers(
                payload["papers"], prior["expand_citations"]["papers"], limit=50
            )
    if key == "resolve_evidence_gaps":
        payload["claims"] = prior.get("extract_claims", {}).get("claims", [])
        payload["verification"] = prior.get("verify_evidence", {})
        payload["round"] = 1
    if key == "resolve_comparison_gaps":
        payload["papers"] = list(
            prior.get("triage_candidates", {}).get("candidates", [])
        )
        expanded = prior.get("expand_search", {}).get("papers", [])
        if isinstance(expanded, list):
            payload["papers"] = _services()._unique_agent_papers(
                payload["papers"], expanded, limit=50
            )
        payload["claims"] = _services()._unique_agent_claims(
            prior.get("extract_claims", {}).get("claims", [])
        )
        payload["comparison"] = prior.get("refresh_comparison") or prior.get(
            "compare_candidates", {}
        )
    if key in {"critique_claims_refined", "critique_claims_final"}:
        payload["claims"] = prior.get("extract_claims", {}).get("claims", [])
        payload["verification"] = prior.get("verify_evidence_refined", {})
        if key == "critique_claims_final":
            payload["claims"] = _services()._unique_agent_claims(
                payload["claims"],
                prior.get("resolve_comparison_gaps", {}).get("claims", []),
            )
            payload["verification"] = prior.get("verify_comparison_evidence", {})
    if key == "paper_analysis_overview":
        payload["papers"] = list(
            prior.get("triage_candidates", {}).get("candidates", [])
        )
        expanded = prior.get("expand_search", {}).get("papers", [])
        if isinstance(expanded, list):
            payload["papers"] = _services()._unique_agent_papers(
                payload["papers"], expanded, limit=8
            )
        cited = prior.get("expand_citations", {}).get("papers", [])
        if isinstance(cited, list):
            payload["papers"] = _services()._unique_agent_papers(
                payload["papers"], cited, limit=8
            )
        payload["claims"] = _services()._unique_agent_claims(
            prior.get("extract_claims", {}).get("claims", []),
            prior.get("resolve_comparison_gaps", {}).get("claims", []),
        )
        payload["verification"] = (
            prior.get("verify_comparison_evidence")
            or prior.get("verify_evidence_refined")
            or prior.get("verify_evidence", {})
        )
        payload["critique"] = (
            prior.get("critique_claims_final")
            or prior.get("critique_claims_refined")
            or prior.get("critique_claims", {})
        )
    if key == "extract_evidence":
        payload["paper_ids"] = [
            item.get("local_paper_id")
            for item in prior.get("triage_candidates", {}).get("candidates", [])
            if item.get("local_paper_id")
        ][:8]
    if key == "ocr_pages":
        payload["paper_ids"] = [
            item.get("local_paper_id")
            for item in prior.get("triage_candidates", {}).get("candidates", [])
            if item.get("local_paper_id")
        ][:8]
        payload["max_pages"] = 24
    if key == "prepare_fulltext":
        payload["papers"] = list(
            prior.get("triage_candidates", {}).get("candidates", [])
        )
        expanded = prior.get("expand_search", {}).get("papers", [])
        if isinstance(expanded, list):
            payload["papers"] = _services()._unique_agent_papers(
                payload["papers"], expanded, limit=8
            )
        cited = prior.get("expand_citations", {}).get("papers", [])
        if isinstance(cited, list):
            payload["papers"] = _services()._unique_agent_papers(
                payload["papers"], cited, limit=8
            )
    if key in {"compare_candidates", "refresh_comparison", "reading_route"}:
        payload["papers"] = list(
            prior.get("triage_candidates", {}).get("candidates", [])
        )
        expanded = prior.get("expand_search", {}).get("papers", [])
        if isinstance(expanded, list):
            payload["papers"] = _services()._unique_agent_papers(
                payload["papers"], expanded, limit=50
            )
        cited = prior.get("expand_citations", {}).get("papers", [])
        if isinstance(cited, list):
            payload["papers"] = _services()._unique_agent_papers(
                payload["papers"], cited, limit=50
            )
        payload["claims"] = _services()._unique_agent_claims(
            prior.get("extract_claims", {}).get("claims", []),
            prior.get("resolve_comparison_gaps", {}).get("claims", []),
        )
    if key == "draft_reading_card":
        candidates = list(prior.get("triage_candidates", {}).get("candidates", []))
        expanded = prior.get("expand_search", {}).get("papers", [])
        if isinstance(expanded, list):
            candidates = _services()._unique_agent_papers(
                candidates, expanded, limit=50
            )
        cited = prior.get("expand_citations", {}).get("papers", [])
        if isinstance(cited, list):
            candidates = _services()._unique_agent_papers(candidates, cited, limit=50)
        focused = _services()._agent_focus_papers(candidates, limit=1)
        payload["paper"] = focused[0] if focused else {}
    if key in {
        "synthesize_map",
        "draft_review",
        "follow_up_map",
        "evaluate_stop_conditions",
    }:
        payload["triage"] = prior.get("triage_candidates", {})
        payload["run"] = current
        payload["claims"] = _services()._unique_agent_claims(
            prior.get("extract_claims", {}).get("claims", []),
            prior.get("resolve_comparison_gaps", {}).get("claims", []),
        )
        payload["comparison"] = prior.get("compare_candidates", {})
        payload["reading_route"] = prior.get("reading_route", {})
        payload["reading_card_draft"] = prior.get("draft_reading_card", {})
        payload["follow_up"] = prior.get("follow_up_map", {})
        payload["verification"] = (
            prior.get("verify_comparison_evidence")
            or prior.get("verify_evidence_refined")
            or prior.get("verify_evidence", {})
        )
        payload["critique"] = (
            prior.get("critique_claims_final")
            or prior.get("critique_claims_refined")
            or prior.get("critique_claims", {})
        )
        payload["paper_analysis_overview"] = prior.get("paper_analysis_overview", {})
    return payload


def _agent_execute_step_attempt(
    run_id: str, step: dict, goal: dict, payload: dict, attempt: int
) -> None:
    try:
        result = _services()._agent_execute_tool(
            run_id, step["id"], step["tool_name"], step["tool_class"], payload
        )
        complete, completion_error = _services()._agent_completion_ok(step, result)
        if not complete:
            _services()._agent_record_validation_failure(
                run_id, step["id"], completion_error, result
            )
            raise RuntimeError(completion_error)
        output = _services()._bounded_agent_payload(result)
        _services()._agent_update_step(
            run_id,
            step["id"],
            status="success",
            output_json=_services().json.dumps(output, ensure_ascii=False),
            completed_at=_services().now_iso(),
        )
        if step["step_key"] in {
            "synthesize_map",
            "draft_review",
            "follow_up_map",
            "critique_claims",
            "critique_claims_refined",
            "critique_claims_final",
        }:
            artifact_id = str(_services().uuid.uuid4())
            with _services().DB_LOCK, _services().db_session() as db:
                artifact_type = result.get("artifact_type") or (
                    "critique"
                    if step["step_key"].startswith("critique_claims")
                    else "research_map"
                )
                version = db.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM agent_artifacts WHERE goal_id=? AND artifact_type=?",
                    (goal["id"], artifact_type),
                ).fetchone()[0]
                db.execute(
                    "INSERT INTO agent_artifacts(id,goal_id,run_id,step_id,artifact_type,title,content_json,version,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        artifact_id,
                        goal["id"],
                        run_id,
                        step["id"],
                        artifact_type,
                        result.get("title", goal["name"]),
                        _services().json.dumps(output, ensure_ascii=False),
                        version,
                        _services().now_iso(),
                    ),
                )
    except Exception as exc:
        detail = _services().clean_text(str(exc), 1200)
        max_attempts = int(step.get("max_attempts") or 2)
        if attempt < max_attempts:
            _services()._agent_update_step(
                run_id,
                step["id"],
                status="queued",
                error=f"第 {attempt} 次失败，将重试：{detail}",
                completed_at="",
                message="等待自动重试",
            )
        else:
            _services()._agent_update_step(
                run_id,
                step["id"],
                status="failed",
                error=detail,
                completed_at=_services().now_iso(),
                message="已达到重试上限",
            )


def _agent_execute_ready_wave(
    run_id: str, current: dict, pending: list[dict], by_key: dict, max_calls: int
) -> str:
    stale_running = [item for item in pending if item.get("status") == "running"]
    if stale_running:
        detail = "检测到没有活动工作线程的运行中步骤，已停止以避免重复执行"
        with _services().DB_LOCK, _services().db_session() as db:
            now = _services().now_iso()
            for item in stale_running:
                db.execute(
                    "UPDATE agent_steps SET status='failed',error=?,message=?,updated_at=?,completed_at=? WHERE id=? AND status='running'",
                    (detail, "执行状态需要人工检查", now, now, item["id"]),
                )
        _services()._agent_update_run(
            run_id,
            status="partial",
            error=detail,
            completed_at=_services().now_iso(),
            current_step_id="",
        )
        return "done"

    ready = [
        item
        for item in pending
        if item.get("status") == "queued"
        and all(
            by_key.get(dep, {}).get("status") == "success"
            for dep in (item.get("dependencies") or [])
        )
    ]
    if not ready:
        blocked = [
            item
            for item in pending
            if item.get("status") == "queued"
            and any(
                by_key.get(dep, {}).get("status") in {"failed", "skipped", "cancelled"}
                for dep in (item.get("dependencies") or [])
            )
        ]
        if blocked:
            with _services().DB_LOCK, _services().db_session() as db:
                for item in blocked:
                    db.execute(
                        "UPDATE agent_steps SET status='skipped',error=?,updated_at=?,completed_at=? WHERE id=?",
                        (
                            "依赖步骤未完成，已跳过",
                            _services().now_iso(),
                            _services().now_iso(),
                            item["id"],
                        ),
                    )
            return "continue"
        awaiting = [
            item
            for item in current.get("steps", [])
            if item.get("status") == "awaiting_confirmation"
        ]
        if awaiting:
            _services()._agent_update_run(
                run_id,
                status="awaiting_confirmation",
                error="等待用户确认后继续",
                current_step_id=awaiting[0]["id"],
            )
            return "done"
        _services()._agent_update_run(
            run_id,
            status="partial",
            error="DAG 无可执行步骤，可能存在循环依赖或未满足的完成条件",
            completed_at=_services().now_iso(),
            current_step_id="",
        )
        return "done"

    with _services().DB_LOCK, _services().db_session() as db:
        goal_row = db.execute(
            "SELECT * FROM research_goals WHERE id=?", (current["goal"]["id"],)
        ).fetchone()
        goal = _services().research_goal_snapshot(goal_row)
        prior = {key: _services()._agent_step_output(db, run_id, key) for key in by_key}
        calls_done = int(
            db.execute(
                "SELECT COUNT(*) FROM agent_tool_calls WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        )
    available_calls = max_calls - calls_done
    if available_calls <= 0:
        with _services().DB_LOCK, _services().db_session() as db:
            db.execute(
                "UPDATE agent_steps SET status='skipped',error=?,message=?,completed_at=?,updated_at=? WHERE run_id=? AND status='queued'",
                (
                    "达到工具调用预算",
                    "已达到运行预算",
                    _services().now_iso(),
                    _services().now_iso(),
                    run_id,
                ),
            )
        _services()._agent_update_run(
            run_id,
            status="partial",
            error=f"达到工具调用预算（{max_calls} 次）",
            completed_at=_services().now_iso(),
            current_step_id="",
        )
        return "done"

    worker_limit = min(_services().AGENT_STEP_CONCURRENCY, available_calls)
    work, awaiting = [], []
    for step in ready:
        payload = _services()._agent_build_step_payload(
            step, current, goal, prior, run_id
        )
        _services()._agent_update_step(
            run_id,
            step["id"],
            input_json=_services().json.dumps(
                _services()._bounded_agent_payload(payload, 80000), ensure_ascii=False
            ),
        )
        if _services()._agent_step_needs_confirmation(step, payload, current, prior):
            _services()._agent_update_step(
                run_id,
                step["id"],
                status="awaiting_confirmation",
                message="此步骤需要明确确认后才会访问外部模型或执行外部写入",
                error="",
                started_at="",
            )
            awaiting.append(step)
            continue
        if len(work) >= worker_limit:
            break
        attempt = int(step.get("attempts") or 0) + 1
        _services()._agent_update_step(
            run_id,
            step["id"],
            status="running",
            started_at=_services().now_iso(),
            attempts=attempt,
            error="",
            message="开始执行",
        )
        work.append((step, payload, attempt))

    if not work:
        existing_awaiting = [
            item
            for item in current.get("steps", [])
            if item.get("status") == "awaiting_confirmation"
        ]
        awaiting = awaiting or existing_awaiting
        if awaiting:
            _services()._agent_update_run(
                run_id,
                status="awaiting_confirmation",
                error="等待用户确认后继续",
                current_step_id=awaiting[0]["id"],
            )
            return "done"
        return "continue"

    _services()._agent_update_run(
        run_id, status="running", current_step_id=work[0][0]["id"], error=""
    )
    with _services().ThreadPoolExecutor(
        max_workers=len(work), thread_name_prefix="agent-dag"
    ) as pool:
        futures = [
            pool.submit(
                _services()._agent_execute_step_attempt,
                run_id,
                step,
                goal,
                payload,
                attempt,
            )
            for step, payload, attempt in work
        ]
        for future in _services().as_completed(futures):
            future.result()
    return "continue"


def run_agent_workflow(run_id: str) -> None:
    """Run ready DAG steps in bounded waves and persist every state change."""
    with _services().AGENT_ACTIVE_RUNS_LOCK:
        if run_id in _services().AGENT_ACTIVE_RUNS:
            return
        _services().AGENT_ACTIVE_RUNS.add(run_id)

    try:
        run = _services().agent_run_snapshot(run_id)
        if not run or run["status"] not in {
            "awaiting_confirmation",
            "queued",
            "running",
        }:
            return
        started_at = run.get("started_at") or _services().now_iso()
        started_perf = _services().time.perf_counter()
        budget = run.get("budget") or {}
        max_runtime = max(30, int(budget.get("max_runtime_seconds", 900)))
        max_calls = max(1, int(budget.get("max_tool_calls", 40)))
        _services()._agent_update_run(
            run_id, status="running", started_at=started_at, error=""
        )

        if run.get("workflow") == "autonomous":
            _services().execute_autonomous_run(run_id)
            return

        while True:
            current = _services().agent_run_snapshot(run_id)
            if not current:
                return
            status = current.get("status")
            if status in {"paused", "cancelled", "success", "partial", "failed"}:
                return
            if _services().time.perf_counter() - started_perf > max_runtime:
                with _services().DB_LOCK, _services().db_session() as db:
                    now = _services().now_iso()
                    db.execute(
                        "UPDATE agent_steps SET status='skipped',error=?,message=?,completed_at=?,updated_at=? WHERE run_id=? AND status='queued'",
                        ("达到总运行时间预算", "已达到运行预算", now, now, run_id),
                    )
                _services()._agent_update_run(
                    run_id,
                    status="partial",
                    error=f"达到总运行时间预算（{max_runtime} 秒）",
                    completed_at=_services().now_iso(),
                    current_step_id="",
                )
                return

            by_key = {item["step_key"]: item for item in current.get("steps", [])}
            pending = [
                item
                for item in current.get("steps", [])
                if item.get("status") in {"queued", "running"}
            ]
            if pending:
                outcome = _services()._agent_execute_ready_wave(
                    run_id, current, pending, by_key, max_calls
                )
                if outcome == "done":
                    return
                continue

            awaiting = [
                item
                for item in current.get("steps", [])
                if item.get("status") == "awaiting_confirmation"
            ]
            if awaiting:
                _services()._agent_update_run(
                    run_id,
                    status="awaiting_confirmation",
                    error="等待用户确认后继续",
                    current_step_id=awaiting[0]["id"],
                )
                return

            failed = [
                item
                for item in current.get("steps", [])
                if item.get("status") == "failed"
            ]
            if failed:
                _services()._agent_update_run(
                    run_id,
                    status="partial",
                    error=f"有 {len(failed)} 个步骤失败，结果仅部分完成",
                    completed_at=_services().now_iso(),
                    current_step_id="",
                )
                return

            if _services()._agent_restart_refinement_cycle(run_id, current):
                continue

            stop_step = next(
                (
                    item
                    for item in current.get("steps", [])
                    if item.get("step_key") == "evaluate_stop_conditions"
                ),
                None,
            )
            stop_output = stop_step.get("output") if isinstance(stop_step, dict) else {}
            unresolved = (
                stop_output.get("blocking_reasons")
                if isinstance(stop_output, dict)
                else []
            )
            if (
                isinstance(stop_output, dict)
                and stop_output.get("stop") is False
                and unresolved
            ):
                message = "停止条件仍有未解决项：" + "；".join(
                    str(item) for item in unresolved[:3]
                )
                budget = dict(current.get("budget") or {})
                budget["completion_state"] = "partial"
                with _services().DB_LOCK, _services().db_session() as db:
                    db.execute(
                        "UPDATE agent_runs SET budget_json=?,error=?,updated_at=? WHERE id=?",
                        (
                            _services().json.dumps(budget, ensure_ascii=False),
                            message,
                            _services().now_iso(),
                            run_id,
                        ),
                    )

            _services()._agent_update_run(
                run_id,
                status="success",
                current_step_id="",
                completed_at=_services().now_iso(),
            )
            with _services().DB_LOCK, _services().db_session() as db:
                goal_id = db.execute(
                    "SELECT goal_id FROM agent_runs WHERE id=?", (run_id,)
                ).fetchone()[0]
                db.execute(
                    "UPDATE research_goals SET current_stage='artifact_ready',updated_at=? WHERE id=?",
                    (_services().now_iso(), goal_id),
                )
                _services().record_audit(
                    db, "agent.run_completed", "agent_run", run_id, {"goal_id": goal_id}
                )
            return
    except Exception as exc:
        current = _services().agent_run_snapshot(run_id, include_steps=False)
        if current and current.get("status") not in {
            "paused",
            "cancelled",
            "success",
            "partial",
            "failed",
        }:
            _services()._agent_update_run(
                run_id,
                status="failed",
                error=_services().clean_text(str(exc), 1200),
                completed_at=_services().now_iso(),
                current_step_id="",
            )
    finally:
        with _services().AGENT_ACTIVE_RUNS_LOCK:
            _services().AGENT_ACTIVE_RUNS.discard(run_id)


__all__ = [
    "_agent_update_step",
    "_agent_update_run",
    "_agent_schema_type_name",
    "_agent_schema_type_matches",
    "_agent_schema_path_value",
    "_agent_schema_errors",
    "_agent_completion_path_required",
    "_agent_record_validation_failure",
    "_agent_execute_tool",
    "_agent_step_needs_confirmation",
    "_agent_completion_ok",
    "_agent_restart_refinement_cycle",
    "_agent_build_step_payload",
    "_agent_execute_step_attempt",
    "_agent_execute_ready_wave",
    "run_agent_workflow",
]
