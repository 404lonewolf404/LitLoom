from __future__ import annotations
from ..services import app_services as _services


class _HandlerAIMixin:
    def agent_brief(self, paper_id: str, data: dict):
        if not data.get("consent"):
            raise ValueError(
                "请先确认允许将这篇论文的元数据/摘要发送到已配置的模型服务"
            )
        settings = _services().model_settings()
        api_key = _services().model_api_key()
        if not api_key or not settings["enabled"]:
            return self.send_json(
                {
                    "error": "尚未启用模型服务。请在页面的模型配置中填写 OpenAI 兼容 API Key 并启用，或设置 LIT_AGENT_API_KEY。"
                },
                503,
            )
        try:
            result = _services().generate_abstract_brief_analysis(paper_id)
        except LookupError as exc:
            return self.send_json({"error": str(exc)}, 404)
        except Exception as exc:
            return self.send_json(
                {
                    "error": f"摘要助读失败（已自动重试）：{_services().model_error_detail(exc, api_key)}"
                },
                502,
            )
        return self.send_json(result)

    def _fulltext_ready(self, data: dict, paper_id: str = "") -> dict | None:
        if not data.get("consent"):
            raise ValueError("请先确认允许将这篇论文的全文发送到已配置的模型服务")
        settings = _services().model_settings()
        api_key = _services().model_api_key()
        if not api_key or not settings["enabled"]:
            self.send_json(
                {
                    "error": "尚未启用模型服务。请在模型配置中填写 OpenAI 兼容 API Key 并启用，或设置 LIT_AGENT_API_KEY。"
                },
                503,
            )
            return None
        if paper_id and not _services().paper_has_fulltext_source(paper_id):
            self.send_json(
                {
                    "error": "这篇论文还没有可用的本地 PDF 或已提取全文；请先添加或下载 PDF，再开始全文分析。"
                },
                400,
            )
            return None
        return settings

    def ai_reading_card(self, paper_id: str, data: dict):
        if self._fulltext_ready(data, paper_id) is None:
            return
        try:
            result = _services().run_fulltext_ai_analysis(
                paper_id,
                "reading_card",
                data.get("template", "fulltext_reading_card"),
                data.get("language", "zh-CN"),
                visual_enabled=False,
            )
        except LookupError as exc:
            return self.send_json({"error": str(exc)}, 404)
        except ValueError as exc:
            message = str(exc)
            status = 502 if "模型输出" in message or "JSON" in message else 400
            return self.send_json({"error": message}, status)
        except Exception as exc:
            api_key = _services().model_api_key()
            return self.send_json(
                {
                    "error": f"全文阅读卡分析失败：{_services().model_error_detail(exc, api_key)}"
                },
                502,
            )
        # A generated reading card is useful immediately.  Apply empty fields
        # by default while keeping ``apply:false`` available for API clients
        # that explicitly want a review-only draft.
        applied = None
        should_apply = data.get("apply", True) is not False
        if should_apply:
            with _services().DB_LOCK, _services().db_session() as db:
                row = db.execute(
                    "SELECT * FROM reading_cards WHERE paper_id=?", (paper_id,)
                ).fetchone()
                existing = _services().reading_card_dict(row, paper_id)
                merged = _services().merge_ai_reading_card(
                    existing,
                    result.get("draft", {}),
                    bool(data.get("merge_empty", True)),
                )
                applied = _services().save_reading_card(db, paper_id, merged, "ai")
                _services().record_audit(
                    db,
                    "reading_card.ai_applied",
                    "paper",
                    paper_id,
                    {
                        "artifact_id": result["artifact_id"],
                        "merge_empty": bool(data.get("merge_empty", True)),
                    },
                )
        return self.send_json(
            {**result, "applied": applied, "auto_applied": bool(should_apply)}
        )

    def ai_deep_read(self, paper_id: str, data: dict):
        if self._fulltext_ready(data, paper_id) is None:
            return
        try:
            visual_enabled = data.get("visual_enabled", True)
            if not isinstance(visual_enabled, bool):
                raise ValueError("visual_enabled 必须是布尔值")
            result = _services().run_fulltext_ai_analysis(
                paper_id,
                "deep_read",
                data.get("template", "graduate_deep_read"),
                data.get("language", "zh-CN"),
                visual_enabled=visual_enabled,
            )
        except LookupError as exc:
            return self.send_json({"error": str(exc)}, 404)
        except ValueError as exc:
            message = str(exc)
            status = 502 if "模型输出" in message or "JSON" in message else 400
            return self.send_json({"error": message}, status)
        except Exception as exc:
            api_key = _services().model_api_key()
            return self.send_json(
                {
                    "error": f"论文精读失败：{_services().model_error_detail(exc, api_key)}"
                },
                502,
            )
        return self.send_json(result)

    def apply_ai_reading_card(self, paper_id: str, data: dict):
        artifact_id = _services().clean_text(data.get("artifact_id", ""), 100)
        if not artifact_id:
            raise ValueError("缺少待应用的阅读卡分析 ID")
        with _services().DB_LOCK, _services().db_session() as db:
            row = db.execute(
                "SELECT answer FROM ai_artifacts WHERE id=? AND paper_id=? AND artifact_type='reading_card_draft'",
                (artifact_id, paper_id),
            ).fetchone()
            if not row:
                return self.send_json(
                    {"error": "阅读卡分析不存在或不属于这篇论文"}, 404
                )
            analysis = _services().parse_model_json(row["answer"])
            if isinstance(analysis.get("analysis"), dict):
                analysis = analysis["analysis"]
            draft = analysis.get("reading_card", analysis)
            current_row = db.execute(
                "SELECT * FROM reading_cards WHERE paper_id=?", (paper_id,)
            ).fetchone()
            current = _services().reading_card_dict(current_row, paper_id)
            merged = _services().merge_ai_reading_card(
                current, draft, bool(data.get("merge_empty", True))
            )
            card = _services().save_reading_card(db, paper_id, merged, "ai")
            _services().record_audit(
                db,
                "reading_card.ai_applied",
                "paper",
                paper_id,
                {
                    "artifact_id": artifact_id,
                    "merge_empty": bool(data.get("merge_empty", True)),
                },
            )
        return self.send_json(
            {"applied": True, "artifact_id": artifact_id, "reading_card": card}
        )
