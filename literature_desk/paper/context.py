"""PDF page context, visual page selection, and bounded model input."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import threading

from ..services import app_services as _services


def render_pdf_page_visual(
    attachment: dict, page_number: int, dpi: int | None = None
) -> dict:
    """Render one PDF page to a compact JPEG for a vision model.

    Rendering is a derived cache and never replaces the original PDF.  The
    function is intentionally optional: installations without PyMuPDF retain
    text-only analysis and expose the precise reason in ``visual_failures``.
    """
    if dpi is None:
        dpi = _services().VISUAL_RENDER_DPI
    cache = _services()._visual_cache_path(attachment, page_number)
    _services().active_visuals_dir().mkdir(parents=True, exist_ok=True)
    if cache.is_file() and cache.stat().st_size > 100:
        raw = cache.read_bytes()
        return {
            "attachment_id": attachment["id"],
            "page": int(page_number),
            "path": str(cache),
            "bytes": len(raw),
            "sha256": _services().hashlib.sha256(raw).hexdigest(),
            "data_url": "data:image/jpeg;base64,"
            + _services().base64.b64encode(raw).decode("ascii"),
            "cached": True,
        }
    target = (
        _services().active_files_dir()
        / _services().Path(attachment["stored_name"]).name
    )
    if not target.is_file():
        raise ValueError("PDF 附件文件不存在")
    try:
        import fitz  # PyMuPDF, optional but preferred for layout-faithful rendering
    except ImportError as exc:
        raise ValueError(
            "未安装 PyMuPDF，无法识别公式、图片和表格；请运行 pip install PyMuPDF"
        ) from exc
    try:
        # Opening/rendering several large PDFs at once can briefly consume
        # more memory than the model call itself. Keep this budget separate
        # from the model-request budget and let other work continue.
        with _services()._pdf_render_slot():
            document = fitz.open(str(target))
            try:
                index = int(page_number) - 1
                if index < 0 or index >= document.page_count:
                    raise ValueError(f"PDF 页码超出范围：{page_number}")
                page = document.load_page(index)
                scale = max(1.0, min(2.5, float(dpi) / 72.0))
                pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                raw = pixmap.tobytes(
                    "jpeg", jpg_quality=_services().VISUAL_JPEG_QUALITY
                )
            finally:
                document.close()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"PDF 页面渲染失败：{str(exc)[:240]}") from exc
    cache.write_bytes(raw)
    return {
        "attachment_id": attachment["id"],
        "page": int(page_number),
        "path": str(cache),
        "bytes": len(raw),
        "sha256": _services().hashlib.sha256(raw).hexdigest(),
        "data_url": "data:image/jpeg;base64,"
        + _services().base64.b64encode(raw).decode("ascii"),
        "cached": False,
    }


def prepare_visual_pages(rows: list[dict], max_pages: int = 8) -> dict:
    """Render representative pages for one multimodal chunk.

    A chunk may contain many pages.  Sending at most eight high-resolution
    pages keeps request size predictable while retaining every page reference
    in the text payload.  Pages are selected in order and deduplicated by
    attachment/page identity.
    """
    visuals, failures, seen = [], [], set()
    attachments = {}
    with _services().DB_LOCK, _services().db_session() as db:
        ids = sorted(
            {
                str(row.get("attachment_id", ""))
                for row in rows
                if row.get("attachment_id")
            }
        )
        if ids:
            marks = ",".join("?" for _ in ids)
            attachments = {
                str(item["id"]): dict(item)
                for item in db.execute(
                    f"SELECT id,stored_name,sha256 FROM attachments WHERE id IN ({marks})",
                    ids,
                )
            }
    for row in rows:
        attachment_id = str(row.get("attachment_id", ""))
        page = int(row.get("page_number") or 0)
        key = (attachment_id, page)
        if not attachment_id or not page or key in seen:
            continue
        seen.add(key)
        attachment = attachments.get(attachment_id)
        if not attachment:
            failures.append(
                {
                    "attachment_id": attachment_id,
                    "page": page,
                    "error": "附件记录不存在",
                }
            )
            continue
        if len(visuals) >= max_pages:
            failures.append(
                {
                    "attachment_id": attachment_id,
                    "page": page,
                    "error": "页面数量超过本次视觉上下文上限",
                }
            )
            continue
        try:
            rendered = _services().render_pdf_page_visual(
                {**attachment, "id": attachment_id}, page
            )
            rendered["pdf_url"] = (
                "/files/"
                + _services().Path(attachment["stored_name"]).name
                + f"#page={page}"
            )
            rendered["url"] = "/visual-pages/" + _services().Path(rendered["path"]).name
            visuals.append(rendered)
        except Exception as exc:
            failures.append(
                {"attachment_id": attachment_id, "page": page, "error": str(exc)[:240]}
            )
    return {
        "images": visuals,
        "failures": failures,
        "rendered_pages": len(visuals),
        "requested_pages": len(seen),
    }


def get_paper_pages_for_ai(paper_id: str) -> tuple[dict, list[dict]]:
    """Return a paper and its page-level text, extracting missing PDFs first."""
    with _services().DB_LOCK, _services().db_session() as db:
        paper = db.execute(
            "SELECT id,title,authors,year,venue,abstract,topic FROM papers WHERE id=?",
            (paper_id,),
        ).fetchone()
        # A paper can have the same PDF attached once by auto-download and
        # once manually. Analyze one canonical attachment per content hash so
        # duplicate files cannot double model calls or visual rendering.
        attachment_rows = db.execute(
            "SELECT a.* FROM attachments a WHERE a.paper_id=? AND a.id IN ("
            "SELECT MIN(id) FROM attachments WHERE paper_id=? GROUP BY CASE WHEN sha256<>'' THEN sha256 ELSE id END"
            ") ORDER BY a.added_at",
            (paper_id, paper_id),
        ).fetchall()
    if not paper:
        raise LookupError("论文不存在")
    if not attachment_rows:
        raise ValueError("这篇论文还没有本地 PDF 附件；请先添加 PDF 才能进行全文分析")
    for attachment in attachment_rows:
        with _services().DB_LOCK, _services().db_session() as db:
            extracted = db.execute(
                "SELECT 1 FROM document_pages WHERE attachment_id=? LIMIT 1",
                (attachment["id"],),
            ).fetchone()
        if not extracted:
            _services().extract_attachment_pages(attachment["id"])
    with _services().DB_LOCK, _services().db_session() as db:
        rows = [
            dict(row)
            for row in db.execute(
                "SELECT d.attachment_id,d.page_number,d.text,d.char_count,d.quality,d.extractor,d.text_sha256,d.extracted_at, "
                "a.original_name,a.stored_name,a.sha256 AS attachment_sha256 "
                "FROM document_pages d JOIN attachments a ON a.id=d.attachment_id "
                "WHERE d.paper_id=? AND a.id IN (SELECT MIN(id) FROM attachments WHERE paper_id=? GROUP BY CASE WHEN sha256<>'' THEN sha256 ELSE id END) "
                "ORDER BY d.attachment_id,d.page_number",
                (paper_id, paper_id),
            )
        ]
    if not rows:
        raise ValueError(
            "PDF 尚未提取出可分析的逐页文本；可以尝试重新提取或使用 OCR 版 PDF"
        )
    # Downloads can differ only in PDF metadata while yielding byte-for-byte
    # identical extracted pages. File SHA-256 cannot catch that case, so use
    # the ordered page-text fingerprint as a second deduplication boundary.
    # Keep the first attachment's IDs for stable page evidence and visual refs.
    return dict(paper), _services()._deduplicate_page_rows(rows)


def _deduplicate_page_rows(rows: list[dict]) -> list[dict]:
    """Keep one extracted page set when attachments contain the same text."""
    grouped: dict[str, list[dict]] = {}
    order: list[str] = []
    for row in rows:
        attachment_id = str(row.get("attachment_id", ""))
        if not attachment_id:
            continue
        if attachment_id not in grouped:
            grouped[attachment_id] = []
            order.append(attachment_id)
        grouped[attachment_id].append(row)
    canonical: set[str] = set()
    seen_signatures: set[str] = set()
    for attachment_id in order:
        pages = grouped[attachment_id]
        signature_payload = [
            {
                "page": int(row.get("page_number") or 0),
                "text_sha256": str(row.get("text_sha256") or ""),
                "char_count": int(row.get("char_count") or 0),
                "quality": str(row.get("quality") or ""),
            }
            for row in pages
        ]
        signature = (
            _services()
            .hashlib.sha256(
                _services()
                .json.dumps(signature_payload, ensure_ascii=False, sort_keys=True)
                .encode("utf-8")
            )
            .hexdigest()
        )
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        canonical.add(attachment_id)
    return [row for row in rows if str(row.get("attachment_id", "")) in canonical]


def paper_has_local_pdf(paper_id: str) -> bool:
    """Check whether a paper has an attached PDF file without extracting it."""
    with _services().DB_LOCK, _services().db_session() as db:
        rows = db.execute(
            "SELECT stored_name FROM attachments WHERE paper_id=?", (paper_id,)
        ).fetchall()
    return any(
        bool(name)
        and (
            _services().active_files_dir() / _services().Path(str(name)).name
        ).is_file()
        for name in (row["stored_name"] for row in rows)
    )


def paper_has_fulltext_source(paper_id: str) -> bool:
    """Check for either a readable local PDF or previously extracted page text."""
    if _services().paper_has_local_pdf(paper_id):
        return True
    with _services().DB_LOCK, _services().db_session() as db:
        return bool(
            db.execute(
                "SELECT 1 FROM document_pages WHERE paper_id=? LIMIT 1", (paper_id,)
            ).fetchone()
        )


def fulltext_source_fingerprint(paper_id: str) -> str:
    """Return a stable fingerprint for the locally extracted source.

    This deliberately excludes model output and secrets.  It lets a repeated
    reading-card request reuse an unchanged artifact after a refresh or
    restart, while a replaced attachment or re-extracted page invalidates the
    cache automatically.
    """
    with _services().DB_LOCK, _services().db_session() as db:
        rows = db.execute(
            "SELECT d.attachment_id,d.page_number,d.text_sha256,d.quality,d.char_count,a.sha256 AS attachment_sha256 "
            "FROM document_pages d JOIN attachments a ON a.id=d.attachment_id "
            "WHERE d.paper_id=? AND a.id IN (SELECT MIN(id) FROM attachments WHERE paper_id=? GROUP BY CASE WHEN sha256<>'' THEN sha256 ELSE id END) "
            "ORDER BY d.attachment_id,d.page_number",
            (paper_id, paper_id),
        ).fetchall()
    rows = _services()._deduplicate_page_rows([dict(row) for row in rows])
    if not rows:
        return ""
    payload = [
        {
            "attachment_id": str(row["attachment_id"]),
            "page": int(row["page_number"] or 0),
            "text_sha256": str(row["text_sha256"] or ""),
            "quality": str(row["quality"] or ""),
            "char_count": int(row["char_count"] or 0),
            "attachment_sha256": str(row["attachment_sha256"] or ""),
        }
        for row in rows
    ]
    return (
        _services()
        .hashlib.sha256(
            _services()
            .json.dumps(payload, ensure_ascii=False, sort_keys=True)
            .encode("utf-8")
        )
        .hexdigest()
    )


def select_reading_card_context(
    rows: list[dict], max_chars: int | None = None
) -> list[dict]:
    """Select high-signal PDF pages for the quick text-only reading card.

    The full page list is retained for local citation validation. Only the
    model context is bounded, which prevents a long paper or duplicate OCR
    appendix from turning a quick card into a slow full-document request.
    """
    if max_chars is None:
        max_chars = _services().READING_CARD_CONTEXT_CHARS
    if not rows:
        return []
    budget = max(18000, min(int(max_chars), 80000))
    normalized = []
    for index, row in enumerate(rows):
        text = _services().clean_text(row.get("text", ""), 500000)
        if not text.strip():
            continue
        page = int(row.get("page_number") or 0)
        if page <= 0:
            continue
        folded = text.casefold()
        score = 0
        if page <= 3:
            score += 8
        if page >= max(int(item.get("page_number") or 0) for item in rows) - 2:
            score += 3
        cue_groups = (
            (r"abstract|summary|introduction|background|摘要|引言|背景", 7),
            (r"method|approach|architecture|algorithm|方法|模型|算法|架构|流程", 6),
            (r"dataset|data collection|benchmark|数据集|数据采集|基准", 5),
            (r"experiment|evaluation|result|ablation|实验|评估|结果|消融|对比", 6),
            (r"limitation|conclusion|future work|局限|结论|未来工作|不足", 4),
            (r"fig(?:ure)?\.?\s*\d+|table\s*\d+|图\s*\d+|表\s*\d+", 2),
        )
        for pattern, weight in cue_groups:
            if _services().re.search(pattern, folded, _services().re.I):
                score += weight
        if _services().re.search(
            r"references|bibliography|参考文献", folded[:1200], _services().re.I
        ):
            score -= 12
        normalized.append((score, page, index, row, text))
    if not normalized:
        return []

    # Always retain the opening pages for title/abstract and the highest signal
    # pages thereafter. The final result is restored to source order.
    first_pages = {item[2] for item in normalized if item[1] <= 3}
    ranked = sorted(normalized, key=lambda item: (-item[0], item[1], item[2]))
    selected = []
    selected_indices = set()
    used_chars = 0
    for item in ranked:
        score, page, index, row, text = item
        if index in selected_indices:
            continue
        # Do not spend the context budget on a references-only page unless the
        # PDF has no other usable material.
        if score < 0 and selected:
            continue
        remaining = budget - used_chars
        if remaining <= 0 and index not in first_pages:
            break
        if len(text) > remaining and index not in first_pages and remaining < 2400:
            continue
        copy = dict(row)
        if len(text) > max(2200, remaining):
            head = max(1200, remaining * 2 // 3)
            tail = max(500, remaining - head - 80)
            copy["text"] = (
                text[:head]
                + "\n[此页中间内容已省略；需要公式、图表或完整上下文请使用全文精读]"
                + text[-tail:]
            )
        else:
            copy["text"] = text
        selected.append(copy)
        selected_indices.add(index)
        used_chars += len(copy["text"])
        if used_chars >= budget:
            break
    return sorted(
        selected,
        key=lambda item: (
            int(item.get("page_number") or 0),
            str(item.get("attachment_id", "")),
        ),
    )


def build_page_chunks(rows: list[dict], max_chars: int = 9000) -> list[dict]:
    """Split extracted text while retaining an explicit page anchor per chunk."""
    # Reading-card requests intentionally use a larger single-pass budget;
    # detailed analysis continues to pass its smaller configured chunk size.
    max_chars = min(max(int(max_chars), 4000), 120000)
    chunks, current, current_pages, current_refs, current_chars = [], [], [], [], 0

    def unique_refs(values: list[dict]) -> list[dict]:
        seen = set()
        output = []
        for value in values:
            key = (str(value.get("attachment_id", "")), int(value.get("page") or 0))
            if key in seen:
                continue
            seen.add(key)
            output.append(value)
        return output

    for row in rows:
        text = _services().clean_text(row.get("text", ""), 500000)
        if not text.strip():
            continue
        page = int(row.get("page_number") or 0)
        attachment_id = str(row.get("attachment_id", ""))
        marker = (
            f"[附件 {attachment_id[:8]} 第 {page} 页]\n"
            if attachment_id
            else f"[第 {page} 页]\n"
        )
        # A single page can contain a very large table. Split it without
        # dropping the page marker so every later quote remains locatable.
        step = max_chars - len(marker) - 1
        for start in range(0, len(text), step):
            piece = marker + text[start : start + step]
            if current and current_chars + len(piece) > max_chars:
                chunks.append(
                    {
                        "text": "\n\n".join(current),
                        "pages": sorted(set(current_pages)),
                        "page_refs": unique_refs(current_refs),
                    }
                )
                current, current_pages, current_refs, current_chars = [], [], [], 0
            current.append(piece)
            current_pages.append(page)
            current_refs.append({"attachment_id": attachment_id, "page": page})
            current_chars += len(piece) + 2
    if current:
        chunks.append(
            {
                "text": "\n\n".join(current),
                "pages": sorted(set(current_pages)),
                "page_refs": unique_refs(current_refs),
            }
        )
    return chunks


def select_visual_page_rows(
    rows: list[dict], max_pages: int, mode: str = "reading_card"
) -> list[dict]:
    """Choose a bounded, useful set of page images without dropping page text.

    Reading cards are a fast first pass, so prefer pages with equations,
    figures, tables, algorithms, and experimental results. Deep reads retain
    the document-order strategy and a larger caller-provided budget.
    """
    unique_rows, seen = [], set()
    for row in rows:
        attachment_id = str(row.get("attachment_id", ""))
        try:
            page = int(row.get("page_number") or 0)
        except (TypeError, ValueError):
            page = 0
        key = (attachment_id, page)
        if not attachment_id or not page or key in seen:
            continue
        seen.add(key)
        unique_rows.append(row)
    budget = max(0, int(max_pages))
    if len(unique_rows) <= budget:
        return unique_rows[:budget]

    cue_patterns = (
        (
            _services().re.compile(
                r"\b(?:fig(?:ure)?\.?|table|tab\.?|eq(?:uation)?\.?|algorithm|alg\.?)\s*\d+",
                _services().re.I,
            ),
            5,
        ),
        (_services().re.compile(r"(?:图|表|公式|算法)\s*\d+"), 5),
        (
            _services().re.compile(
                r"\b(?:ablation|benchmark|dataset|baseline|comparison|experimental results?)\b",
                _services().re.I,
            ),
            2,
        ),
        (
            _services().re.compile(
                r"(?:消融|基准|数据集|基线|对比实验|实验结果|架构图|流程图|损失函数)"
            ),
            2,
        ),
        (
            _services().re.compile(
                r"(?:\\frac|\\sum|\\int|∑|∫|≈|≠|≤|≥|\bJ\s*=|\bL\s*=)"
            ),
            2,
        ),
    )
    # Keep the expensive image budget useful across visual types. A page that
    # contains a table and another page that contains an equation are more
    # valuable together than two pages with the same generic Figure cue.
    cue_categories = (
        (
            "figure",
            _services().re.compile(
                r"\b(?:fig(?:ure)?\.?)\s*\d+|(?:图)\s*\d+", _services().re.I
            ),
        ),
        (
            "table",
            _services().re.compile(
                r"\b(?:table|tab\.?)\s*\d+|(?:表)\s*\d+", _services().re.I
            ),
        ),
        (
            "formula",
            _services().re.compile(
                r"\b(?:eq(?:uation)?\.?)\s*\d+|(?:公式)\s*\d+|(?:\\frac|\\sum|\\int|∑|∫|≈|≠|≤|≥|\bJ\s*=|\bL\s*=)",
                _services().re.I,
            ),
        ),
        (
            "algorithm",
            _services().re.compile(
                r"\b(?:algorithm|alg\.?)\s*\d+|(?:算法)\s*\d+", _services().re.I
            ),
        ),
    )

    def score(row: dict) -> int:
        text = str(row.get("text", ""))
        score_value = sum(
            weight for pattern, weight in cue_patterns if pattern.search(text)
        )
        if str(row.get("quality", "")).casefold() in {"low", "empty"}:
            score_value += 1
        if _services().re.search(
            r"(?im)^\s*(?:references|bibliography|参考文献)\s*$", text[:1200]
        ):
            score_value -= 10
        return score_value

    def categories(row: dict) -> set[str]:
        text = str(row.get("text", ""))
        return {name for name, pattern in cue_categories if pattern.search(text)}

    scored = [
        (index, row, score(row), categories(row))
        for index, row in enumerate(unique_rows)
    ]
    ranked = sorted(scored, key=lambda item: (-item[2], -len(item[3]), item[0]))[
        :budget
    ]
    # Image input is the expensive part of a multimodal request. Pages whose
    # extracted text has no equation/figure/table/algorithm cue can use the
    # text path without changing the evidence backbone. Keep one fallback page
    # when a PDF has captions that extraction failed to recognize.
    if ranked and score(ranked[0][1]) <= 0:
        if mode == "reading_card":
            # The first pass stays cheap when the text extractor exposes no
            # useful visual cues. Deep reading uses the fallback below so a
            # PDF with broken captions or a sparse text layer is not reduced
            # to a single arbitrary image.
            ranked = ranked[:1]
        else:
            # Caption extraction is not reliable for scanned or layout-heavy
            # PDFs. Evenly sample the chunk in document order so deep reading
            # still sees representative formulas, figures, and tables.
            sample_count = min(budget, len(unique_rows))
            if sample_count <= 1:
                ranked = [
                    (index, row, score(row), categories(row))
                    for index, row in enumerate(unique_rows[:sample_count])
                ]
            else:
                positions = [
                    round(index * (len(unique_rows) - 1) / (sample_count - 1))
                    for index in range(sample_count)
                ]
                ranked = [
                    (
                        position,
                        unique_rows[position],
                        score(unique_rows[position]),
                        categories(unique_rows[position]),
                    )
                    for position in dict.fromkeys(positions)
                ]
    else:
        ranked = [item for item in ranked if item[2] > 0]
        if mode != "reading_card" and len(ranked) < budget:
            selected_keys = {
                (str(row.get("attachment_id", "")), int(row.get("page_number") or 0))
                for _, row, _score, _categories in ranked
            }
            for item in enumerate(unique_rows):
                row = item[1]
                key = (
                    str(row.get("attachment_id", "")),
                    int(row.get("page_number") or 0),
                )
                if key not in selected_keys:
                    ranked.append((item[0], row, score(row), categories(row)))
                    selected_keys.add(key)
                if len(ranked) >= budget:
                    break
    if mode != "reading_card" and len(ranked) > 1:
        # Greedily cover different cue categories while retaining the base
        # relevance score. This avoids selecting two table-caption pages when
        # a formula or figure page is available in the same chunk.
        remaining = list(ranked)
        chosen = []
        covered = set()
        while remaining and len(chosen) < budget:
            best_index, best_value = 0, -(10**9)
            for index, item in enumerate(remaining):
                _position, _row, item_score, item_categories = item
                novelty = len(item_categories - covered)
                value = item_score + novelty * 3 - (0.01 * _position)
                if value > best_value:
                    best_index, best_value = index, value
            item = remaining.pop(best_index)
            chosen.append(item)
            covered.update(item[3])
        ranked = chosen
    selected = {
        (str(row.get("attachment_id", "")), int(row.get("page_number") or 0))
        for _, row, _score, _categories in ranked
    }
    # Keep document order in the multimodal request so page evidence stays
    # predictable even though selection is relevance-ranked.
    return [
        row
        for row in unique_rows
        if (str(row.get("attachment_id", "")), int(row.get("page_number") or 0))
        in selected
    ]


def visual_page_cue_score(text: str) -> int:
    """Score textual signals that a page contains visual evidence worth checking."""
    value = str(text or "")
    patterns = (
        # Rechecks are more selective than the initial visual-page planner:
        # generic words such as "results" are not enough to justify another
        # multimodal request.
        (
            _services().re.compile(
                r"\b(?:fig(?:ure)?\.?|table|tab\.?|eq(?:uation)?\.?|algorithm|alg\.?)\s*\d+",
                _services().re.I,
            ),
            6,
        ),
        (_services().re.compile(r"(?:图|表|公式|算法)\s*\d+"), 6),
        (
            _services().re.compile(
                r"(?:\\frac|\\sum|\\int|∑|∫|≈|≠|≤|≥|\bJ\s*=|\bL\s*=)"
            ),
            4,
        ),
    )
    return sum(weight for pattern, weight in patterns if pattern.search(value))


def select_visual_recheck_rows(
    rows: list[dict],
    chunk_result: dict,
    visual: dict,
    max_pages: int,
) -> list[dict]:
    """Find rendered cue pages whose first multimodal pass gave no observation.

    This is deliberately a pure, bounded planner. It does not infer that a
    page is missing evidence merely because the final synthesis omitted it;
    the check is against the page-level chunk response, before reduction.
    """
    budget = max(0, int(max_pages or 0))
    if (
        budget <= 0
        or not isinstance(chunk_result, dict)
        or not isinstance(visual, dict)
    ):
        return []
    failures = (
        visual.get("failures") if isinstance(visual.get("failures"), list) else []
    )
    if any(
        "不接受视觉输入" in _services().clean_text(item.get("error", ""), 300)
        for item in failures
        if isinstance(item, dict)
    ):
        return []
    rendered = visual.get("images") if isinstance(visual.get("images"), list) else []
    observed = set()
    observations = chunk_result.get("visual_observations")
    if not isinstance(observations, list):
        observations = chunk_result.get("visual_evidence")
    for item in observations if isinstance(observations, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            page = int(item.get("page"))
        except (TypeError, ValueError):
            continue
        attachment_id = _services().clean_text(item.get("attachment_id", ""), 100)
        observed.add((attachment_id, page))
        # Some providers omit attachment_id when a request contains one PDF.
        # A page-only marker is still enough to avoid a duplicate recheck.
        observed.add(("", page))
    row_by_key = {
        (str(row.get("attachment_id", "")), int(row.get("page_number") or 0)): row
        for row in rows
        if row.get("attachment_id") and int(row.get("page_number") or 0)
    }
    candidates = []
    seen = set()
    # A model request can fail after the page was rendered.  The caller drops
    # that image from ``visual["images"]`` so a failed page cannot be counted as
    # visual coverage, but it still deserves one bounded retry.  Prioritize
    # those pages before ordinary cue gaps; otherwise a long document can use
    # its entire recheck budget on successful pages and permanently lose a
    # transient empty/JSON response.
    for failure in failures:
        if not isinstance(failure, dict):
            continue
        error = _services().clean_text(failure.get("error", ""), 500)
        if not error or "视觉页面分析失败" not in error:
            continue
        try:
            page = int(failure.get("page"))
        except (TypeError, ValueError):
            continue
        attachment_id = str(failure.get("attachment_id", ""))
        key = (attachment_id, page)
        row = row_by_key.get(key)
        if (
            row
            and key not in seen
            and key not in observed
            and ("", page) not in observed
        ):
            candidates.append((1000, page, row))
            seen.add(key)
    for image in rendered:
        try:
            page = int(image.get("page"))
        except (TypeError, ValueError):
            continue
        attachment_id = str(image.get("attachment_id", ""))
        key = (attachment_id, page)
        row = row_by_key.get(key)
        if not row or key in seen or key in observed or ("", page) in observed:
            continue
        score = _services().visual_page_cue_score(row.get("text", ""))
        if score > 0:
            candidates.append((score, page, row))
            seen.add(key)
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return [row for _score, _page, row in candidates[:budget]]


def _ai_note_payload(notes: list[dict]) -> dict:
    return {"notes": notes, "evidence_spans": [], "uncertainties": []}


def record_timed_model_call(
    metrics: list[dict],
    metrics_lock: threading.Lock,
    phase: str,
    kind: str,
    request,
    input_chars: int = 0,
    image_count: int = 0,
    page_numbers: list[int] | None = None,
) -> dict:
    """Measure one model round-trip without recording prompts or credentials."""
    started = _services().time.perf_counter()
    error_type = ""
    response_fields = []
    try:
        result = request()
        if isinstance(result, dict):
            response_fields = sorted(
                _services().clean_text(key, 80)
                for key in result.keys()
                if isinstance(key, str)
            )[:24]
        return result
    except Exception as exc:
        error_type = type(exc).__name__
        raise
    finally:
        item = {
            "phase": _services().clean_text(phase, 40),
            "kind": _services().clean_text(kind, 40),
            "duration_ms": round((_services().time.perf_counter() - started) * 1000, 2),
            "input_chars": max(0, int(input_chars or 0)),
            "image_count": max(0, int(image_count or 0)),
            "status": "failed" if error_type else "success",
            "error_type": error_type,
            "response_fields": response_fields,
        }
        safe_pages = []
        for value in page_numbers or []:
            try:
                page = int(value)
            except (TypeError, ValueError):
                continue
            if page > 0 and page not in safe_pages:
                safe_pages.append(page)
        if safe_pages:
            item["pages"] = safe_pages
        with metrics_lock:
            metrics.append(item)


__all__ = [
    "render_pdf_page_visual",
    "prepare_visual_pages",
    "get_paper_pages_for_ai",
    "_deduplicate_page_rows",
    "paper_has_local_pdf",
    "paper_has_fulltext_source",
    "fulltext_source_fingerprint",
    "select_reading_card_context",
    "build_page_chunks",
    "select_visual_page_rows",
    "visual_page_cue_score",
    "select_visual_recheck_rows",
    "_ai_note_payload",
    "record_timed_model_call",
]
