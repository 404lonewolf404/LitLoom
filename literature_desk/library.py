"""Paper identity, discovery memory, library, recommendations, and backups."""

from __future__ import annotations
import sqlite3

from .services import app_services as _services


def norm_doi(value: str) -> str:
    value = (value or "").strip().lower()
    value = _services().re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", value)
    return value.strip().rstrip(".,; ")


def norm_arxiv(value: str) -> str:
    value = (value or "").strip().lower()
    value = _services().re.sub(r"^(https?://)?(www\.)?arxiv\.org/(abs|pdf)/", "", value)
    value = _services().re.sub(r"\.pdf$", "", value)
    value = _services().re.sub(r"v\d+$", "", value)
    return value.strip()


def title_key(value: str) -> str:
    value = (value or "").lower()
    value = _services().re.sub(r"[^\w\s]", " ", value, flags=_services().re.UNICODE)
    return " ".join(value.split())


def relevance_tokens(value: str) -> set[str]:
    raw = (value or "").lower()
    words = _services().re.findall(r"[a-z0-9][a-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}", raw)
    terms = {
        w.strip("_-") for w in words if w.strip("_-") not in _services().REL_STOPWORDS
    }
    # Chinese titles rarely use spaces.  Keep the complete phrase for
    # explainability, then add bounded bi/tri-grams so a query such as
    # “多模态推理” can match “多模态模型推理” without an embedding service.
    for run in _services().re.findall(r"[\u4e00-\u9fff]{2,}", raw):
        if len(run) > 2 and run not in _services().REL_STOPWORDS:
            for width in (2, 3):
                if len(run) < width:
                    continue
                terms.update(
                    run[index : index + width] for index in range(len(run) - width + 1)
                )
    return {term for term in terms if term not in _services().REL_STOPWORDS}


def expanded_relevance_terms(value: str) -> set[str]:
    """Return original and curated bridge terms for multilingual ranking."""
    raw = _services().clean_text(value, 2000)
    terms = set(_services().relevance_tokens(raw))
    lowered = raw.casefold()
    for concept, aliases in _services().SEARCH_CONCEPT_ALIASES.items():
        if concept.casefold() in lowered:
            terms.update(_services().relevance_tokens(" ".join(aliases[:3])))
    for facet in _services().extract_search_facets(raw).values():
        terms.update(
            _services().relevance_tokens(" ".join(facet.get("expansions", [])[:4]))
        )
    return terms


def search_plan_terms(value: str) -> tuple[list[str], list[str]]:
    """Expose meaningful concepts to search planners without CJK n-gram noise."""
    raw = _services().clean_text(value, 1000)
    if not raw:
        return [], []
    lowered = raw.casefold()
    concepts = []
    expanded = []

    def add(target: list[str], candidate: str) -> None:
        candidate = _services().clean_text(candidate, 200)
        if candidate and candidate.casefold() not in {
            item.casefold() for item in target
        }:
            target.append(candidate)

    matched_concepts = []
    for concept in sorted(_services().SEARCH_CONCEPT_ALIASES, key=len, reverse=True):
        normalized = concept.casefold()
        if _services().re.search(r"[\u4e00-\u9fff]", concept):
            matched = normalized in lowered
        else:
            matched = bool(
                _services().re.search(
                    r"(?<![a-z0-9])"
                    + _services().re.escape(normalized)
                    + r"(?![a-z0-9])",
                    lowered,
                )
            )
        if matched:
            matched_concepts.append(concept)
            add(concepts, concept)

    facets = _services().extract_search_facets(raw)
    for facet in facets.values():
        for hit in facet.get("hits", []):
            add(concepts, hit)
    for phrase in _services().re.findall(r'"([^"\n]+)"', raw):
        add(concepts, phrase)
    english_terms = [
        term
        for term in _services().re.findall(r"[a-z0-9]+(?:[-'][a-z0-9]+)*", lowered)
        if len(term) >= 3 and term not in _services().REL_STOPWORDS
    ]
    for term in english_terms:
        add(concepts, term)

    for concept in concepts:
        add(expanded, concept)
    for concept in matched_concepts:
        for alias in _services().SEARCH_CONCEPT_ALIASES.get(concept, ()):
            add(expanded, alias)
    for facet in facets.values():
        for alias in facet.get("expansions", []):
            add(expanded, alias)
    for term in english_terms:
        add(expanded, term)
    return concepts[:24], expanded[:36]


def extract_search_facets(query: str) -> dict:
    """Extract conservative research facets from a mixed-language question."""
    raw = _services().clean_text(query, 1000)
    lowered = raw.casefold()
    facets = {}
    for facet, terms in _services().SEARCH_FACET_ALIASES.items():
        hits = []
        expansions = []
        for term, aliases in terms.items():
            if term.casefold() in lowered:
                hits.append(term)
                expansions.extend(aliases)
            elif _services().re.search(
                r"\b" + _services().re.escape(term.casefold()) + r"\b", lowered
            ):
                hits.append(term)
                expansions.extend(aliases)
        if hits:
            facets[facet] = {
                "hits": list(dict.fromkeys(hits)),
                "expansions": list(dict.fromkeys(expansions))[:8],
            }
    return facets


def _bridge_query_terms(raw: str) -> str:
    """Replace known Chinese/English concepts in one pass, longest first."""
    candidate = raw
    candidate = _services().re.sub(
        r"(?<=[A-Za-z0-9])(?=[\u4e00-\u9fff])", " ", candidate
    )
    candidate = _services().re.sub(
        r"(?<=[\u4e00-\u9fff])(?=[A-Za-z0-9])", " ", candidate
    )
    replacements = []
    for concept, aliases in _services().SEARCH_CONCEPT_ALIASES.items():
        if concept.casefold() in candidate.casefold() and aliases:
            replacements.append((concept, aliases[0]))
    for concept, alias in sorted(
        replacements, key=lambda pair: len(pair[0]), reverse=True
    ):
        candidate = _services().re.sub(
            _services().re.escape(concept),
            alias,
            candidate,
            count=1,
            flags=_services().re.IGNORECASE,
        )
    candidate = _services().re.sub(
        r"(?<=[A-Za-z0-9])(?=[\u4e00-\u9fff])", " ", candidate
    )
    candidate = _services().re.sub(
        r"(?<=[\u4e00-\u9fff])(?=[A-Za-z0-9])", " ", candidate
    )
    return _services().clean_text(candidate, 1000)


def _bridge_non_latin_terms(raw: str) -> str:
    """Bridge only Chinese terms, preserving intentional English aliases."""
    candidate = raw
    candidate = _services().re.sub(
        r"(?<=[A-Za-z0-9])(?=[\u4e00-\u9fff])", " ", candidate
    )
    candidate = _services().re.sub(
        r"(?<=[\u4e00-\u9fff])(?=[A-Za-z0-9])", " ", candidate
    )
    replacements = []
    for concept, aliases in _services().SEARCH_CONCEPT_ALIASES.items():
        if (
            _services().re.search(r"[\u4e00-\u9fff]", concept)
            and concept.casefold() in candidate.casefold()
            and aliases
        ):
            replacements.append((concept, aliases[0]))
    for concept, alias in sorted(
        replacements, key=lambda pair: len(pair[0]), reverse=True
    ):
        candidate = _services().re.sub(
            _services().re.escape(concept),
            alias,
            candidate,
            count=1,
            flags=_services().re.IGNORECASE,
        )
    candidate = _services().re.sub(
        r"(?<=[A-Za-z0-9])(?=[\u4e00-\u9fff])", " ", candidate
    )
    candidate = _services().re.sub(
        r"(?<=[\u4e00-\u9fff])(?=[A-Za-z0-9])", " ", candidate
    )
    return _services().clean_text(candidate, 1000)


def search_query_variants(
    query: str, max_variants: int = 6, depth: str = "balanced"
) -> list[dict]:
    """Build a deterministic, recall-oriented search plan.

    The primary wording is always retained.  Additional variants are simple
    provider-compatible phrases, so the union can be deduplicated across
    arXiv, OpenAlex, Crossref and Europe PMC without provider-specific syntax.
    ``focused`` keeps calls cheap, ``deep`` adds high-signal survey/benchmark
    lanes, and ``balanced`` is the default for long-running topics.
    """
    raw = _services().clean_text(query, 1000)
    if not raw:
        return []
    depth = depth if depth in _services().SEARCH_DEPTHS else "balanced"
    cap = max(1, min(int(max_variants), 12))
    if depth == "deep":
        cap = max(cap, 12)
    # Reserve room for the deep-discovery lanes; otherwise a broad alias list
    # could crowd out evaluation, limitations and frontier entry points.
    alias_limit = max(2, cap - 6) if depth == "deep" else cap
    variants = [
        {
            "id": "primary",
            "query": raw,
            "kind": "原始问题",
            "reason": "保留你的原始检索表达",
            "lane": "primary",
        }
    ]
    seen = {raw.casefold()}

    def add(candidate: str, kind: str, reason: str, lane: str):
        candidate = _services().clean_text(candidate, 1000)
        key = candidate.casefold()
        if not candidate or key in seen or len(variants) >= cap:
            return False
        seen.add(key)
        variants.append(
            {
                "id": f"variant-{len(variants)}",
                "query": candidate,
                "kind": kind,
                "reason": reason,
                "lane": lane,
            }
        )
        return True

    lowered = raw.casefold()
    # Build coherent field queries before one-term substitutions.  A literal
    # question such as “用深度学习模型卷积注意力等机制进行食物体积的估计”
    # must not be sent to scholarly APIs as one long Chinese sentence: most
    # indexes then match only “attention” or “convolution” and return unrelated
    # detection papers.  The facet frame keeps the object + task together and
    # treats methods as constraints rather than the main topic.
    facets = _services().extract_search_facets(raw)
    object_terms = list((facets.get("对象") or {}).get("expansions") or [])
    task_terms = list((facets.get("任务") or {}).get("expansions") or [])
    method_terms = list((facets.get("方法") or {}).get("expansions") or [])
    if task_terms:
        task = task_terms[0]
        method = method_terms[0] if method_terms else ""
        obj = object_terms[0] if object_terms else ""
        # The task bridge may already contain the object (food volume, portion
        # size), so avoid a noisy duplicate such as “food food volume …”.
        if obj and obj.casefold() not in task.casefold():
            task_with_object = f"{obj} {task}"
        else:
            task_with_object = task
        add(
            " ".join(part for part in (task_with_object, method) if part),
            "语义任务骨架",
            "同时保留研究对象、核心任务和方法约束",
            "semantic",
        )
        if obj and method_terms:
            second_task = task if obj.casefold() in task.casefold() else f"{obj} {task}"
            add(
                " ".join(dict.fromkeys([second_task, method_terms[-1]])),
                "对象任务方法",
                "用对象 + 任务 + 另一种方法表达召回同主题论文",
                "semantic",
            )
        # Image-based food estimation is often indexed with dietary-assessment
        # or portion-size wording rather than the exact Chinese translation.
        if obj and any(
            "volume" in term.casefold() or "portion" in term.casefold()
            for term in task_terms
        ):
            add(
                "food portion size estimation computer vision",
                "领域同义表达",
                "覆盖食物份量、图像测量和饮食评估文献的常见写法",
                "semantic",
            )

    bridge = _services()._bridge_query_terms(raw)
    if bridge.casefold() != raw.casefold():
        # A mixed Chinese/English bridge can be useful for debugging, but a
        # long literal sentence is a poor provider query.  Prefer the compact
        # semantic lanes above when they exist.
        if not task_terms or len(bridge.split()) <= 8:
            add(bridge, "英文桥接", "把已识别的中文术语桥接到英文索引表达", "bridge")

    # One-term substitutions are useful for compact keyword searches. For a
    # sentence with a recognized research task they create malformed mixed
    # Chinese/English queries and drown out the coherent object/task lanes.
    if not task_terms:
        for concept, aliases in _services().SEARCH_CONCEPT_ALIASES.items():
            if concept.casefold() not in lowered:
                continue
            for alias in aliases[:2]:
                candidate = _services().re.sub(
                    _services().re.escape(concept),
                    alias,
                    raw,
                    count=1,
                    flags=_services().re.IGNORECASE,
                )
                candidate = _services()._bridge_non_latin_terms(candidate)
                add(
                    candidate,
                    "概念别名",
                    f"用“{alias}”补召回“{concept}”的论文",
                    "alias",
                )
            if len(variants) >= alias_limit:
                break

    if depth != "focused":
        facet_terms = []
        for item in facets.values():
            facet_terms.extend(item["expansions"][:2])
        if facet_terms:
            add(
                " ".join(dict.fromkeys(facet_terms[:4])),
                "概念骨架",
                "按对象、方法、任务和评价词重组检索骨架",
                "facets",
            )
    if depth == "deep":
        base = bridge if bridge.casefold() != raw.casefold() else raw
        add(
            f"{base} survey",
            "综述入口",
            "补充综述与系统性地图，优先建立领域全貌",
            "survey",
        )
        add(f"{base} benchmark", "基准入口", "补充基准、数据集与评测论文", "benchmark")
        add(f"{base} empirical study", "实证入口", "补充实证比较和失败分析", "evidence")
        add(
            f"{base} dataset evaluation",
            "数据集评测入口",
            "补充数据集、评测协议和可比较实验",
            "evaluation",
        )
        add(
            f"{base} limitations failure analysis",
            "局限与失败入口",
            "主动寻找边界条件、失败案例和负结果",
            "limitations",
        )
        add(
            f"{base} state of the art",
            "前沿综述入口",
            "补充领域近期最佳结果与比较表",
            "frontier",
        )
    return variants[:cap]


def candidate_identity(record: dict) -> str:
    """Build a stable per-source identity for merging variant hits."""
    doi = _services().norm_doi(record.get("doi", ""))
    if doi:
        return "doi:" + doi
    arxiv = _services().norm_arxiv(record.get("arxiv_id", ""))
    if arxiv:
        return "arxiv:" + arxiv
    title = _services().title_key(record.get("title", ""))
    year = record.get("year") or ""
    if title:
        return f"title:{title}:{year}"
    external = _services().clean_text(record.get("external_id", ""), 300).casefold()
    return (
        "external:" + external
        if external
        else "record:"
        + _services()
        .hashlib.sha1(
            _services()
            .json.dumps(record, sort_keys=True, ensure_ascii=False)
            .encode("utf-8")
        )
        .hexdigest()
    )


def candidate_identity_variants(record: dict) -> set[str]:
    """Return stable identity aliases to catch DOI/arXiv/title crosswalks."""
    keys = {_services().candidate_identity(record)}
    doi = _services().norm_doi(record.get("doi", ""))
    arxiv = _services().norm_arxiv(record.get("arxiv_id", ""))
    if doi:
        keys.add("doi:" + doi)
    if arxiv:
        keys.add("arxiv:" + arxiv)
    title = _services().title_key(record.get("title", ""))
    year = record.get("year") or ""
    if title:
        keys.add(f"title:{title}:{year}")
    for alias in (
        record.get("_dedup_aliases", [])
        if isinstance(record.get("_dedup_aliases"), list)
        else []
    ):
        alias = _services().clean_text(alias, 700)
        if alias:
            keys.add(alias)
    return keys


def record_discovery_search_memory(
    db: sqlite3.Connection,
    intent_key: str,
    record: dict,
    run_id: str,
    seen_at: str | None = None,
) -> None:
    """Persist one identity observation for durable, intent-scoped dedupe."""
    if not intent_key or not isinstance(record, dict):
        return
    timestamp = seen_at or _services().now_iso()
    title = _services().clean_text(record.get("title", ""), 600)
    for identity_key in _services().candidate_identity_variants(record):
        db.execute(
            """INSERT INTO discovery_search_memory(intent_key,identity_key,title,first_seen_at,last_seen_at,seen_count,last_run_id)
               VALUES(?,?,?,?,?,1,?)
               ON CONFLICT(intent_key,identity_key) DO UPDATE SET
                 title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE discovery_search_memory.title END,
                 last_seen_at=excluded.last_seen_at,seen_count=discovery_search_memory.seen_count+1,last_run_id=excluded.last_run_id""",
            (intent_key, identity_key, title, timestamp, timestamp, run_id),
        )


def discovery_intent_key(query: str, filters: dict | None = None) -> str:
    """Return a stable scope key for cross-run candidate feedback.

    Presentation controls (sort order, diversity, model screening and source
    parallelism) deliberately do not affect this key.  Only the research
    question and explicit scope boundaries do, so a user can revisit a search
    without relearning that the same paper is irrelevant.
    """
    normalized = _services().normalize_search_options(
        filters if isinstance(filters, dict) else {}
    )
    boundaries = {}
    for key in ("author", "venue", "work_type", "year_from", "year_to"):
        value = normalized.get(key)
        if value not in (None, ""):
            boundaries[key] = value
    excluded = normalized.get("exclude_terms", "")
    if excluded:
        boundaries["exclude_terms"] = sorted(
            {
                _services().clean_text(item, 120).casefold()
                for item in _services().re.split(r"[,;\n]+", excluded)
                if _services().clean_text(item, 120)
            }
        )
    payload = {
        "query": " ".join(_services().clean_text(query, 1000).casefold().split()),
        "boundaries": boundaries,
    }
    digest = (
        _services()
        .hashlib.sha256(
            _services()
            .json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            .encode("utf-8")
        )
        .hexdigest()
    )
    return "intent:" + digest[:32]


def _json_object(value, default: dict | None = None) -> dict:
    """Parse an untrusted JSON object without leaking malformed snapshots."""
    fallback = dict(default or {})
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = _services().json.loads(value or "{}")
    except (TypeError, ValueError, _services().json.JSONDecodeError):
        return fallback
    return parsed if isinstance(parsed, dict) else fallback


def _discovery_feedback_by_key(
    db: sqlite3.Connection, intent_key: str
) -> dict[str, dict]:
    if not intent_key:
        return {}
    rows = db.execute(
        "SELECT candidate_key,decision,note,candidate_title,created_at,updated_at "
        "FROM discovery_candidate_feedback WHERE intent_key=?",
        (intent_key,),
    ).fetchall()
    return {
        row["candidate_key"]: {
            "decision": row["decision"],
            "note": row["note"],
            "candidate_title": row["candidate_title"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    }


def _discovery_feedback_examples(
    db: sqlite3.Connection, intent_key: str, limit: int = 10
) -> list[dict]:
    """Return a small, title-only feedback sample for the model prompt."""
    safe_limit = min(max(int(limit), 1), 20)
    rows = db.execute(
        "SELECT candidate_key,decision,candidate_title,updated_at "
        "FROM discovery_candidate_feedback WHERE intent_key=? AND decision<>'new' "
        "ORDER BY updated_at DESC LIMIT ?",
        (intent_key, safe_limit),
    ).fetchall()
    return [
        {
            "decision": row["decision"],
            "title": _services().clean_text(row["candidate_title"], 500),
        }
        for row in rows
    ]


def title_similarity(left: str, right: str) -> float:
    """Compare normalized titles without treating different editions as equal."""

    def terms(value: str) -> set[str]:
        normalized = _services().title_key(value)
        found = set(normalized.split())
        for run in _services().re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
            found.update(
                run[index : index + 2] for index in range(max(0, len(run) - 1))
            )
        return found

    left_terms = terms(left)
    right_terms = terms(right)
    if not left_terms or not right_terms:
        return 0.0
    return len(left_terms & right_terms) / max(1, len(left_terms | right_terms))


def merge_search_records(records: list[dict]) -> tuple[list[dict], int]:
    """Merge exact and near-identical hits without returning provider aliases.

    Search providers often expose one work as a journal DOI, an arXiv
    preprint, and a web result.  Strong identifiers are still kept as fields
    on the merged candidate, but a near-identical cross-type DOI/arXiv hit is
    shown once.  Clearly different identifiers of the same type remain
    separate because they may be genuinely different editions or corrections.
    """
    merged = {}
    duplicate_count = 0
    for record in records:
        key = _services().candidate_identity(record)
        previous = merged.get(key)
        if previous is None:
            merged[key] = record
            continue
        duplicate_count += 1
        # Prefer the record carrying more evidence, while retaining the
        # original source/query path on the selected item.
        previous_quality = (
            bool(previous.get("doi") or previous.get("arxiv_id")),
            len(previous.get("abstract", "")),
            len(previous.get("authors", "")),
        )
        current_quality = (
            bool(record.get("doi") or record.get("arxiv_id")),
            len(record.get("abstract", "")),
            len(record.get("authors", "")),
        )
        if current_quality > previous_quality:
            merged[key] = {**previous, **record}
    # A second pass catches title punctuation and provider-specific subtitle
    # differences.  The candidate set is bounded by connector page limits.
    keys = list(merged)
    for index, left_key in enumerate(keys):
        left = merged.get(left_key)
        if not left or not left.get("title"):
            continue
        for right_key in keys[index + 1 :]:
            right = merged.get(right_key)
            if not right or not right.get("title"):
                continue
            left_year, right_year = left.get("year"), right.get("year")
            try:
                years_conflict = (
                    bool(left_year and right_year)
                    and abs(int(left_year) - int(right_year)) > 1
                )
            except (TypeError, ValueError):
                years_conflict = False
            left_strong = bool(left.get("doi") or left.get("arxiv_id"))
            right_strong = bool(right.get("doi") or right.get("arxiv_id"))
            similarity = _services().title_similarity(left["title"], right["title"])
            # A very close title is enough when one provider omitted the year;
            # this is common for Tavily HTML pages.  Lower-similarity matches
            # still require a consistent year to avoid accidental merges.
            if (
                years_conflict
                or (not left_year or not right_year)
                and similarity < 0.97
            ):
                continue
            if similarity < 0.92:
                continue
            left_doi, right_doi = _services().norm_doi(
                left.get("doi", "")
            ), _services().norm_doi(right.get("doi", ""))
            left_arxiv, right_arxiv = _services().norm_arxiv(
                left.get("arxiv_id", "")
            ), _services().norm_arxiv(right.get("arxiv_id", ""))
            different_same_type = bool(
                (left_doi and right_doi and left_doi != right_doi)
                or (left_arxiv and right_arxiv and left_arxiv != right_arxiv)
            )
            cross_type_versions = bool(
                (left_doi and right_arxiv) or (right_doi and left_arxiv)
            )
            # Different DOI-only or arXiv-only identifiers are retained.  A
            # DOI + arXiv crosswalk with an almost identical title is the
            # common preprint/journal representation of one work, so merge it.
            if different_same_type and not cross_type_versions:
                continue
            if (
                left_strong
                and right_strong
                and not cross_type_versions
                and similarity < 0.985
            ):
                continue
            previous_quality = (
                left_strong,
                len(left.get("abstract", "")),
                len(left.get("authors", "")),
            )
            current_quality = (
                right_strong,
                len(right.get("abstract", "")),
                len(right.get("authors", "")),
            )
            winner, loser = (
                (right, left) if current_quality > previous_quality else (left, right)
            )
            combined = dict(loser)
            # Do not let an empty field from the better-scored provider erase
            # useful metadata from the other provider.
            for field, value in winner.items():
                if value not in (None, "", [], {}):
                    combined[field] = value
            for field, value in loser.items():
                if combined.get(field) in (None, "", [], {}) and value not in (
                    None,
                    "",
                    [],
                    {},
                ):
                    combined[field] = value
            combined["doi"] = _services().norm_doi(combined.get("doi", ""))
            combined["arxiv_id"] = _services().norm_arxiv(combined.get("arxiv_id", ""))
            combined["_dedup_aliases"] = sorted(
                set(
                    (left.get("_dedup_aliases") or [])
                    + (right.get("_dedup_aliases") or [])
                    + [left_key, right_key]
                )
            )
            for list_field in ("provider_sources", "source_citations", "search_paths"):
                values = []
                for item in (left.get(list_field) or []) + (
                    right.get(list_field) or []
                ):
                    if item not in values:
                        values.append(item)
                if values:
                    combined[list_field] = values
            merged[left_key] = combined
            merged.pop(right_key, None)
            duplicate_count += 1
            left = merged[left_key]
    return list(merged.values()), duplicate_count


def relevance_label(score: int) -> str:
    if score < 10:
        return "待核验"
    if score >= 60:
        return "高相关"
    if score >= 35:
        return "邻近方向"
    return "低相关"


def relevance_token_weight(term: str) -> float:
    """Down-weight short Chinese n-grams while retaining their recall value."""
    if _services().re.fullmatch(r"[\u4e00-\u9fff]+", term or ""):
        if len(term) == 2:
            return 0.35
        if len(term) == 3:
            return 0.60
    return 1.0


def score_relevance(
    candidate: dict,
    query: str,
    known: list[sqlite3.Row],
    feedback: list[sqlite3.Row] | None = None,
) -> dict:
    """Return an explainable triage score against a query and library memory."""
    query_terms = _services().relevance_tokens(query)
    expanded_terms = _services().expanded_relevance_terms(query)
    bridge_terms = expanded_terms - query_terms
    title_terms = _services().relevance_tokens(candidate.get("title", ""))
    body_terms = _services().relevance_tokens(
        " ".join([candidate.get("abstract", ""), candidate.get("authors", "")])
    )
    candidate_terms = title_terms | body_terms
    title_hits = sorted(expanded_terms & title_terms)
    body_hits = sorted(expanded_terms & body_terms)
    original_title_hits = sorted(query_terms & title_terms)
    bridge_title_hits = sorted(set(title_hits) & bridge_terms)
    score = min(
        55,
        round(
            sum(
                _services().relevance_token_weight(term) for term in original_title_hits
            )
            * 22
            + sum(
                _services().relevance_token_weight(term) for term in bridge_title_hits
            )
            * 14
            + sum(
                _services().relevance_token_weight(term)
                for term in query_terms & body_terms
            )
            * 7
            + sum(
                _services().relevance_token_weight(term)
                for term in set(body_hits) & bridge_terms
            )
            * 5
        ),
    )
    reasons = []
    if original_title_hits:
        reasons.append("标题命中 " + ", ".join(original_title_hits[:5]))
    if bridge_title_hits:
        reasons.append("标题命中桥接概念 " + ", ".join(bridge_title_hits[:5]))
    if body_hits and not title_hits:
        reasons.append("摘要/作者命中 " + ", ".join(body_hits[:5]))
    phrases = [
        _services().clean_text(value, 120).casefold()
        for value in _services().re.findall(r'"([^"\n]+)"', query)
        if _services().clean_text(value, 120)
    ]
    title_lower = _services().clean_text(candidate.get("title", ""), 2000).casefold()
    abstract_lower = (
        _services().clean_text(candidate.get("abstract", ""), 30000).casefold()
    )
    phrase_hits = [
        phrase
        for phrase in phrases
        if phrase in title_lower or phrase in abstract_lower
    ]
    if phrase_hits:
        score += min(22, 11 * len(phrase_hits))
        reasons.append("精确短语命中 " + ", ".join(phrase_hits[:3]))
    if expanded_terms:
        coverage = len(set(title_hits + body_hits)) / max(1, len(expanded_terms))
        if coverage >= 0.4:
            score += 10
            reasons.append(f"查询概念覆盖 {round(coverage * 100)}%")
    # Long natural-language queries usually contain a research object, a task
    # and one or more methods.  Matching a method alone (for example an
    # attention-based car detector) is not enough for a food-volume query.
    # Score facet coverage explicitly and cap method-only candidates so source
    # rank cannot push them above papers that answer the actual task.
    facets = _services().extract_search_facets(query)
    facet_hits = {}
    facet_score_cap = 100
    object_alias_tokens = set()
    object_strong_tokens = set()
    task_strong_tokens = set()
    if facets.get("对象"):
        object_alias_tokens = _services().relevance_tokens(
            " ".join(facets["对象"].get("expansions", [])[:8])
        )
        object_strong_tokens = object_alias_tokens - {
            "image",
            "visual",
            "recognition",
            "computer",
            "vision",
        }
    if facets.get("任务"):
        task_strong_tokens = _services().relevance_tokens(
            " ".join(facets["任务"].get("expansions", [])[:8])
        ) & {"volume", "portion", "measurement", "quantification", "size"}
    for facet_name, facet in facets.items():
        aliases = set(
            _services().relevance_tokens(" ".join(facet.get("expansions", [])[:8]))
        )
        # Phrases such as “food volume estimation” contain the object word
        # food.  Do not let that object token falsely count as a task hit.
        if facet_name == "任务" and object_alias_tokens:
            aliases -= object_alias_tokens
        hits = aliases & candidate_terms
        if facet_name == "对象":
            hits &= object_strong_tokens
        if hits:
            facet_hits[facet_name] = sorted(hits)
            score += 6 if facet_name in {"对象", "任务"} else 3
            reasons.append(f"命中{facet_name}：" + ", ".join(sorted(hits)[:4]))
    has_object = bool(facets.get("对象"))
    has_task = bool(facets.get("任务"))
    object_hit = "对象" in facet_hits
    task_hit = "任务" in facet_hits
    title_facet_terms = _services().relevance_tokens(candidate.get("title", ""))
    title_object_hit = bool(object_strong_tokens & title_facet_terms)
    title_task_hit = bool(task_strong_tokens & title_facet_terms)
    body_object_hit = bool(object_strong_tokens & body_terms)
    body_task_hit = bool(task_strong_tokens & body_terms)
    if has_object and has_task:
        if object_hit and task_hit:
            score += 14
            reasons.append("对象与核心任务同时命中")
            if not title_object_hit and not title_task_hit:
                # Both concepts appearing only in a long abstract is weaker
                # evidence than a title-level match; this prevents incidental
                # mentions from outranking papers whose title answers the task.
                facet_score_cap = min(facet_score_cap, 34)
                reasons.append("对象与任务仅在摘要中出现，降低优先级")
            elif not title_object_hit or not title_task_hit:
                facet_score_cap = min(facet_score_cap, 58)
        elif not object_hit and not task_hit:
            facet_score_cap = 12
            reasons.append("未命中研究对象或核心任务，降为低相关")
        else:
            facet_score_cap = 27
            reasons.append("只命中对象或任务之一，需人工核验")
    elif has_task and not task_hit:
        facet_score_cap = 24
        reasons.append("未命中核心任务词，避免仅凭方法词推荐")
    if has_object and object_hit and not object_strong_tokens & candidate_terms:
        facet_score_cap = min(facet_score_cap, 24)
    if bridge_title_hits or set(body_hits) & bridge_terms:
        reasons.append("已用中英文概念桥接校正相关度")
    best_overlap = 0
    best_title = ""
    for row in known:
        other_terms = _services().relevance_tokens(
            " ".join([row["title"] or "", row["abstract"] or ""])
        )
        if not other_terms or not (title_terms or body_terms):
            continue
        overlap = len((title_terms | body_terms) & other_terms) / max(
            1, len(title_terms | body_terms)
        )
        if overlap > best_overlap:
            best_overlap, best_title = overlap, row["title"]
    if best_overlap >= 0.35:
        score += min(35, round(best_overlap * 55))
        reasons.append(
            "与库内论文有主题重合" + (f"：{best_title[:42]}" if best_title else "")
        )
    elif best_overlap >= 0.18:
        score += 12
        reasons.append("与已有主题存在部分重合")
    for row in feedback or []:
        other_terms = _services().relevance_tokens(
            " ".join([row["title"] or "", row["abstract"] or ""])
        )
        if not other_terms or not (title_terms or body_terms):
            continue
        overlap = len((title_terms | body_terms) & other_terms) / max(
            1, len(title_terms | body_terms)
        )
        if overlap >= 0.28 and row["user_decision"] == "relevant":
            score += 12
            reasons.append("根据你的主题反馈加权")
            break
        if overlap >= 0.28 and row["user_decision"] in {"irrelevant", "ignored"}:
            score -= 18
            reasons.append("根据你此前的忽略反馈降权")
            break
    if candidate.get("doi") or candidate.get("arxiv_id"):
        score += 5
    if candidate.get("open_access"):
        score += 2
    score = min(score, facet_score_cap)
    score = max(0, min(100, score))
    return {
        "score": score,
        "label": _services().relevance_label(score),
        "reasons": reasons or ["未发现明确词项重合，建议人工快速扫读"],
    }


def _model_web_has_object_task_evidence(paper: dict, facets: dict) -> bool:
    """Require a concrete object/task signal before exposing a model-web hit.

    Model-led search is intentionally broad, but lexical scoring can mistake
    phrases such as ``food security`` for a food-object paper.  Use the title
    and abstract as a cheap local guard before a candidate reaches the review
    queue; this does not replace the model verdict, it prevents obvious
    cross-domain false positives from being promoted as "adjacent" work.
    """
    text = (
        _services()
        .clean_text(f"{paper.get('title', '')} {paper.get('abstract', '')}", 12000)
        .casefold()
    )
    object_terms = [
        _services().clean_text(value, 120).casefold()
        for value in ((facets.get("对象") or {}).get("expansions") or [])
        if _services().clean_text(value, 120)
    ]
    task_terms = [
        _services().clean_text(value, 120).casefold()
        for value in ((facets.get("任务") or {}).get("expansions") or [])
        if _services().clean_text(value, 120)
    ]
    if not object_terms or not task_terms:
        return True
    if any(term in {"food", "meal", "dish"} for term in object_terms):
        # ``food security``, ``food crop`` and ``food yield`` are common
        # distractors for food-volume/portion questions. Require a phrase
        # that denotes an actual edible object, scene, or dietary measurement.
        object_hit = any(
            pattern in text
            for pattern in (
                "food volume",
                "food portion",
                "food image",
                "food item",
                "food intake",
                "food segmentation",
                "food recognition",
                "food mesh",
                "food reconstruction",
                "meal",
                "dish",
                "dietary",
                "nutrition",
                "plate",
                "edible",
                "eating",
            )
        )
    else:
        object_hit = any(term in text for term in object_terms)
    # Prefer the requested task terms, then fall back to a small set of common
    # measurement verbs so a phrase like "food volume" is still recognized.
    task_hit = any(term in text for term in task_terms if term not in object_terms)
    if not task_hit:
        task_hit = bool(
            _services().re.search(
                r"\b(?:volume|portion|measurement|measure|estimation|estimate|quantification|classification|recognition|segmentation|detection|prediction|reconstruction)\b",
                text,
            )
        )
    return object_hit and task_hit


def filter_model_web_quality(
    results: list[dict], query: str, options: dict
) -> tuple[list[dict], int]:
    """Keep model-web candidates that have evidence for the asked object/task.

    A model may deliberately broaden a later search to avoid repeats. Without a
    guard, shared method words (for example ``attention`` and ``estimation``)
    can turn an unrelated paper into a visible candidate. For a structured
    natural-language question, an empty result with an explanation is safer
    than filling the review queue with false positives. Exploratory mode keeps
    the old broad behavior by design.
    """
    if (
        not results
        or _services().clean_text(options.get("search_backend", ""), 40).casefold()
        != "model_web"
    ):
        return results, 0
    if (
        _services().clean_text(options.get("discovery_mode", "balanced"), 40).casefold()
        == "explore"
    ):
        return results, 0
    facets = _services().extract_search_facets(query)
    if not facets.get("对象") or not facets.get("任务"):
        return results, 0
    kept = []
    filtered = 0
    for paper in results:
        try:
            score = int(paper.get("relevance_score") or 0)
        except (TypeError, ValueError):
            score = 0
        model_label = (
            _services().clean_text(paper.get("model_relevance_label"), 40).casefold()
        )
        reasons = (
            paper.get("relevance_reasons")
            if isinstance(paper.get("relevance_reasons"), list)
            else []
        )
        if paper.get("relevance_reasons") is not reasons:
            paper["relevance_reasons"] = reasons
        has_facet_evidence = any(
            text.startswith(("命中对象：", "命中任务：", "对象与核心任务同时命中"))
            for text in (_services().clean_text(item, 300) for item in reasons)
        )
        has_object_task_evidence = _services()._model_web_has_object_task_evidence(
            paper, facets
        )
        selection_backfilled = bool(
            paper.get("selection_backfilled") or paper.get("_selection_backfilled")
        )
        # A candidate restored from the durable queue belongs to this exact
        # research intent.  It was already visible in an earlier run, which
        # means it passed that run's model-web quality gate.  Re-running the
        # current lexical guard here used to throw away the very history rows
        # that were added to prevent a repeated search from shrinking to a
        # handful of papers (for example when an older abstract used a
        # synonym that the current facet extractor does not know).  Trust the
        # durable acceptance marker, while keeping legacy rows conservative:
        # rows without the marker still need the normal evidence check.
        history_quality_accepted = bool(paper.get("history_quality_accepted"))
        # A direct model verdict can rescue a synonym-heavy title, but still
        # needs a minimal lexical signal. Adjacent results must clear the
        # normal threshold and expose at least one requested facet.
        accepted = (
            history_quality_accepted
            or (model_label == "direct" and score >= 25 and has_object_task_evidence)
            or (score >= 35 and has_facet_evidence and has_object_task_evidence)
            # A bounded fill is intentionally conservative, but it should not
            # disappear just because the model's final JSON ranked fewer rows
            # than requested.  It already passed the object/task evidence
            # check in _model_search_backfill_selection; retain it as an
            # explicit uncertain candidate for human review.
            or (selection_backfilled and score >= 12 and has_object_task_evidence)
        )
        if accepted:
            if history_quality_accepted:
                reasons = paper.setdefault("relevance_reasons", [])
                message = "同一研究主题的历史候选此前已通过相关性筛选，本轮作为历史补位保留；请人工确认是否需要再次阅读"
                if message not in reasons:
                    reasons.append(message)
            if selection_backfilled and model_label not in {"direct", "adjacent"}:
                paper["model_relevance_label"] = "uncertain"
                paper["relevance_label"] = "邻近方向" if score >= 35 else "跨领域线索"
                reasons = paper.setdefault("relevance_reasons", [])
                message = (
                    "AI 主筛选结果不足请求数量，已从真实工具候选补位；建议人工核验"
                )
                if message not in reasons:
                    reasons.append(message)
            # Persist the decision on every accepted model-web candidate so a
            # later repeat can reuse it without re-running a stricter or
            # differently configured lexical gate.
            paper["history_quality_accepted"] = True
            kept.append(paper)
        else:
            filtered += 1
    return kept, filtered


def filter_agent_candidate_quality(
    results: list[dict], query: str
) -> tuple[list[dict], dict]:
    """Apply the same object/task guard to Agent connector searches.

    Agent searches use the normal academic connectors so they can keep working
    when model-web search is unavailable.  That path used to bypass the
    model-web quality gate, which allowed method-only hits (for example a
    medical or finance paper containing ``attention`` and ``estimation``) into
    the main candidate set.  This local, deterministic pass is intentionally
    conservative: it removes only candidates with no evidence for either the
    requested object and task, while retaining adjacent papers that expose at
    least one requested facet for human review.

    The returned summary is persisted by the Agent step and gives the UI an
    explanation even when every candidate is filtered out.
    """
    items = [item for item in (results or []) if isinstance(item, dict)]
    facets = _services().extract_search_facets(query)
    summary = {
        "input_count": len(items),
        "kept_count": len(items),
        "filtered_count": 0,
        "direct": 0,
        "adjacent": 0,
        "uncertain": 0,
        "unrelated": 0,
        "facets": {key: value.get("hits", []) for key, value in facets.items()},
        "filtered_reasons": [],
    }
    # A short keyword query may not contain separable object/task facets.  In
    # that case the connector's normal ranking is the safer recall-preserving
    # behavior; the summary still tells the user why no extra gate ran.
    if not facets.get("对象") or not facets.get("任务"):
        summary["mode"] = "connector_ranking_only"
        return items, summary

    kept = []
    for item in items:
        score = item.get("relevance_score")
        try:
            score = (
                float(score)
                if score is not None
                else float(_services().score_relevance(item, query, [], [])["score"])
            )
        except (TypeError, ValueError):
            score = float(_services().score_relevance(item, query, [], [])["score"])
        evidence = _services()._model_web_has_object_task_evidence(item, facets)
        title_body = (
            _services()
            .clean_text(f"{item.get('title', '')} {item.get('abstract', '')}", 12000)
            .casefold()
        )
        object_terms = [
            _services().clean_text(value, 120).casefold()
            for value in [
                *((facets.get("对象") or {}).get("hits") or []),
                *((facets.get("对象") or {}).get("expansions") or []),
            ]
            if _services().clean_text(value, 120)
        ]
        task_terms = [
            _services().clean_text(value, 120).casefold()
            for value in [
                *((facets.get("任务") or {}).get("hits") or []),
                *((facets.get("任务") or {}).get("expansions") or []),
            ]
            if _services().clean_text(value, 120)
        ]
        object_hit = any(term in title_body for term in object_terms)
        task_hit = any(term in title_body for term in task_terms)
        if evidence and score >= 60:
            relation = "direct"
        elif evidence and (object_hit or task_hit) and score >= 24:
            relation = "adjacent"
        elif evidence and score >= 18:
            relation = "uncertain"
        else:
            relation = "unrelated"
        item["agent_relevance_class"] = relation
        item["agent_relevance_score"] = round(score, 2)
        if relation == "unrelated":
            summary["filtered_count"] += 1
            summary["unrelated"] += 1
            reason = "标题和摘要没有同时提供研究对象与核心任务证据"
            item["quality_filter_reason"] = reason
            if len(summary["filtered_reasons"]) < 8:
                summary["filtered_reasons"].append(
                    {
                        "title": _services().clean_text(item.get("title"), 240),
                        "reason": reason,
                    }
                )
            continue
        summary[relation] += 1
        existing_reasons = item.get("relevance_reasons")
        item["relevance_reasons"] = (
            list(existing_reasons)
            if isinstance(existing_reasons, list)
            else (
                [_services().clean_text(existing_reasons, 500)]
                if existing_reasons
                else []
            )
        )
        item["relevance_reasons"].append(
            {
                "direct": "Agent 质量闸门：研究对象与核心任务均有证据",
                "adjacent": "Agent 质量闸门：至少命中一个研究维度，保留作邻近线索",
                "uncertain": "Agent 质量闸门：有弱证据，保留并标记为待核验",
            }[relation]
        )
        kept.append(item)
    summary["kept_count"] = len(kept)
    summary["mode"] = "object_task_quality_gate"
    summary["gaps"] = []
    if not kept and items:
        summary["gaps"].append("本轮候选均未同时满足研究对象与核心任务证据")
        summary["gaps"].append(
            "可改写研究问题、放宽任务词，或切换到探索模式查看跨领域线索"
        )
    elif len(kept) < 5:
        summary["gaps"].append("相关候选数量较少，建议用同义词或限制条件补搜")
    return kept, summary


def clean_text(value, max_len=30000) -> str:
    return str(value or "").strip()[:max_len]


def compact_search_raw(record: object) -> dict:
    """Keep source provenance bounded and prevent recursive candidate payloads.

    Candidate rows are reused as history on later searches.  Storing the whole
    candidate as ``raw`` meant that a history refill could store ``raw.raw``
    repeatedly, eventually making every result response much larger than the
    actual paper metadata.  ``raw`` is provenance for import/audit only, so a
    small allow-list is enough and deliberately excludes nested paper objects.
    """
    if not isinstance(record, dict):
        return {}
    compact = {}
    for field in _services()._SEARCH_RAW_FIELDS:
        value = record.get(field)
        if value in (None, "", [], {}):
            continue
        if field in {
            "provider_sources",
            "source_citations",
            "verification_sources",
            "search_paths",
        }:
            if isinstance(value, list):
                safe_items = []
                for item in value[:24]:
                    if isinstance(item, dict):
                        safe_items.append(
                            {
                                _services()
                                .clean_text(key, 80): _services()
                                .clean_text(val, 1000)
                                for key, val in item.items()
                                if key not in {"raw", "record", "paper"}
                            }
                        )
                    elif isinstance(item, (str, int, float, bool)):
                        safe_items.append(_services().clean_text(item, 1000))
                if safe_items:
                    compact[field] = safe_items
            continue
        if isinstance(value, (str, int, float, bool)):
            compact[field] = (
                _services().clean_text(value, 30000)
                if isinstance(value, str)
                else value
            )
    return compact


def compact_candidate_record(record: object) -> dict:
    """Return a shallow candidate copy with a non-recursive provenance raw."""
    if not isinstance(record, dict):
        return {}
    item = dict(record)
    item["raw"] = _services().compact_search_raw(
        record.get("raw") if isinstance(record.get("raw"), dict) else record
    )
    return item


def normalize_record(record: dict) -> dict:
    doi = _services().norm_doi(record.get("doi", ""))
    arxiv = _services().norm_arxiv(record.get("arxiv_id", ""))
    title = _services().clean_text(record.get("title"), 1000)
    try:
        year = int(record.get("year")) if record.get("year") else None
        if year and not 1800 <= year <= 2200:
            year = None
    except (ValueError, TypeError):
        year = None
    tags = record.get("tags", [])
    if isinstance(tags, str):
        tags = [x.strip() for x in tags.split(",") if x.strip()]
    return {
        "title": title,
        "title_key": _services().title_key(title),
        "authors": _services().clean_text(record.get("authors", ""), 4000),
        "year": year,
        "abstract": _services().clean_text(record.get("abstract", ""), 30000),
        "doi": doi,
        "arxiv_id": arxiv,
        "venue": _services().clean_text(record.get("venue", ""), 1000),
        "url": _services().clean_text(record.get("url", ""), 3000),
        "pdf_url": _services().clean_text(record.get("pdf_url", ""), 3000),
        "status": (
            record.get("status")
            if record.get("status") in _services().PAPER_STATUSES
            else "inbox"
        ),
        "topic": _services().clean_text(record.get("topic", ""), 300),
        "tags_json": _services().json.dumps(
            list(dict.fromkeys(tags))[:100], ensure_ascii=False
        ),
        "notes": _services().clean_text(record.get("notes", ""), 100000),
        "is_favorite": 1 if record.get("is_favorite") else 0,
    }


def paper_pdf_candidates(paper: sqlite3.Row | dict) -> list[str]:
    """Build conservative direct-PDF candidates from stored paper metadata."""
    get = (
        paper.get
        if isinstance(paper, dict)
        else lambda key, default="": paper[key] if key in paper.keys() else default
    )
    candidates = []
    for value in (get("pdf_url", ""),):
        value = _services().clean_text(value, 3000).strip()
        if value and value not in candidates:
            candidates.append(value)
    arxiv_id = _services().norm_arxiv(get("arxiv_id", ""))
    if arxiv_id:
        candidates.append("https://arxiv.org/pdf/" + _services().quote(arxiv_id))
    doi = _services().norm_doi(get("doi", ""))
    if doi:
        candidates.append("https://doi.org/" + _services().quote(doi, safe="/"))
        if doi.lower().startswith("10.64898/"):
            candidates.append(
                "https://www.biorxiv.org/content/"
                + _services().quote(doi, safe="/")
                + ".full.pdf"
            )
    landing = _services().clean_text(get("url", ""), 3000).strip()
    parsed = _services().urlparse(landing)
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")
    if path.lower().endswith(".pdf"):
        candidates.append(landing)
    if host.endswith("biorxiv.org") or host.endswith("medrxiv.org"):
        if path.startswith("/content/"):
            candidates.append(f"{parsed.scheme}://{parsed.netloc}{path}.full.pdf")
    return list(dict.fromkeys(item for item in candidates if item))


def download_public_pdf(paper_id: str, url: str = "") -> dict:
    """Download one validated public PDF into the managed attachment store.

    This shared implementation is used by the normal paper action and the
    Agent's explicit download action, so both paths apply the same HTTPS,
    size, content and deduplication rules. A candidate is never imported by
    this function; it must already be a paper in the local library.
    """
    with _services().DB_LOCK, _services().db_session() as db:
        paper = db.execute(
            "SELECT id,title,pdf_url,url,arxiv_id,doi FROM papers WHERE id=?",
            (paper_id,),
        ).fetchone()
    if not paper:
        raise LookupError("论文不存在或尚未导入文献库")
    candidates = (
        [_services().clean_text(url, 3000).strip()]
        if url
        else _services().paper_pdf_candidates(paper)
    )
    candidates = [candidate for candidate in candidates if candidate]
    if not candidates:
        raise ValueError("这篇论文没有可用的公开 PDF 地址")
    chunks, total, source_url, source_name, failures = [], 0, "", "", []
    for candidate in candidates:
        parsed = _services().urlparse(candidate)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
        ):
            failures.append("地址格式无效")
            continue
        if parsed.scheme == "http" and parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            failures.append("非本机地址必须使用 HTTPS")
            continue
        request = _services().Request(
            candidate,
            headers={
                "Accept": "application/pdf,application/octet-stream;q=0.8,*/*;q=0.1",
                "User-Agent": _services().USER_AGENT,
            },
        )
        try:
            resolved_url = candidate
            with _services().urlopen(request, timeout=60) as response:
                resolved_url = (
                    response.geturl() if hasattr(response, "geturl") else candidate
                )
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > 100 * 1024 * 1024:
                        raise ValueError("远程 PDF 超过 100 MB 限制")
                    chunks.append(chunk)
            content = b"".join(chunks)
            if not content.startswith(b"%PDF"):
                failures.append("返回内容不是 PDF")
                resolved = _services().urlparse(resolved_url)
                resolved_path = resolved.path.rstrip("/")
                resolved_host = (resolved.hostname or "").lower()
                if (
                    resolved_host.endswith("biorxiv.org")
                    or resolved_host.endswith("medrxiv.org")
                ) and resolved_path.startswith("/content/"):
                    derived = (
                        f"{resolved.scheme}://{resolved.netloc}{resolved_path}.full.pdf"
                    )
                    if derived not in candidates:
                        candidates.append(derived)
                chunks, total = [], 0
                continue
            source_url, source_name = candidate, (
                _services().Path(_services().unquote(parsed.path).rstrip("/")).name
                or "paper.pdf"
            )
            break
        except _services().HTTPError as exc:
            failures.append(f"HTTP {exc.code}")
        except (_services().URLError, TimeoutError, OSError, ValueError) as exc:
            failures.append(str(exc)[:120])
        chunks, total = [], 0
    if not source_url:
        raise ValueError("PDF 下载失败：" + "；".join(failures[:3]))
    content = b"".join(chunks)
    filename = source_name or (_services().clean_text(paper["title"], 180) + ".pdf")
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"
    digest = _services().hashlib.sha256(content).hexdigest()
    _services().active_files_dir().mkdir(parents=True, exist_ok=True)
    stored = digest + ".pdf"
    target = _services().active_files_dir() / stored
    if not target.exists():
        target.write_bytes(content)
    with _services().DB_LOCK, _services().db_session() as db:
        existing = db.execute(
            "SELECT id,original_name,stored_name,sha256,byte_size FROM attachments WHERE paper_id=? AND sha256=? ORDER BY added_at LIMIT 1",
            (paper_id, digest),
        ).fetchone()
        if existing:
            result = {
                "id": existing["id"],
                "filename": existing["original_name"],
                "sha256": existing["sha256"],
                "url": "/files/" + existing["stored_name"],
                "deduplicated": True,
            }
        else:
            aid = str(_services().uuid.uuid4())
            db.execute(
                "INSERT INTO attachments(id,paper_id,original_name,stored_name,sha256,byte_size,added_at) VALUES(?,?,?,?,?,?,?)",
                (
                    aid,
                    paper_id,
                    filename,
                    stored,
                    digest,
                    len(content),
                    _services().now_iso(),
                ),
            )
            _services().record_audit(
                db,
                "attachment.downloaded",
                "paper",
                paper_id,
                {"sha256": digest, "byte_size": len(content), "source_url": source_url},
            )
            result = {
                "id": aid,
                "filename": filename,
                "sha256": digest,
                "url": "/files/" + stored,
                "deduplicated": False,
            }
    result.update({"source_url": source_url, "downloaded": not result["deduplicated"]})
    return result


def paper_text(record: dict | sqlite3.Row) -> str:
    """The stable, local text representation used by explainable ranking."""
    getter = (
        record.get
        if hasattr(record, "get")
        else lambda key, default="": record[key] if key in record.keys() else default
    )
    tags = getter("tags", []) if isinstance(record, dict) else []
    if isinstance(tags, str):
        tags = [tags]
    return (
        " ".join(
            str(getter(key, "") or "")
            for key in ("title", "abstract", "authors", "topic")
        )
        + " "
        + " ".join(tags)
    )


def text_overlap(left: str, right: str) -> dict:
    """Return a symmetric lexical similarity signal with human-readable hits."""
    left_terms = _services().expanded_relevance_terms(left)
    right_terms = _services().expanded_relevance_terms(right)
    if not left_terms or not right_terms:
        return {"score": 0, "hits": []}
    hits = sorted(left_terms & right_terms)
    # Combine coverage of the shorter concept set with a Jaccard penalty for
    # broad abstracts. This keeps a two-term research query meaningful while
    # preventing one shared generic term from looking like a close match.
    coverage = len(hits) / max(1, min(len(left_terms), len(right_terms)))
    jaccard = len(hits) / max(1, len(left_terms | right_terms))
    score = min(100, round((coverage * 0.7 + jaccard * 0.3) * 100))
    return {"score": score, "hits": hits[:8]}


def connection_tier(score: int) -> str:
    if score >= 48:
        return "高相关"
    if score >= 20:
        return "邻近方向"
    return "跨领域线索"


def _openalex_work_id(value: str) -> str:
    value = _services().clean_text(value or "", 300).rstrip("/")
    match = _services().re.search(r"(?:^|/)(W\d+)$", value, _services().re.I)
    return match.group(1).upper() if match else ""


def _openalex_seed_ids(paper_ids: list[str]) -> list[str]:
    paper_ids = list(
        dict.fromkeys(_services().clean_text(value, 80) for value in paper_ids if value)
    )[:20]
    if not paper_ids:
        return []
    placeholders = ",".join("?" for _ in paper_ids)
    with _services().DB_LOCK, _services().db_session() as db:
        rows = db.execute(
            f"SELECT external_id FROM source_records WHERE source='openalex' AND paper_id IN ({placeholders}) ORDER BY fetched_at DESC",
            paper_ids,
        ).fetchall()
    return list(
        dict.fromkeys(
            work_id
            for row in rows
            if (work_id := _services()._openalex_work_id(row["external_id"]))
        )
    )[:3]


def _semantic_scholar_seed_ids(paper_ids: list[str]) -> list[str]:
    """Resolve stable Semantic Scholar seed identifiers for recommendations.

    Semantic Scholar accepts its own paper ID and common scholarly identifiers
    such as DOI and arXiv ID. Prefer the provider ID already attached to a
    paper, then fall back to DOI/arXiv so imported papers can still use the
    provider's recommendation model without a local reimplementation.
    """
    paper_ids = list(
        dict.fromkeys(_services().clean_text(value, 80) for value in paper_ids if value)
    )[:20]
    if not paper_ids:
        return []
    placeholders = ",".join("?" for _ in paper_ids)
    with _services().DB_LOCK, _services().db_session() as db:
        rows = db.execute(
            f"SELECT p.doi,p.arxiv_id,s.external_id FROM papers p "
            f"LEFT JOIN source_records s ON s.paper_id=p.id AND s.source='semanticscholar' "
            f"WHERE p.id IN ({placeholders}) ORDER BY s.fetched_at DESC",
            paper_ids,
        ).fetchall()
    seeds = []
    for row in rows:
        for value in (row["external_id"], row["doi"], row["arxiv_id"]):
            value = _services().clean_text(value, 300)
            if value and value not in seeds:
                seeds.append(value)
                break
        if len(seeds) >= _services().SEMANTIC_RECOMMENDATION_MAX_SEEDS:
            break
    return seeds


def _semantic_recommendation_record(item: dict, rank: int, seed_count: int = 1) -> dict:
    """Normalize one Semantic Scholar recommendation without inventing data."""
    external_ids = item.get("externalIds") or {}
    doi = _services().norm_doi(external_ids.get("DOI") or item.get("doi") or "")
    arxiv_id = _services().norm_arxiv(
        external_ids.get("ArXiv") or item.get("arxiv_id") or ""
    )
    paper_id = _services().clean_text(item.get("paperId") or item.get("id") or "", 300)
    title = _services().clean_text(item.get("title"), 1000)
    if not title:
        return {}
    authors = ", ".join(
        _services().clean_text(
            author.get("name") if isinstance(author, dict) else author, 240
        )
        for author in (item.get("authors") or [])[:12]
        if _services().clean_text(
            author.get("name") if isinstance(author, dict) else author, 240
        )
    )
    url = _services()._model_search_url(item.get("url"))
    if not url and paper_id:
        url = f"https://www.semanticscholar.org/paper/{_services().quote(paper_id, safe='')}"
    open_pdf = item.get("openAccessPdf") or {}
    score = min(
        100,
        max(40, 100 - (max(1, int(rank)) - 1) * 3)
        + min(10, max(0, seed_count - 1) * 5),
    )
    return {
        "title": title,
        "authors": authors,
        "year": item.get("year"),
        "abstract": _services().clean_text(item.get("abstract"), 30000),
        "doi": doi,
        "arxiv_id": arxiv_id,
        "venue": _services().clean_text(item.get("venue"), 1000),
        "url": url,
        "pdf_url": _services()._model_search_url(open_pdf.get("url")),
        "source": "semanticscholar",
        "external_id": paper_id or doi or arxiv_id or url,
        "open_access": bool(open_pdf.get("url")),
        "citation_count": int(item.get("citationCount") or 0),
        "recommendation_provider": "Semantic Scholar",
        "recommendation_score": score,
        "recommendation_rank": max(1, int(rank)),
        "recommendation_reasons": ["Semantic Scholar 官方推荐模型召回"],
    }


def _load_semantic_scholar_recommendations(seed_ids: list[str]) -> dict:
    """Use Semantic Scholar's hosted recommendation model for reading seeds."""
    seeds = list(
        dict.fromkeys(
            _services().clean_text(value, 300)
            for value in (seed_ids or [])
            if _services().clean_text(value, 300)
        )
    )[: _services().SEMANTIC_RECOMMENDATION_MAX_SEEDS]
    if not seeds:
        return {
            "provider": "Semantic Scholar",
            "by_semantic_id": {},
            "by_doi": {},
            "external_candidates": [],
            "related_count": 0,
            "seed_count": 0,
            "errors": [],
        }

    def fetch(seed: str):
        endpoint = (
            "https://api.semanticscholar.org/recommendations/v1/papers/for/"
            + _services().quote(seed, safe="")
        )
        endpoint += "?" + _services().urlencode(
            {
                "limit": _services().SEMANTIC_RECOMMENDATION_MAX_RESULTS,
                "fields": "paperId,title,abstract,year,authors,venue,externalIds,url,openAccessPdf,citationCount",
            }
        )
        return _services().http_json(endpoint, timeout=12)

    merged = {}
    errors = []
    with _services().ThreadPoolExecutor(
        max_workers=min(_services().SEMANTIC_RECOMMENDATION_MAX_SEEDS, len(seeds)),
        thread_name_prefix="semantic-recommend",
    ) as pool:
        futures = {pool.submit(fetch, seed): seed for seed in seeds}
        for future in _services().as_completed(futures):
            seed = futures[future]
            try:
                payload = future.result()
            except Exception as exc:
                errors.append(f"{seed}: {_services().clean_text(str(exc), 240)}")
                continue
            rows = payload.get("recommendedPapers") if isinstance(payload, dict) else []
            if not isinstance(rows, list):
                rows = (
                    payload.get("data")
                    if isinstance(payload, dict)
                    and isinstance(payload.get("data"), list)
                    else []
                )
            for rank, item in enumerate(
                rows[: _services().SEMANTIC_RECOMMENDATION_MAX_RESULTS], start=1
            ):
                if not isinstance(item, dict):
                    continue
                record = _services()._semantic_recommendation_record(
                    item, rank, len(seeds)
                )
                if not record:
                    continue
                identity = _services().candidate_identity(record)
                existing = merged.get(identity)
                if not existing or (
                    record.get("recommendation_score", 0),
                    len(record.get("abstract", "")),
                ) > (
                    existing.get("recommendation_score", 0),
                    len(existing.get("abstract", "")),
                ):
                    merged[identity] = record
    candidates = sorted(
        merged.values(),
        key=lambda item: (
            -int(item.get("recommendation_score") or 0),
            int(item.get("recommendation_rank") or 0),
            item.get("title", ""),
        ),
    )[: _services().SEMANTIC_RECOMMENDATION_MAX_RESULTS]
    by_semantic_id = {}
    by_doi = {}
    for item in candidates:
        if item.get("external_id"):
            by_semantic_id[item["external_id"]] = {
                "score": item["recommendation_score"],
                "rank": item["recommendation_rank"],
                "seed_count": len(seeds),
                "provider": "Semantic Scholar",
            }
        if item.get("doi"):
            by_doi[item["doi"]] = {
                "score": item["recommendation_score"],
                "rank": item["recommendation_rank"],
                "seed_count": len(seeds),
                "provider": "Semantic Scholar",
            }
    return {
        "provider": "Semantic Scholar",
        "by_semantic_id": by_semantic_id,
        "by_doi": by_doi,
        "external_candidates": candidates,
        "related_count": len(candidates),
        "seed_count": len(seeds),
        "errors": errors[:8],
    }


def _load_openalex_recommendations(seed_ids: list[str]) -> dict:
    """Ask OpenAlex for its related-work graph and return bounded rank signals."""
    seed_ids = list(
        dict.fromkeys(
            _services()._openalex_work_id(value)
            for value in seed_ids
            if _services()._openalex_work_id(value)
        )
    )[:3]
    if not seed_ids:
        return {"by_openalex": {}, "by_doi": {}, "seed_count": 0}
    seed_url = "https://api.openalex.org/works?" + _services().urlencode(
        {
            "filter": "ids.openalex:" + "|".join(seed_ids),
            "per-page": len(seed_ids),
            "select": "id,related_works",
        }
    )
    seed_data = _services().http_json(seed_url, timeout=12)
    rank_by_id = {}
    for seed in seed_data.get("results", []):
        seed_work_id = _services()._openalex_work_id(seed.get("id", ""))
        for rank, raw_id in enumerate(seed.get("related_works") or [], start=1):
            work_id = _services()._openalex_work_id(raw_id)
            if not work_id or work_id == seed_work_id:
                continue
            item = rank_by_id.setdefault(work_id, {"rank": rank, "seed_count": 0})
            item["rank"] = min(item["rank"], rank)
            item["seed_count"] += 1
    ordered_ids = sorted(
        rank_by_id, key=lambda work_id: (rank_by_id[work_id]["rank"], work_id)
    )[:50]
    by_doi = {}
    external_candidates = []
    if ordered_ids:
        details_url = "https://api.openalex.org/works?" + _services().urlencode(
            {
                "filter": "ids.openalex:" + "|".join(ordered_ids),
                "per-page": len(ordered_ids),
                "select": "id,title,publication_year,doi,authorships,abstract_inverted_index,primary_location,type,cited_by_count",
            }
        )
        detail_data = _services().http_json(details_url, timeout=12)
        for item in detail_data.get("results", []):
            work_id = _services()._openalex_work_id(item.get("id", ""))
            rank_info = rank_by_id.get(work_id)
            if not rank_info:
                continue
            score = min(
                100,
                max(38, 100 - (rank_info["rank"] - 1) * 4)
                + min(12, (rank_info["seed_count"] - 1) * 6),
            )
            doi = _services().norm_doi(item.get("doi", ""))
            if doi:
                by_doi[doi] = {
                    "score": score,
                    "rank": rank_info["rank"],
                    "seed_count": rank_info["seed_count"],
                }
            record = _services()._openalex_relation_candidate(item, "OpenAlex 相关工作")
            if record.get("title"):
                record["recommendation_score"] = score
                record["recommendation_rank"] = rank_info["rank"]
                record["recommendation_reasons"] = [
                    "OpenAlex 根据已读/收藏论文的相关工作图谱召回"
                ]
                external_candidates.append(record)
    by_openalex = {}
    for work_id, rank_info in rank_by_id.items():
        score = min(
            100,
            max(38, 100 - (rank_info["rank"] - 1) * 4)
            + min(12, (rank_info["seed_count"] - 1) * 6),
        )
        by_openalex[work_id] = {
            "score": score,
            "rank": rank_info["rank"],
            "seed_count": rank_info["seed_count"],
        }
    external_candidates.sort(
        key=lambda item: (
            -item["recommendation_score"],
            item["recommendation_rank"],
            item["title"],
        )
    )
    return {
        "by_openalex": by_openalex,
        "by_doi": by_doi,
        "external_candidates": external_candidates[:50],
        "seed_count": len(seed_ids),
        "related_count": len(by_openalex),
    }


def _merge_recommendation_provider_results(
    openalex: dict | None, semantic: dict | None
) -> dict:
    """Merge provider recommendations while retaining per-provider provenance."""
    openalex = openalex if isinstance(openalex, dict) else {}
    semantic = semantic if isinstance(semantic, dict) else {}
    # Keep the historical OpenAlex-only ``external_candidates`` field for API
    # consumers that still expect it, but also expose one provider-neutral
    # candidate list. The latter lets the UI and MCP clients use both hosted
    # recommendation services without pretending that an S2 result came from
    # OpenAlex.
    openalex_candidates = []
    all_candidates = []
    seen = set()
    for provider_name, provider_candidates in (
        ("OpenAlex", openalex.get("external_candidates") or []),
        ("Semantic Scholar", semantic.get("external_candidates") or []),
    ):
        for item in provider_candidates:
            if not isinstance(item, dict):
                continue
            normalized = dict(item)
            normalized.setdefault("recommendation_provider", provider_name)
            normalized.setdefault(
                "recommendation_reasons", [f"{provider_name} 官方推荐服务召回"]
            )
            key = _services().candidate_identity(normalized)
            if key in seen:
                continue
            seen.add(key)
            all_candidates.append(normalized)
            if provider_name == "OpenAlex":
                openalex_candidates.append(normalized)
    all_candidates.sort(
        key=lambda item: (
            -int(item.get("recommendation_score") or 0),
            int(item.get("recommendation_rank") or 0),
            item.get("title", ""),
        )
    )
    openalex_candidates.sort(
        key=lambda item: (
            -int(item.get("recommendation_score") or 0),
            int(item.get("recommendation_rank") or 0),
            item.get("title", ""),
        )
    )
    by_doi = {}
    for source in (openalex, semantic):
        for doi, signal in (source.get("by_doi") or {}).items():
            current = by_doi.get(doi)
            if not current or int(signal.get("score") or 0) > int(
                current.get("score") or 0
            ):
                by_doi[doi] = dict(signal)
    return {
        "by_openalex": dict(openalex.get("by_openalex") or {}),
        "by_semantic_id": dict(semantic.get("by_semantic_id") or {}),
        "by_doi": by_doi,
        # Backwards-compatible field used by older integrations.
        "external_candidates": openalex_candidates[
            : _services().SEMANTIC_RECOMMENDATION_MAX_RESULTS
        ],
        # Provider-neutral field for new clients and the LitLoom UI.
        "external_recommendations": all_candidates[
            : _services().SEMANTIC_RECOMMENDATION_MAX_RESULTS
        ],
        "seed_count": max(
            int(openalex.get("seed_count") or 0), int(semantic.get("seed_count") or 0)
        ),
        "related_count": len(all_candidates),
        "providers": {
            "openalex": {
                "status": "ready" if openalex else "unavailable",
                "count": len(openalex.get("external_candidates") or []),
                "errors": list(openalex.get("errors") or []),
            },
            "semanticscholar": {
                "status": (
                    "ready"
                    if semantic
                    and (semantic.get("related_count") or not semantic.get("errors"))
                    else "unavailable"
                ),
                "count": len(semantic.get("external_candidates") or []),
                "errors": list(semantic.get("errors") or []),
            },
        },
    }


def _refresh_openalex_recommendations(
    cache_key: str, seed_ids: list[str], semantic_seed_ids: list[str] | None = None
) -> None:
    """Refresh hosted recommendations in parallel and degrade per provider."""
    semantic_seed_ids = list(semantic_seed_ids or [])
    results = {}
    errors = []
    with _services().ThreadPoolExecutor(
        max_workers=2 if semantic_seed_ids else 1,
        thread_name_prefix="recommend-provider",
    ) as pool:
        futures = {}
        if seed_ids:
            futures[
                pool.submit(_services()._load_openalex_recommendations, seed_ids)
            ] = "OpenAlex"
        if semantic_seed_ids:
            futures[
                pool.submit(
                    _services()._load_semantic_scholar_recommendations,
                    semantic_seed_ids,
                )
            ] = "Semantic Scholar"
        for future in _services().as_completed(futures):
            provider = futures[future]
            try:
                results[provider] = future.result()
            except Exception as exc:
                errors.append(f"{provider}：{_services().clean_text(str(exc), 240)}")
    for provider, result in results.items():
        for detail in (result.get("errors") or []) if isinstance(result, dict) else []:
            errors.append(f"{provider}：{_services().clean_text(detail, 240)}")
    merged = _services()._merge_recommendation_provider_results(
        results.get("OpenAlex"), results.get("Semantic Scholar")
    )
    merged["provider_errors"] = errors[:8]
    with _services().OPENALEX_RECOMMENDATION_CACHE_LOCK:
        previous = _services().OPENALEX_RECOMMENDATION_CACHE.get(cache_key) or {}
    if results or merged.get("external_candidates"):
        state = {
            "status": "ready",
            "rankings": merged,
            "updated_at": _services().now_iso(),
            "at": _services().time.monotonic(),
            "error": "；".join(errors[:4]),
        }
    else:
        state = {
            "status": "unavailable",
            "rankings": previous.get("rankings") or {},
            "updated_at": previous.get("updated_at", ""),
            "at": _services().time.monotonic(),
            "retry_at": _services().time.monotonic()
            + _services().OPENALEX_RECOMMENDATION_ERROR_BACKOFF_SECONDS,
            "error": "；".join(errors[:4]) or "推荐服务暂时不可用",
        }
    with _services().OPENALEX_RECOMMENDATION_CACHE_LOCK:
        _services().OPENALEX_RECOMMENDATION_CACHE[cache_key] = state


def _openalex_recommendation_snapshot(seed_paper_ids: list[str]) -> dict:
    seed_ids = _services()._openalex_seed_ids(seed_paper_ids)
    semantic_seed_ids = _services()._semantic_scholar_seed_ids(seed_paper_ids)
    if not seed_ids and not semantic_seed_ids:
        return {
            "status": "not_applicable",
            "rankings": {},
            "provider": "OpenAlex + Semantic Scholar",
            "message": "阅读记录中还没有可用于外部推荐的学术标识，暂用本地研究画像推荐。",
        }
    provider_label = " + ".join(
        name
        for name, enabled in (
            ("OpenAlex", bool(seed_ids)),
            ("Semantic Scholar", bool(semantic_seed_ids)),
        )
        if enabled
    )
    cache_key = ",".join(sorted(seed_ids))
    if semantic_seed_ids:
        cache_key = "oa:" + cache_key + "|s2:" + ",".join(sorted(semantic_seed_ids))
    now = _services().time.monotonic()
    with _services().OPENALEX_RECOMMENDATION_CACHE_LOCK:
        cached = _services().OPENALEX_RECOMMENDATION_CACHE.get(cache_key) or {}
        age = now - float(cached.get("at", 0) or 0)
        if cached.get("status") == "running":
            message = (
                "正在并行更新 OpenAlex 与 Semantic Scholar 推荐；已有结果继续生效。"
                if cached.get("rankings")
                else "正在并行获取外部推荐信号。"
            )
            return {
                "status": "running",
                "rankings": cached.get("rankings") or {},
                "provider": provider_label,
                "message": message,
                "updated_at": cached.get("updated_at", ""),
            }
        if (
            cached.get("status") == "ready"
            and age < _services().OPENALEX_RECOMMENDATION_TTL_SECONDS
        ):
            ranking_data = cached.get("rankings") or {}
            message = (
                "已结合 OpenAlex、Semantic Scholar 与本地阅读反馈排序。"
                if ranking_data.get("related_count")
                else "外部推荐暂无可用信号，当前根据本地研究画像推荐。"
            )
            if cached.get("error"):
                message += " 部分推荐服务暂不可用，已保留可用结果。"
            return {
                "status": "ready",
                "rankings": ranking_data,
                "provider": provider_label,
                "message": message,
                "updated_at": cached.get("updated_at", ""),
            }
        if cached.get("status") == "unavailable" and now < float(
            cached.get("retry_at", 0) or 0
        ):
            ranking_data = cached.get("rankings") or {}
            message = (
                "外部推荐服务暂不可用，保留上次信号并结合本地反馈。"
                if ranking_data.get("related_count")
                else "外部推荐服务暂不可用，当前使用本地研究画像推荐。"
            )
            return {
                "status": "unavailable",
                "rankings": ranking_data,
                "provider": provider_label,
                "message": message,
                "error": cached.get("error", ""),
                "updated_at": cached.get("updated_at", ""),
            }
        _services().OPENALEX_RECOMMENDATION_CACHE[cache_key] = {
            "status": "running",
            "rankings": cached.get("rankings") or {},
            "updated_at": cached.get("updated_at", ""),
            "at": now,
            "error": "",
        }
    with _services().OPENALEX_RECOMMENDATION_EXECUTOR_LOCK:
        if _services().OPENALEX_RECOMMENDATION_EXECUTOR is None:
            _services().OPENALEX_RECOMMENDATION_EXECUTOR = (
                _services().ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="openalex-recommend"
                )
            )
        executor = _services().OPENALEX_RECOMMENDATION_EXECUTOR
    try:
        executor.submit(
            _services()._refresh_openalex_recommendations,
            cache_key,
            seed_ids,
            semantic_seed_ids,
        )
    except RuntimeError:
        with _services().OPENALEX_RECOMMENDATION_CACHE_LOCK:
            _services().OPENALEX_RECOMMENDATION_CACHE[cache_key] = {
                "status": "unavailable",
                "rankings": cached.get("rankings") or {},
                "updated_at": cached.get("updated_at", ""),
                "at": _services().time.monotonic(),
                "retry_at": _services().time.monotonic() + 30,
                "error": "推荐任务暂时无法排队",
            }
    return {
        "status": "running",
        "rankings": cached.get("rankings") or {},
        "provider": provider_label,
        "message": "正在并行获取外部推荐信号。",
        "updated_at": cached.get("updated_at", ""),
    }


def _available_openalex_recommendations(rankings: dict, limit: int = 6) -> list[dict]:
    rankings = rankings or {}
    # ``external_recommendations`` is the provider-neutral contract. Fall
    # back to the legacy field so cached snapshots created before this change
    # remain usable after an upgrade.
    candidates = (
        rankings.get("external_recommendations")
        or rankings.get("external_candidates")
        or []
    )
    if not candidates:
        return []
    with _services().DB_LOCK, _services().db_session() as db:
        rows = db.execute(
            "SELECT p.doi,p.title_key,p.year,s.external_id FROM papers p "
            "LEFT JOIN source_records s ON s.paper_id=p.id WHERE p.status<>'excluded'"
        ).fetchall()
    known_dois = {_services().norm_doi(row["doi"]) for row in rows if row["doi"]}
    known_titles = {
        (row["title_key"], row["year"])
        for row in rows
        if row["title_key"] and row["year"]
    }
    known_openalex = {
        _services()._openalex_work_id(row["external_id"])
        for row in rows
        if row["external_id"]
    }
    known_external_ids = {
        _services().clean_text(row["external_id"], 500).casefold()
        for row in rows
        if row["external_id"]
    }
    selected = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        doi = _services().norm_doi(candidate.get("doi", ""))
        external_id = _services().clean_text(
            candidate.get("openalex_id") or candidate.get("external_id") or "", 500
        )
        work_id = _services()._openalex_work_id(external_id)
        semantic_id = _services().clean_text(
            candidate.get("paperId") or (external_id if not work_id else ""), 500
        )
        title_identity = (
            _services().title_key(candidate.get("title", "")),
            candidate.get("year"),
        )
        if (
            (doi and doi in known_dois)
            or (work_id and work_id in known_openalex)
            or (semantic_id and semantic_id.casefold() in known_external_ids)
            or (external_id and external_id.casefold() in known_external_ids)
            or title_identity in known_titles
        ):
            continue
        item = dict(candidate)
        provider_name = _services().clean_text(
            item.get("recommendation_provider")
            or ("OpenAlex" if work_id else "Semantic Scholar"),
            80,
        )
        item["url"] = item.get("url") or (
            ("https://openalex.org/" + work_id) if work_id else ""
        )
        item["recommendation_reasons"] = list(
            item.get("recommendation_reasons") or [f"{provider_name} 官方推荐服务召回"]
        )
        selected.append(item)
        if len(selected) >= max(1, min(int(limit), 12)):
            break
    return selected


def save_openalex_recommendation(external_id: str) -> dict:
    work_id = _services()._openalex_work_id(external_id)
    local_memory = _services().cached_derived(
        f"research_memory:base:{_services().database_change_token()}",
        lambda: _services()._research_memory(50),
        ttl_seconds=30.0,
    )
    seed_ids = _services()._openalex_seed_ids(
        local_memory.get("_provider_seed_paper_ids", [])
    )
    semantic_seed_ids = _services()._semantic_scholar_seed_ids(
        local_memory.get("_provider_seed_paper_ids", [])
    )
    cache_key = ",".join(sorted(seed_ids))
    if semantic_seed_ids:
        cache_key = "oa:" + cache_key + "|s2:" + ",".join(sorted(semantic_seed_ids))
    candidate = None
    with _services().OPENALEX_RECOMMENDATION_CACHE_LOCK:
        state = _services().OPENALEX_RECOMMENDATION_CACHE.get(cache_key) or {}
        cache_age = _services().time.monotonic() - float(state.get("at", 0) or 0)
        if cache_key and cache_age < _services().OPENALEX_RECOMMENDATION_TTL_SECONDS:
            rankings = state.get("rankings") or {}
            candidates = (
                rankings.get("external_recommendations")
                or rankings.get("external_candidates")
                or []
            )
            for item in candidates:
                item_external = _services().clean_text(
                    item.get("openalex_id") or item.get("external_id") or "", 500
                )
                matches_openalex = bool(
                    work_id and _services()._openalex_work_id(item_external) == work_id
                )
                requested_doi = _services().norm_doi(external_id)
                requested_arxiv = _services().norm_arxiv(external_id)
                matches_generic = bool(
                    not work_id
                    and (
                        item_external == _services().clean_text(external_id, 500)
                        or (
                            requested_doi
                            and _services().norm_doi(item.get("doi", ""))
                            == requested_doi
                        )
                        or (
                            requested_arxiv
                            and _services().norm_arxiv(item.get("arxiv_id", ""))
                            == requested_arxiv
                        )
                    )
                )
                if matches_openalex or matches_generic:
                    candidate = dict(item)
                    break
    if not candidate:
        raise LookupError("该推荐已过期或标识无效，请刷新推荐列表后重试")
    candidate["status"] = "to_read"
    provider_source = _services().clean_text(
        candidate.get("source") or ("openalex" if work_id else "semanticscholar"), 80
    )
    provider_external_id = _services().clean_text(
        candidate.get("openalex_id") or candidate.get("external_id") or external_id, 500
    )
    paper_id, created = _services().upsert_paper(
        candidate,
        source=provider_source,
        external_id=provider_external_id,
        raw={
            "recommendation_provider": candidate.get("recommendation_provider")
            or ("OpenAlex" if provider_source == "openalex" else "Semantic Scholar"),
            "recommendation_rank": candidate.get("recommendation_rank"),
        },
    )
    return {"id": paper_id, "created": created}


def research_memory(limit: int = 12) -> dict:
    limit = min(max(int(limit), 1), 50)
    # Recommendation and briefing endpoints often arrive together during the
    # home-page load. Build one bounded full projection and slice it per
    # caller so those requests never repeat the same full-library scan.
    token = _services().database_change_token()
    base = _services().cached_derived(
        f"research_memory:base:{token}",
        lambda: _services()._research_memory(50),
        ttl_seconds=30.0,
    )
    provider = _services()._openalex_recommendation_snapshot(
        base.get("_provider_seed_paper_ids", [])
    )
    external_recommendations = _services()._available_openalex_recommendations(
        provider.get("rankings", {}), limit=6
    )
    if provider.get("rankings"):
        provider_token = _services().clean_text(
            provider.get("updated_at") or provider.get("status"), 80
        )
        base = _services().cached_derived(
            f"research_memory:openalex:{token}:{provider_token}",
            lambda: _services()._research_memory(
                50, provider_rankings=provider["rankings"]
            ),
            ttl_seconds=30.0,
        )
    base = {key: value for key, value in base.items() if not key.startswith("_")}
    return {
        **base,
        "recommendations": list(base.get("recommendations", []))[:limit],
        "external_recommendations": external_recommendations,
        "recommendation_provider": {
            key: value for key, value in provider.items() if key != "rankings"
        },
    }


def _research_memory(limit: int = 12, provider_rankings: dict | None = None) -> dict:
    """Build a deterministic research profile and non-repeating queue.

    The profile is intentionally local and inspectable. It can later be
    replaced by embeddings without changing the API contract or losing the
    feedback history accumulated here.
    """
    limit = min(max(int(limit), 1), 50)
    with _services().DB_LOCK, _services().db_session() as db:
        papers = [
            _services().paper_dict(row)
            for row in db.execute("SELECT * FROM papers ORDER BY updated_at DESC")
        ]
        queries = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM saved_queries ORDER BY active DESC,updated_at DESC"
            )
        ]
        matches = [dict(row) for row in db.execute("SELECT * FROM query_matches")]
        feedback = [dict(row) for row in db.execute("SELECT * FROM learning_feedback")]
    for query in queries:
        query["sources"] = _services().json.loads(query.pop("sources_json", "[]"))
    by_id = {paper["id"]: paper for paper in papers}
    match_by_paper = {}
    for item in matches:
        match_by_paper.setdefault(item["paper_id"], []).append(item)
    feedback_by_paper = {
        item["paper_id"]: item for item in feedback if item.get("scope") == "global"
    }

    # Recommendation ranking does not need the full abstract payload. Keep a
    # bounded local text window and compute its expanded terms exactly once;
    # the previous implementation re-tokenized the same paper for every
    # anchor/query comparison, which became noticeable on larger libraries.
    def ranking_text(record):
        tags = record.get("tags") if isinstance(record, dict) else []
        if isinstance(tags, str):
            tags = [tags]
        abstract = str(record.get("abstract") or "")[:900]
        return (
            " ".join(
                str(record.get(key) or "") for key in ("title", "authors", "topic")
            )
            + " "
            + " ".join(tags or [])
            + " "
            + abstract
        )

    terms_by_paper = {
        paper["id"]: _services().expanded_relevance_terms(ranking_text(paper))
        for paper in papers
    }

    def overlap_terms(left_terms, right_terms):
        if not left_terms or not right_terms:
            return {"score": 0, "hits": []}
        hits = sorted(left_terms & right_terms)
        coverage = len(hits) / max(1, min(len(left_terms), len(right_terms)))
        jaccard = len(hits) / max(1, len(left_terms | right_terms))
        return {
            "score": min(100, round((coverage * 0.7 + jaccard * 0.3) * 100)),
            "hits": hits[:8],
        }

    profile_queries = [query for query in queries if query.get("active")] or queries
    query_match_cache = {}

    def query_match(candidate):
        cached = query_match_cache.get(candidate["id"])
        if cached is not None:
            return cached
        best = {"score": 0, "query": "", "reasons": []}
        for query in profile_queries:
            result = _services().score_relevance(
                candidate, query.get("query", ""), [], []
            )
            if result["score"] > best["score"]:
                best = {
                    "score": result["score"],
                    "query": query.get("name") or query.get("query", ""),
                    "reasons": result.get("reasons", []),
                }
        query_match_cache[candidate["id"]] = best
        return best

    anchors = [
        paper
        for paper in papers
        if paper.get("status") == "read"
        or paper.get("is_favorite")
        or any(
            item.get("user_decision") == "relevant"
            for item in match_by_paper.get(paper["id"], [])
        )
    ]
    # A favorite from another research direction must not steer a topic queue.
    # Keep global reading anchors only when there is no saved research query.
    if profile_queries:
        anchors = [paper for paper in anchors if query_match(paper)["score"] >= 35]
    profile_counter = {}
    for paper in anchors:
        for term in terms_by_paper.get(paper["id"], set()):
            profile_counter[term] = profile_counter.get(term, 0) + 1
    top_terms = [
        term
        for term, _ in sorted(
            profile_counter.items(), key=lambda pair: (-pair[1], pair[0])
        )[:12]
    ]
    topic_counts = {}
    for paper in anchors:
        topic = paper.get("topic") or "未分类"
        topic_counts[topic] = topic_counts.get(topic, 0) + 1

    def anchor_match(candidate):
        best = {"score": 0, "title": "", "hits": []}
        for anchor in anchors:
            result = overlap_terms(
                terms_by_paper.get(candidate["id"], set()),
                terms_by_paper.get(anchor["id"], set()),
            )
            if result["score"] > best["score"]:
                best = {**result, "title": anchor.get("title", "")}
        return best

    provider_scores = {}
    if provider_rankings:
        by_openalex = provider_rankings.get("by_openalex") or {}
        by_doi = provider_rankings.get("by_doi") or {}
        if by_openalex:
            work_ids = list(by_openalex)[:100]
            external_forms = list(
                dict.fromkeys(
                    form
                    for work_id in work_ids
                    for form in (work_id, "https://openalex.org/" + work_id)
                )
            )
            placeholders = ",".join("?" for _ in external_forms)
            with _services().DB_LOCK, _services().db_session() as db:
                source_rows = db.execute(
                    f"SELECT paper_id,external_id FROM source_records WHERE source='openalex' AND external_id IN ({placeholders})",
                    external_forms,
                ).fetchall()
            for row in source_rows:
                work_id = _services()._openalex_work_id(row["external_id"])
                signal = by_openalex.get(work_id)
                if signal:
                    current = provider_scores.get(row["paper_id"])
                    if not current or signal.get("score", 0) > current.get("score", 0):
                        provider_scores[row["paper_id"]] = signal
        for paper in papers:
            doi = _services().norm_doi(paper.get("doi", ""))
            signal = by_doi.get(doi) if doi else None
            if signal:
                current = provider_scores.get(paper["id"])
                if not current or signal.get("score", 0) > current.get("score", 0):
                    provider_scores[paper["id"]] = signal

    candidates = []
    for paper in papers:
        if paper["status"] not in {"inbox", "to_read", "later", "watching"}:
            continue
        global_feedback = feedback_by_paper.get(paper["id"], {})
        if global_feedback.get("decision") == "irrelevant":
            continue
        qmatch = query_match(paper)
        amatch = anchor_match(paper)
        seen = sum(
            int(item.get("seen_count") or 0)
            for item in match_by_paper.get(paper["id"], [])
        )
        ignored = sum(
            item.get("user_decision") in {"irrelevant", "ignored"}
            for item in match_by_paper.get(paper["id"], [])
        )
        score = (
            round(qmatch["score"] * 0.9 + amatch["score"] * 0.1)
            if profile_queries
            else amatch["score"]
        )
        if profile_queries:
            score = min(score, qmatch["score"] + 8)
        provider_signal = provider_scores.get(paper["id"])
        if provider_signal:
            score = round(score * 0.65 + int(provider_signal.get("score", 0)) * 0.35)
        if paper.get("topic") and paper.get("topic") in topic_counts:
            score += 8
        if seen > 1:
            score -= min(20, (seen - 1) * 5)
        score -= min(30, ignored * 15)
        if not anchors and not queries:
            score = 0
        reasons = []
        if qmatch["score"]:
            reasons.append(f"命中主题：{qmatch['query']}")
            if qmatch["reasons"]:
                reasons.append(qmatch["reasons"][0])
        elif profile_queries and qmatch["reasons"]:
            reasons.append(qmatch["reasons"][0])
        if amatch["score"] >= 35:
            reasons.append(f"接近已读论文：{amatch['title'][:36]}")
        if provider_signal:
            provider_name = _services().clean_text(
                provider_signal.get("provider") or "OpenAlex", 80
            )
            if provider_name.casefold() == "openalex":
                reasons.append(
                    f"OpenAlex 相关工作推荐（相似于 {int(provider_signal.get('seed_count', 1))} 篇阅读锚点）"
                )
            else:
                reasons.append(
                    f"{provider_name} 推荐（相似于 {int(provider_signal.get('seed_count', 1))} 篇阅读锚点）"
                )
        if paper.get("topic") and paper.get("topic") in topic_counts:
            reasons.append(f"属于你的研究主题：{paper['topic']}")
        if not seen:
            reasons.append("尚未在自动主题中重复出现")
        if not reasons:
            if not anchors and not queries:
                reasons.append("尚未形成研究画像，按最新入库保留")
            else:
                reasons.append("作为跨领域线索保留，建议先扫读摘要")
        item = {
            **paper,
            "recommendation_score": max(0, min(100, score)),
            "recommendation_reasons": reasons,
            "seen_count": seen,
        }
        candidates.append(item)
    # Keep fresh arrivals ahead when scores tie, then apply the learned score.
    candidates.sort(
        key=lambda item: (item.get("updated_at", ""), item.get("title", "")),
        reverse=True,
    )
    candidates.sort(key=lambda item: item["recommendation_score"], reverse=True)
    selected = []
    topic_slots = {}
    for item in candidates:
        topic = item.get("topic") or "未分类"
        if topic_slots.get(topic, 0) >= 3:
            continue
        selected.append(item)
        topic_slots[topic] = topic_slots.get(topic, 0) + 1
        if len(selected) >= limit:
            break
    return {
        "generated_at": _services().now_iso(),
        "profile": {
            "anchor_count": len(anchors),
            "top_terms": top_terms,
            "top_topics": sorted(
                topic_counts.items(), key=lambda pair: (-pair[1], pair[0])
            )[:8],
            "feedback_count": len(feedback),
        },
        "recommendations": selected,
        "_provider_seed_paper_ids": [paper["id"] for paper in anchors[:3]],
    }


def paper_connections(paper_id: str) -> dict:
    """Explain how one paper relates to saved topics and the local library."""
    with _services().DB_LOCK, _services().db_session() as db:
        paper_row = db.execute(
            "SELECT * FROM papers WHERE id=?", (paper_id,)
        ).fetchone()
        if not paper_row:
            raise ValueError("论文不存在")
        paper = _services().paper_dict(paper_row)
        queries = [
            dict(row)
            for row in db.execute(
                "SELECT id,name,query,active FROM saved_queries ORDER BY active DESC,updated_at DESC"
            )
        ]
        others = [
            _services().paper_dict(row)
            for row in db.execute(
                "SELECT * FROM papers WHERE id<>? AND status<> 'excluded' ORDER BY updated_at DESC",
                (paper_id,),
            )
        ]
    topics = []
    for query in queries:
        result = _services().text_overlap(
            _services().paper_text(paper), query.get("query", "")
        )
        reasons = (
            [f"命中词：{'、'.join(result['hits'][:6])}"]
            if result["hits"]
            else ["没有共享的明确词项"]
        )
        topics.append(
            {
                "id": query["id"],
                "name": query["name"] or query["query"],
                "active": bool(query["active"]),
                "score": result["score"],
                "tier": _services().connection_tier(result["score"]),
                "reasons": reasons,
            }
        )
    topics.sort(key=lambda item: (-item["score"], item["name"]))
    related = []
    for other in others:
        result = _services().text_overlap(
            _services().paper_text(paper), _services().paper_text(other)
        )
        if result["score"] < 10:
            continue
        related.append(
            {
                "id": other["id"],
                "title": other["title"],
                "year": other.get("year"),
                "status": other.get("status"),
                "topic": other.get("topic", ""),
                "score": result["score"],
                "tier": _services().connection_tier(result["score"]),
                "hits": result["hits"],
            }
        )
    related.sort(
        key=lambda item: (-item["score"], item.get("year") or 0, item["title"])
    )
    return {
        "paper_id": paper_id,
        "topics": topics,
        "related": related[:12],
        "generated_at": _services().now_iso(),
    }


def paper_dict(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["tags"] = _services().json.loads(item.pop("tags_json", "[]"))
    item["is_favorite"] = bool(item["is_favorite"])
    return item


def _hydrate_retrieval_metadata(paper: dict, source: str = "legacy_snapshot") -> dict:
    """Expose the explicit retrieval verdict for old saved search records.

    Before the retrieval/AI-screen split was introduced, candidate snapshots
    only carried the legacy ``relevance_*`` (or ``discovery_*``) fields.  The
    UI can still render those fields, but returning the explicit contract here
    keeps refreshed history consistent with newly created searches.
    """
    if not isinstance(paper, dict):
        return paper
    if paper.get("retrieval_relevance_score") in (None, ""):
        score = paper.get("relevance_score")
        if score in (None, ""):
            score = paper.get("discovery_score")
        if score not in (None, ""):
            paper["retrieval_relevance_score"] = score
    if not paper.get("retrieval_relevance_label"):
        label = paper.get("relevance_label") or paper.get("discovery_class")
        if label:
            paper["retrieval_relevance_label"] = label
    if not paper.get("retrieval_relevance_reasons"):
        reasons = paper.get("relevance_reasons") or paper.get("discovery_reasons") or []
        paper["retrieval_relevance_reasons"] = (
            list(reasons) if isinstance(reasons, (list, tuple)) else [str(reasons)]
        )
    if not paper.get("retrieval_relevance_source"):
        paper["retrieval_relevance_source"] = source
    return paper


def paper_detail(paper_id: str, include_ai_artifacts: bool = True) -> dict | None:
    """Return one paper and its durable metadata.

    The browser opens the detail record before it separately requests the
    analysis history.  Returning several potentially multi-megabyte model
    answers in that first request made reopening a paper unnecessarily slow
    and increased SQLite lock time while a background job was writing.  Keep
    the full payload as the backwards-compatible default for API callers, but
    allow the UI to request metadata-only artifact rows.
    """
    with _services().DB_LOCK, _services().db_session() as db:
        row = db.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
        if not row:
            return None
        item = _services().paper_dict(row)
        item["reading_card"] = _services().reading_card_dict(
            db.execute(
                "SELECT * FROM reading_cards WHERE paper_id=?", (paper_id,)
            ).fetchone(),
            paper_id,
        )
        item["sources"] = [
            dict(r)
            for r in db.execute(
                "SELECT source,external_id,fetched_at FROM source_records WHERE paper_id=? ORDER BY fetched_at DESC",
                (paper_id,),
            )
        ]
        item["attachments"] = [
            dict(r)
            for r in db.execute(
                "SELECT id,original_name,stored_name,sha256,byte_size,added_at FROM attachments WHERE paper_id=?",
                (paper_id,),
            )
        ]
        item["has_extracted_pages"] = bool(
            db.execute(
                "SELECT 1 FROM document_pages WHERE paper_id=? LIMIT 1", (paper_id,)
            ).fetchone()
        )
        artifact_columns = (
            "id,artifact_type,provider,model,prompt_version,input_sha256,answer,created_at"
            if include_ai_artifacts
            else "id,artifact_type,provider,model,prompt_version,input_sha256,created_at"
        )
        artifact_rows = [
            dict(r)
            for r in db.execute(
                f"SELECT {artifact_columns} FROM ai_artifacts WHERE paper_id=? ORDER BY created_at DESC LIMIT 5",
                (paper_id,),
            )
        ]
        # Keep the latest abstract brief discoverable even after many full-text
        # analyses have been generated and the mixed five-item window moves on.
        if not any(
            row.get("artifact_type") == "abstract_brief" for row in artifact_rows
        ):
            latest_brief = db.execute(
                f"SELECT {artifact_columns} FROM ai_artifacts WHERE paper_id=? AND artifact_type='abstract_brief' ORDER BY created_at DESC LIMIT 1",
                (paper_id,),
            ).fetchone()
            if latest_brief:
                artifact_rows.append(dict(latest_brief))
                artifact_rows.sort(
                    key=lambda row: row.get("created_at", ""), reverse=True
                )
        item["ai_artifacts"] = artifact_rows
    item["has_local_pdf"] = any(
        bool(attachment.get("stored_name"))
        and (
            _services().active_files_dir()
            / _services().Path(str(attachment["stored_name"])).name
        ).is_file()
        for attachment in item["attachments"]
    )
    item["has_fulltext_source"] = item["has_local_pdf"] or item["has_extracted_pages"]
    item.pop("has_extracted_pages", None)
    return item


def refresh_search_content(db: sqlite3.Connection, paper_id: str) -> None:
    """Refresh one paper's derived search document inside the caller transaction."""
    row = db.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
    if not row:
        db.execute("DELETE FROM paper_search_content WHERE paper_id=?", (paper_id,))
        return
    card = db.execute(
        "SELECT * FROM reading_cards WHERE paper_id=?", (paper_id,)
    ).fetchone()
    card_text = (
        " ".join(str(card[field] or "") for field in _services().READING_CARD_FIELDS)
        if card
        else ""
    )
    try:
        tags = " ".join(_services().json.loads(row["tags_json"] or "[]"))
    except (TypeError, _services().json.JSONDecodeError):
        tags = row["tags_json"] or ""
    db.execute(
        """INSERT INTO paper_search_content(paper_id,title,authors,abstract,topic,tags,notes,reading_card,pdf_text)
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(paper_id) DO UPDATE SET title=excluded.title,authors=excluded.authors,abstract=excluded.abstract,
        topic=excluded.topic,tags=excluded.tags,notes=excluded.notes,reading_card=excluded.reading_card,pdf_text=excluded.pdf_text""",
        (
            paper_id,
            row["title"] or "",
            row["authors"] or "",
            row["abstract"] or "",
            row["topic"] or "",
            tags,
            row["notes"] or "",
            card_text,
            "",
        ),
    )


def rebuild_search_index() -> dict:
    """Rebuild the derived local search index without changing source records."""
    started = _services().time.perf_counter()
    with _services().DB_LOCK, _services().db_session() as db:
        db.execute("DELETE FROM paper_search_content")
        db.execute("DELETE FROM paper_page_search_content")
        for row in db.execute("SELECT id FROM papers ORDER BY id").fetchall():
            _services().refresh_search_content(db, row["id"])
        fts = _services().database_table_exists(db, "paper_search")
        page_fts = _services().database_table_exists(db, "paper_page_search")
        db.execute(
            "INSERT INTO paper_page_search_content(page_id,paper_id,text) SELECT id,paper_id,text FROM document_pages"
        )
        if fts:
            try:
                db.execute("INSERT INTO paper_search(paper_search) VALUES('rebuild')")
            except _services().sqlite3.OperationalError:
                fts = False
        if page_fts:
            try:
                db.execute(
                    "INSERT INTO paper_page_search(paper_page_search) VALUES('rebuild')"
                )
            except _services().sqlite3.OperationalError:
                page_fts = False
        count = db.execute("SELECT COUNT(*) FROM paper_search_content").fetchone()[0]
        page_count = db.execute(
            "SELECT COUNT(*) FROM paper_page_search_content"
        ).fetchone()[0]
        _services().record_audit(
            db,
            "maintenance.search_index_rebuilt",
            "search_index",
            "",
            {
                "documents": count,
                "page_documents": page_count,
                "fts5": fts and page_fts,
            },
        )
    return {
        "rebuilt": True,
        "documents": count,
        "page_documents": page_count,
        "fts5": fts and page_fts,
        "duration_ms": round((_services().time.perf_counter() - started) * 1000, 2),
        "completed_at": _services().now_iso(),
    }


def search_library(
    query: str,
    limit: int = 40,
    offset: int = 0,
    status: str = "",
    topic: str = "",
    filters: dict | None = None,
) -> dict:
    """Search papers and local reading evidence, returning snippets and match fields."""
    query = _services().clean_text(query, 500)
    limit = min(max(int(limit), 1), 200)
    offset = max(0, min(int(offset), 1000000))
    clauses, args = [], []
    filters = filters or {}
    if status:
        statuses = [
            _services().clean_text(value, 40)
            for value in str(status).split(",")
            if _services().clean_text(value, 40)
        ]
        if len(statuses) == 1:
            clauses.append("p.status=?")
            args.append(statuses[0])
        elif statuses:
            clauses.append("p.status IN (" + ",".join("?" for _ in statuses) + ")")
            args.extend(statuses)
    if topic:
        clauses.append("p.topic=?")
        args.append(topic)
    if filters.get("favorite"):
        clauses.append("p.is_favorite=1")
    if filters.get("has_reading_card"):
        clauses.append("EXISTS (SELECT 1 FROM reading_cards rc WHERE rc.paper_id=p.id)")
    for key, op in (("year_from", ">="), ("year_to", "<=")):
        try:
            year = int(filters.get(key)) if filters.get(key) not in (None, "") else None
        except (TypeError, ValueError):
            year = None
        if year:
            clauses.append(f"p.year{op}?")
            args.append(year)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with _services().DB_LOCK, _services().db_session() as db:
        total_args = list(args)
        if query:
            fts = _services().database_table_exists(
                db, "paper_search"
            ) and _services().database_table_exists(db, "paper_page_search")
            ascii_terms = [
                term
                for term in _services().re.findall(
                    r"[A-Za-z0-9][A-Za-z0-9_-]{1,}", query
                )
            ]
            use_fts = fts and bool(ascii_terms)
            if use_fts:
                match = " AND ".join(
                    f'"{term.replace(chr(34), "")}"*' for term in ascii_terms[:12]
                )
                # The content table is joined for stable paper ids; FTS is used
                # only to rank/select rowids because it stores the external content rowid.
                clauses_query = "(p.id IN (SELECT pc.paper_id FROM paper_search JOIN paper_search_content pc ON pc.rowid=paper_search.rowid WHERE paper_search MATCH ?) OR p.id IN (SELECT ppc.paper_id FROM paper_page_search JOIN paper_page_search_content ppc ON ppc.rowid=paper_page_search.rowid WHERE paper_page_search MATCH ?))"
                query_args = [match, match]
            else:
                clauses_query = "(p.title LIKE ? OR p.authors LIKE ? OR p.abstract LIKE ? OR p.topic LIKE ? OR p.notes LIKE ? OR p.doi LIKE ? OR ps.tags LIKE ? OR ps.reading_card LIKE ? OR EXISTS (SELECT 1 FROM document_pages dp WHERE dp.paper_id=p.id AND dp.text LIKE ?))"
                like = f"%{query}%"
                query_args = [like] * 9
            if clauses_query:
                clauses.append(clauses_query)
                args.extend(query_args)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        # Fallback queries need the content table join; FTS queries use a
        # correlated subquery and stay independent of FTS column layout.
        join = (
            " JOIN paper_search_content ps ON ps.paper_id=p.id"
            if query and not (fts and ascii_terms)
            else ""
        )
        total = db.execute(
            f"SELECT COUNT(*) FROM papers p{join}{where}", tuple(args)
        ).fetchone()[0]
        order_by = {
            "oldest": "p.updated_at ASC,p.id ASC",
            "year_desc": "p.year DESC,p.updated_at DESC",
            "year_asc": "p.year ASC,p.updated_at DESC",
            "title": "p.title COLLATE NOCASE ASC,p.id ASC",
        }.get(filters.get("sort"), "p.is_favorite DESC,p.updated_at DESC,p.id ASC")
        ranked_search = bool(
            query
            and fts
            and ascii_terms
            and filters.get("sort") not in {"oldest", "year_desc", "year_asc", "title"}
        )
        rank_select = (
            ", (SELECT MIN(score) FROM (SELECT bm25(paper_search) AS score FROM paper_search JOIN paper_search_content rank_pc ON rank_pc.rowid=paper_search.rowid WHERE paper_search MATCH ? AND rank_pc.paper_id=p.id UNION ALL SELECT bm25(paper_page_search) AS score FROM paper_page_search JOIN paper_page_search_content rank_ppc ON rank_ppc.rowid=paper_page_search.rowid WHERE paper_page_search MATCH ? AND rank_ppc.paper_id=p.id)) AS search_rank"
            if ranked_search
            else ""
        )
        page_select = (
            ", (SELECT snippet(paper_page_search,0,'','','…',30) FROM paper_page_search JOIN paper_page_search_content page_pc ON page_pc.rowid=paper_page_search.rowid WHERE paper_page_search MATCH ? AND page_pc.paper_id=p.id LIMIT 1) AS page_match"
            if query and fts and ascii_terms
            else (
                ", (SELECT substr(dp.text,1,2000) FROM document_pages dp WHERE dp.paper_id=p.id AND dp.text LIKE ? ORDER BY dp.page_number LIMIT 1) AS page_match"
                if query
                else ",'' AS page_match"
            )
        )
        if ranked_search:
            order_by = "p.is_favorite DESC,search_rank ASC,p.updated_at DESC,p.id ASC"
        rank_args = [match, match] if ranked_search else []
        page_args = (
            [match]
            if query and fts and ascii_terms
            else ([f"%{query}%"] if query else [])
        )
        row_args = rank_args + page_args + args
        rows = db.execute(
            f"SELECT p.*,ps.reading_card,ps.pdf_text{rank_select}{page_select} FROM papers p{join or ' LEFT JOIN paper_search_content ps ON ps.paper_id=p.id'}{where} ORDER BY {order_by} LIMIT ? OFFSET ?",
            (*row_args, limit, offset),
        ).fetchall()
        results = []
        ids = [row["id"] for row in rows]
        cards, sources, attachments, artifacts = {}, {}, {}, {}
        if ids:
            marks = ",".join("?" for _ in ids)
            cards = {
                row["paper_id"]: row
                for row in db.execute(
                    f"SELECT * FROM reading_cards WHERE paper_id IN ({marks})", ids
                )
            }
            for row in db.execute(
                f"SELECT paper_id,source,external_id,fetched_at FROM source_records WHERE paper_id IN ({marks}) ORDER BY fetched_at DESC",
                ids,
            ):
                sources.setdefault(row["paper_id"], []).append(dict(row))
            for row in db.execute(
                f"SELECT paper_id,id,original_name,stored_name,sha256,byte_size,added_at FROM attachments WHERE paper_id IN ({marks})",
                ids,
            ):
                attachments.setdefault(row["paper_id"], []).append(dict(row))
            for row in db.execute(
                f"SELECT paper_id,id,artifact_type,provider,model,prompt_version,input_sha256,answer,created_at FROM ai_artifacts WHERE paper_id IN ({marks}) ORDER BY created_at DESC",
                ids,
            ):
                artifacts.setdefault(row["paper_id"], []).append(dict(row))
        for row in rows:
            item = _services().paper_dict(row)
            item.pop("pdf_text", None)
            item["reading_card"] = _services().reading_card_dict(
                cards.get(item["id"]), item["id"]
            )
            item["sources"] = sources.get(item["id"], [])
            item["attachments"] = attachments.get(item["id"], [])
            item["ai_artifacts"] = artifacts.get(item["id"], [])[:5]
            hay = {
                "title": row["title"] or "",
                "authors": row["authors"] or "",
                "abstract": row["abstract"] or "",
                "topic": row["topic"] or "",
                "tags": row["tags_json"] or "",
                "notes": row["notes"] or "",
                "reading_card": row["reading_card"] or "",
                "pdf_text": row["page_match"] or "",
            }
            lower = query.casefold()
            fields = [
                name
                for name, value in hay.items()
                if lower and lower in value.casefold()
            ]
            if not fields and query:
                terms = [
                    term.casefold()
                    for term in _services().re.findall(
                        r"[\w-]{2,}", query, flags=_services().re.UNICODE
                    )
                ]
                fields = [
                    name
                    for name, value in hay.items()
                    if any(term in value.casefold() for term in terms)
                ]
            source = next(
                (
                    name
                    for name in (
                        "title",
                        "authors",
                        "abstract",
                        "topic",
                        "notes",
                        "reading_card",
                        "pdf_text",
                    )
                    if name in fields
                ),
                "",
            )
            value = hay.get(source, "")
            pos = value.casefold().find(lower) if lower else -1
            if pos < 0 and value:
                pos = 0
            item.update(
                {
                    "match_fields": fields,
                    "match_source": source,
                    "snippet": (
                        value[max(0, pos - 100) : pos + 260] if value else ""
                    ).strip(),
                }
            )
            results.append(item)
    return {
        "query": query,
        "results": results,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(results) < total,
        "generated_at": _services().now_iso(),
    }


def maintenance_status() -> dict:
    with _services().DB_LOCK, _services().db_session() as db:
        counts = {
            "papers": db.execute("SELECT COUNT(*) FROM papers").fetchone()[0],
            "search_documents": db.execute(
                "SELECT COUNT(*) FROM paper_search_content"
            ).fetchone()[0],
            "page_search_documents": db.execute(
                "SELECT COUNT(*) FROM paper_page_search_content"
            ).fetchone()[0],
            "revisions": db.execute("SELECT COUNT(*) FROM paper_revisions").fetchone()[
                0
            ],
            "attachments": db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0],
            "pages": db.execute("SELECT COUNT(*) FROM document_pages").fetchone()[0],
            "sync_jobs": db.execute("SELECT COUNT(*) FROM sync_jobs").fetchone()[0],
        }
        job_status = {
            row["status"]: row["n"]
            for row in db.execute(
                "SELECT status,COUNT(*) AS n FROM sync_jobs GROUP BY status"
            )
        }
        fts5 = _services().database_table_exists(db, "paper_search") and _services().database_table_exists(db, "paper_page_search")
        schema = db.execute(
            "SELECT value FROM app_meta WHERE key='schema_version'"
        ).fetchone()
        orphan_attachments = db.execute(
            "SELECT COUNT(*) FROM attachments a LEFT JOIN papers p ON p.id=a.paper_id WHERE p.id IS NULL"
        ).fetchone()[0]
        orphan_pages = db.execute(
            "SELECT COUNT(*) FROM document_pages d LEFT JOIN papers p ON p.id=d.paper_id WHERE p.id IS NULL"
        ).fetchone()[0]
        index_ok = counts["papers"] == counts["search_documents"]
        page_index_ok = counts["pages"] == counts["page_search_documents"]
        database_bytes = (
            db.execute("SELECT pg_database_size(current_database())").fetchone()[0]
            if _services().using_postgres()
            else (
                _services().DB_PATH.stat().st_size
                if _services().DB_PATH.exists()
                else 0
            )
        )
    backup_dir = _services().active_backup_dir()
    backups = (
        sorted(
            [
                *backup_dir.glob("researchdesk-backup-*.zip"),
                *backup_dir.glob("researchdesk-auto-*.zip"),
            ],
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        if backup_dir.exists()
        else []
    )
    policy = _services().get_backup_policy()
    return {
        "database": {
            "path": (
                _services().postgres_database_label(
                    _services().postgres_database_url()
                )
                if _services().using_postgres()
                else str(_services().DB_PATH)
            ),
            "schema_version": schema[0] if schema else "unknown",
            "bytes": database_bytes,
            "wal_bytes": (
                _services().Path(str(_services().DB_PATH) + "-wal").stat().st_size
                if not _services().using_postgres()
                and _services().Path(str(_services().DB_PATH) + "-wal").exists()
                else 0
            ),
        },
        "counts": counts,
        "jobs": job_status,
        "index": {
            "fts5": fts5,
            "search_backend": (
                "PostgreSQL pg_trgm"
                if _services().using_postgres()
                else "SQLite FTS5" if fts5 else "字段降级"
            ),
            "documents_match_papers": index_ok,
            "page_documents_match_pages": page_index_ok,
        },
        "orphans": {"attachments": orphan_attachments, "pages": orphan_pages},
        "backups": {
            "automatic_enabled": policy["enabled"],
            "interval_hours": policy["interval_hours"],
            "retention": policy["retention"],
            "count": len(backups),
            "latest": backups[0].name if backups else "",
            "last_success_at": policy["last_success_at"],
            "next_run_at": policy["next_run_at"],
            "last_error": policy["last_error"],
        },
        "checked_at": _services().now_iso(),
    }


def get_backup_policy() -> dict:
    defaults = {
        "enabled": _services().AUTO_BACKUP_INTERVAL_HOURS > 0,
        "interval_hours": max(1, int(_services().AUTO_BACKUP_INTERVAL_HOURS or 24)),
        "retention": _services().BACKUP_RETENTION,
        "last_success_at": "",
        "next_run_at": "",
        "last_error": "",
    }
    with _services().DB_LOCK, _services().db_session() as db:
        row = db.execute(
            "SELECT value FROM app_meta WHERE key='backup_policy'"
        ).fetchone()
    if not row:
        return defaults
    try:
        stored = _services().json.loads(row[0])
    except (_services().json.JSONDecodeError, TypeError):
        return defaults
    if not isinstance(stored, dict):
        return defaults
    result = defaults | {
        key: stored[key]
        for key in (
            "enabled",
            "interval_hours",
            "retention",
            "last_success_at",
            "next_run_at",
            "last_error",
        )
        if key in stored
    }
    result["enabled"] = result["enabled"] is True
    try:
        result["interval_hours"] = min(8760, max(1, int(result["interval_hours"])))
        result["retention"] = min(365, max(1, int(result["retention"])))
    except (TypeError, ValueError):
        return defaults
    for key in ("last_success_at", "next_run_at", "last_error"):
        result[key] = _services().clean_text(result[key], 1000)
    return result


def save_backup_policy(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("备份设置必须是 JSON 对象")
    current = _services().get_backup_policy()
    enabled_value = data.get("enabled", current["enabled"])
    if not isinstance(enabled_value, bool):
        raise ValueError("自动备份开关必须是布尔值")
    try:
        interval = int(data.get("interval_hours", current["interval_hours"]))
        retention = int(data.get("retention", current["retention"]))
    except (TypeError, ValueError):
        raise ValueError("备份间隔和保留数量必须为整数")
    if (
        isinstance(data.get("interval_hours"), float)
        and not data["interval_hours"].is_integer()
    ):
        raise ValueError("备份间隔必须为整数")
    if isinstance(data.get("retention"), float) and not data["retention"].is_integer():
        raise ValueError("保留数量必须为整数")
    if not 1 <= interval <= 8760:
        raise ValueError("自动备份间隔需在 1 到 8760 小时之间")
    if not 1 <= retention <= 365:
        raise ValueError("自动备份保留数量需在 1 到 365 份之间")
    enabled = enabled_value
    updated = current | {
        "enabled": enabled,
        "interval_hours": interval,
        "retention": retention,
        "last_error": "",
    }
    if enabled:
        updated["next_run_at"] = _services().now_iso()
    with _services().DB_LOCK, _services().db_session() as db:
        db.execute(
            "INSERT INTO app_meta(key,value) VALUES('backup_policy',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_services().json.dumps(updated, ensure_ascii=False),),
        )
        _services().record_audit(
            db,
            "backup.policy_updated",
            "backup",
            "automatic",
            {"enabled": enabled, "interval_hours": interval, "retention": retention},
        )
    return updated


def prune_backup_archives(retention: int | None = None) -> int:
    """Keep the configured number of automatic archives; preserve manual exports."""
    backup_dir = _services().active_backup_dir()
    if not backup_dir.exists():
        return 0
    retention = min(
        365,
        max(
            1,
            int(
                retention
                if retention is not None
                else _services().get_backup_policy()["retention"]
            ),
        ),
    )
    archives = sorted(
        backup_dir.glob("researchdesk-auto-*.zip"),
        key=lambda item: item.name,
        reverse=True,
    )
    removed = 0
    for archive in archives[retention:]:
        try:
            archive.unlink()
            removed += 1
        except OSError:
            # A user may be downloading an archive or have it locked on Windows.
            continue
    return removed


def create_backup_archive(automatic: bool = False) -> dict:
    """Create a consistent database/files archive for manual or scheduled backups."""
    with _services().BACKUP_LOCK:
        backup_dir = _services().active_backup_dir()
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = (
            _services().datetime.now().strftime("%Y%m%d-%H%M%S")
            + "-"
            + _services().uuid.uuid4().hex[:8]
        )
        prefix = "researchdesk-auto" if automatic else "researchdesk-backup"
        destination = backup_dir / f"{prefix}-{stamp}.zip"
        temporary_archive = backup_dir / f".{prefix}-{stamp}.tmp"
        database_filename = (
            "litloom-postgresql.dump"
            if _services().using_postgres()
            else "literature.sqlite3"
        )
        copy_path = backup_dir / (
            f"snapshot-{stamp}.dump"
            if _services().using_postgres()
            else f"snapshot-{stamp}.sqlite3"
        )
        try:
            if _services().using_postgres():
                _services().postgres_dump_database(
                    _services().postgres_database_url(), copy_path
                )
            else:
                with _services().DB_LOCK:
                    live = _services().connect()
                    try:
                        target = _services().sqlite3.connect(copy_path)
                        try:
                            live.backup(target)
                        finally:
                            target.close()
                    finally:
                        live.close()
            files = []
            for file in (
                sorted(_services().active_files_dir().glob("*.pdf"))
                if _services().active_files_dir().exists()
                else []
            ):
                files.append(
                    {
                        "path": "files/" + file.name,
                        "sha256": _services().sha256_file(file),
                        "bytes": file.stat().st_size,
                    }
                )
            manifest = {
                "product": "LitLoom",
                "app_version": _services().APP_VERSION,
                "schema_version": _services().DB_SCHEMA_VERSION,
                "created_at": _services().now_iso(),
                "automatic": bool(automatic),
                "database": {
                    "path": database_filename,
                    "sha256": _services().sha256_file(copy_path),
                },
                "files": files,
            }
            with _services().zipfile.ZipFile(
                temporary_archive, "w", _services().zipfile.ZIP_DEFLATED
            ) as zf:
                zf.write(copy_path, database_filename)
                if _services().active_files_dir().exists():
                    for file in _services().active_files_dir().glob("*.pdf"):
                        zf.write(file, "files/" + file.name)
                zf.writestr(
                    "manifest.json",
                    _services().json.dumps(manifest, ensure_ascii=False, indent=2),
                )
            _services().os.replace(temporary_archive, destination)
            with _services().DB_LOCK, _services().db_session() as db:
                _services().record_audit(
                    db,
                    "backup.created",
                    "backup",
                    destination.name,
                    {
                        "automatic": bool(automatic),
                        "attachment_count": len(files),
                        "sha256": manifest["database"]["sha256"],
                    },
                )
            _services().prune_backup_archives(
                _services().get_backup_policy()["retention"]
            )
            return {
                "path": str(destination),
                "filename": destination.name,
                "download_url": "/api/backups/" + destination.name,
                "papers": manifest["database"]["sha256"],
                "attachment_count": len(files),
                "manifest": manifest,
            }
        finally:
            copy_path.unlink(missing_ok=True)
            temporary_archive.unlink(missing_ok=True)


def scheduled_backup_tick() -> None:
    if not _services().AUTO_BACKUP_RUN_LOCK.acquire(blocking=False):
        return
    try:
        policy = _services().get_backup_policy()
        if not policy["enabled"]:
            return
        now = _services().datetime.now(_services().timezone.utc).replace(microsecond=0)
        try:
            next_run = (
                _services().datetime.fromisoformat(policy["next_run_at"])
                if policy["next_run_at"]
                else None
            )
        except ValueError:
            next_run = None
        if next_run and next_run > now:
            return
        try:
            _services().create_backup_archive(automatic=True)
            policy["last_success_at"] = _services().now_iso()
            policy["last_error"] = ""
            policy["next_run_at"] = (
                (
                    _services().datetime.now(_services().timezone.utc)
                    + _services().timedelta(hours=policy["interval_hours"])
                )
                .replace(microsecond=0)
                .isoformat()
            )
        except Exception as exc:
            policy["last_error"] = _services().clean_text(str(exc), 1000)
            retry_hours = min(policy["interval_hours"], 6)
            policy["next_run_at"] = (
                (
                    _services().datetime.now(_services().timezone.utc)
                    + _services().timedelta(hours=retry_hours)
                )
                .replace(microsecond=0)
                .isoformat()
            )
        with _services().DB_LOCK, _services().db_session() as db:
            db.execute(
                "INSERT INTO app_meta(key,value) VALUES('backup_policy',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_services().json.dumps(policy, ensure_ascii=False),),
            )
    finally:
        _services().AUTO_BACKUP_RUN_LOCK.release()


def paper_history(paper_id: str, limit: int = 100) -> list[dict]:
    with _services().DB_LOCK, _services().db_session() as db:
        rows = db.execute(
            "SELECT id,paper_id,revision_type,content_json,source,created_at FROM paper_revisions WHERE paper_id=? ORDER BY id DESC LIMIT ?",
            (paper_id, min(max(int(limit), 1), 500)),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["content"] = _services().json.loads(item.pop("content_json"))
        except _services().json.JSONDecodeError:
            item["content"] = {}
        result.append(item)
    return result


def reading_card_dict(row: sqlite3.Row | dict | None, paper_id: str) -> dict:
    item = {field: "" for field in _services().READING_CARD_FIELDS}
    item.update({"paper_id": paper_id, "created_at": "", "updated_at": ""})
    if row:
        values = dict(row)
        for field in (*_services().READING_CARD_FIELDS, "created_at", "updated_at"):
            if field in values:
                item[field] = values[field] or ""
    item["completed_fields"] = sum(
        bool(item[field].strip()) for field in _services().READING_CARD_FIELDS
    )
    item["field_count"] = len(_services().READING_CARD_FIELDS)
    item["completion_pct"] = round(
        item["completed_fields"] / len(_services().READING_CARD_FIELDS) * 100
    )
    return item


def record_paper_revision(
    db: sqlite3.Connection,
    paper_id: str,
    revision_type: str,
    content: dict,
    source: str = "user",
) -> None:
    serialized = _services().json.dumps(content, ensure_ascii=False, sort_keys=True)
    latest = db.execute(
        "SELECT content_json FROM paper_revisions WHERE paper_id=? AND revision_type=? ORDER BY id DESC LIMIT 1",
        (paper_id, revision_type),
    ).fetchone()
    if latest and latest[0] == serialized:
        return
    db.execute(
        "INSERT INTO paper_revisions(paper_id,revision_type,content_json,source,created_at) VALUES(?,?,?,?,?)",
        (
            paper_id,
            revision_type,
            serialized,
            _services().clean_text(source, 40) or "user",
            _services().now_iso(),
        ),
    )


def save_reading_card(
    db: sqlite3.Connection, paper_id: str, data: dict, source: str = "user"
) -> dict:
    values = {
        field: _services().clean_text(data.get(field, ""), 20000)
        for field in _services().READING_CARD_FIELDS
    }
    _services().record_paper_revision(db, paper_id, "reading_card", values, source)
    now = _services().now_iso()
    columns = ",".join(_services().READING_CARD_FIELDS)
    placeholders = ",".join("?" for _ in _services().READING_CARD_FIELDS)
    assignments = ",".join(
        f"{field}=excluded.{field}" for field in _services().READING_CARD_FIELDS
    )
    db.execute(
        f"INSERT INTO reading_cards(paper_id,{columns},created_at,updated_at) VALUES(?,{placeholders},?,?) "
        f"ON CONFLICT(paper_id) DO UPDATE SET {assignments},updated_at=excluded.updated_at",
        (paper_id, *values.values(), now, now),
    )
    return _services().reading_card_dict(
        db.execute(
            "SELECT * FROM reading_cards WHERE paper_id=?", (paper_id,)
        ).fetchone(),
        paper_id,
    )


def batch_update_papers(ids, updates: dict) -> dict:
    if not isinstance(ids, list):
        raise ValueError("ids 必须是论文 ID 列表")
    if not 1 <= len(ids) <= 500:
        raise ValueError("每次批量操作需选择 1 到 500 篇论文")
    paper_ids = list(
        dict.fromkeys(
            _services().clean_text(item, 200)
            for item in ids
            if _services().clean_text(item, 200)
        )
    )
    if not paper_ids:
        raise ValueError("请至少选择一篇论文")
    if not isinstance(updates, dict):
        raise ValueError("updates 必须是对象")
    allowed = {"status", "topic", "is_favorite", "tags"}
    normalized = {key: value for key, value in updates.items() if key in allowed}
    if not normalized:
        raise ValueError("没有可更新的字段")
    if (
        "status" in normalized
        and normalized["status"] not in _services().PAPER_STATUSES
    ):
        raise ValueError("未知阅读状态")
    if "topic" in normalized:
        normalized["topic"] = _services().clean_text(normalized["topic"], 300)
    if "is_favorite" in normalized:
        favorite = normalized["is_favorite"]
        if favorite not in (True, False, 0, 1):
            raise ValueError("收藏状态必须是布尔值")
        normalized["is_favorite"] = int(bool(favorite))
    if "tags" in normalized:
        tags = normalized["tags"]
        if isinstance(tags, str):
            tags = [part.strip() for part in tags.split(",") if part.strip()]
        if not isinstance(tags, list):
            raise ValueError("标签必须是列表或逗号分隔文本")
        normalized["tags_json"] = _services().json.dumps(
            list(
                dict.fromkeys(
                    _services().clean_text(tag, 80)
                    for tag in tags
                    if _services().clean_text(tag, 80)
                )
            )[:50],
            ensure_ascii=False,
        )
        normalized.pop("tags")
    assignments = ",".join(f"{key}=?" for key in normalized)
    values = list(normalized.values())
    now = _services().now_iso()
    with _services().DB_LOCK, _services().db_session() as db:
        placeholders = ",".join("?" for _ in paper_ids)
        existing = {
            row[0]
            for row in db.execute(
                f"SELECT id FROM papers WHERE id IN ({placeholders})", paper_ids
            )
        }
        skipped = [paper_id for paper_id in paper_ids if paper_id not in existing]
        updated_ids = [paper_id for paper_id in paper_ids if paper_id in existing]
        if updated_ids:
            update_placeholders = ",".join("?" for _ in updated_ids)
            db.execute(
                f"UPDATE papers SET {assignments},updated_at=? WHERE id IN ({update_placeholders})",
                (*values, now, *updated_ids),
            )
            _services().record_audit(
                db,
                "papers.batch_updated",
                "paper",
                "",
                {
                    "selected": len(paper_ids),
                    "updated": len(updated_ids),
                    "skipped": len(skipped),
                    "fields": sorted(updates.keys()),
                },
            )
    return {"updated": len(updated_ids), "skipped": skipped}


def delete_papers(ids, *, protect_favorites: bool = False) -> dict:
    """Permanently remove selected library papers and their paper-owned data.

    Search runs and their candidate history remain available for auditability;
    records that point at a deleted local paper are detached. Attachment files
    are removed only when no remaining paper references the same content hash.
    """
    if not isinstance(ids, list):
        raise ValueError("ids 必须是论文 ID 列表")
    if not 1 <= len(ids) <= 5000:
        raise ValueError("每次批量删除需选择 1 到 5000 篇论文")
    paper_ids = list(
        dict.fromkeys(
            _services().clean_text(item, 200)
            for item in ids
            if _services().clean_text(item, 200)
        )
    )
    if not paper_ids:
        raise ValueError("请至少选择一篇论文")

    removed = []
    protected = []
    skipped = []
    attachment_files = []
    now = _services().now_iso()
    with _services().DB_LOCK, _services().db_session() as db:
        marks = ",".join("?" for _ in paper_ids)
        rows = db.execute(
            f"SELECT id,title,is_favorite FROM papers WHERE id IN ({marks})", paper_ids
        ).fetchall()
        by_id = {row["id"]: row for row in rows}
        for paper_id in paper_ids:
            row = by_id.get(paper_id)
            if not row:
                skipped.append(paper_id)
                continue
            if protect_favorites and bool(row["is_favorite"]):
                protected.append(paper_id)
                continue
            attachment_files.extend(
                dict(item)
                for item in db.execute(
                    "SELECT id,stored_name,sha256 FROM attachments WHERE paper_id=?",
                    (paper_id,),
                ).fetchall()
            )
            # These tables intentionally keep long-lived research history but
            # must not retain a live link to a paper that no longer exists.
            db.execute(
                "UPDATE discovery_candidates SET imported_paper_id='' WHERE imported_paper_id=?",
                (paper_id,),
            )
            db.execute(
                "UPDATE research_goal_papers SET local_paper_id='' WHERE local_paper_id=?",
                (paper_id,),
            )
            db.execute("DELETE FROM research_claims WHERE paper_id=?", (paper_id,))
            db.execute("DELETE FROM evidence_spans WHERE paper_id=?", (paper_id,))
            db.execute(
                "UPDATE ai_jobs SET cancel_requested=1,status='cancelled',stage='cancelled',message='所属论文已删除',updated_at=?,completed_at=? WHERE paper_id=? AND status IN ('queued','running')",
                (now, now, paper_id),
            )
            db.execute(
                "UPDATE semantic_index_jobs SET cancel_requested=1,status='cancelled',stage='cancelled',message='所属论文已删除',updated_at=?,completed_at=? WHERE paper_id=? AND status IN ('queued','running')",
                (now, now, paper_id),
            )
            cur = db.execute("DELETE FROM papers WHERE id=?", (paper_id,))
            if cur.rowcount:
                removed.append({"id": paper_id, "title": row["title"]})
        if removed:
            _services().record_audit(
                db,
                "papers.deleted",
                "paper",
                "",
                {
                    "deleted": len(removed),
                    "protected_favorites": len(protected),
                    "skipped": len(skipped),
                    "protect_favorites": bool(protect_favorites),
                },
            )

    # Files are outside SQLite's transaction. Remove only unreferenced files;
    # a failed cleanup is harmless and can be reported by maintenance status.
    for attachment in attachment_files:
        stored_name = _services().Path(str(attachment.get("stored_name", ""))).name
        if not stored_name:
            continue
        try:
            with _services().DB_LOCK, _services().db_session() as db:
                still_used = db.execute(
                    "SELECT 1 FROM attachments WHERE stored_name=? LIMIT 1",
                    (stored_name,),
                ).fetchone()
            if still_used:
                continue
            target = _services().active_files_dir() / stored_name
            if target.exists() and target.is_file():
                target.unlink()
            digest = _services().clean_text(attachment.get("sha256", ""), 64)
            prefixes = {
                digest,
                _services().clean_text(attachment.get("id", ""), 100),
            } - {""}
            if _services().active_visuals_dir().exists():
                for prefix in prefixes:
                    for image in (
                        _services().active_visuals_dir().glob(prefix + "-*.jpg")
                    ):
                        try:
                            image.unlink()
                        except OSError:
                            pass
        except OSError:
            # Keep the database deletion successful; diagnostics can expose
            # any orphaned file for a later maintenance cleanup.
            continue
    _services().invalidate_derived_cache()
    return {
        "deleted": len(removed),
        "deleted_paper_ids": [item["id"] for item in removed],
        "protected": protected,
        "skipped": skipped,
        "attachments_considered": len(attachment_files),
    }


def attach_source(
    db,
    paper_id: str,
    source: str,
    external_id: str = "",
    query_id: str = "",
    raw: dict | None = None,
):
    db.execute(
        """INSERT INTO source_records(paper_id,source,external_id,query_id,fetched_at,raw_json)
                  VALUES(?,?,?,?,?,?)
                  ON CONFLICT(paper_id,source,external_id,query_id) DO UPDATE SET fetched_at=excluded.fetched_at,raw_json=excluded.raw_json""",
        (
            paper_id,
            source,
            external_id,
            query_id,
            _services().now_iso(),
            _services().json.dumps(raw or {}, ensure_ascii=False)[:60000],
        ),
    )


def upsert_paper(
    record: dict,
    source: str = "manual",
    external_id: str = "",
    query_id: str = "",
    raw: dict | None = None,
) -> tuple[str, bool]:
    p = _services().normalize_record(record)
    if not p["title"]:
        raise ValueError("论文标题不能为空")
    with _services().DB_LOCK, _services().db_session() as db:
        found = None
        if p["doi"]:
            found = db.execute(
                "SELECT * FROM papers WHERE doi=? LIMIT 1", (p["doi"],)
            ).fetchone()
        if not found and p["arxiv_id"]:
            found = db.execute(
                "SELECT * FROM papers WHERE arxiv_id=? LIMIT 1", (p["arxiv_id"],)
            ).fetchone()
        # Exact normalized title + year is a conservative duplicate guard only when a strong identifier is absent.
        if not found and not p["doi"] and not p["arxiv_id"] and p["year"]:
            found = db.execute(
                "SELECT * FROM papers WHERE title_key=? AND year=? LIMIT 1",
                (p["title_key"], p["year"]),
            ).fetchone()
            if not found:
                # Provider subtitles and punctuation can differ while the
                # work has no DOI/arXiv ID yet.  Require a very high title
                # overlap and the same publication year before merging.
                for candidate in db.execute(
                    "SELECT * FROM papers WHERE year=? ORDER BY updated_at DESC LIMIT 200",
                    (p["year"],),
                ).fetchall():
                    if (
                        _services().title_similarity(p["title"], candidate["title"])
                        >= 0.92
                    ):
                        found = candidate
                        break
        if found:
            pid = found["id"]
            # Fill gaps in bibliographic fields without overwriting user notes/status/topic.
            for field in (
                "authors",
                "abstract",
                "doi",
                "arxiv_id",
                "venue",
                "url",
                "pdf_url",
            ):
                if p[field] and not found[field]:
                    db.execute(
                        f"UPDATE papers SET {field}=?,updated_at=? WHERE id=?",
                        (p[field], _services().now_iso(), pid),
                    )
            _services().attach_source(db, pid, source, external_id, query_id, raw)
            _services().record_audit(
                db,
                "paper.merged",
                "paper",
                pid,
                {"source": source, "query_id": query_id, "external_id": external_id},
            )
            _services().invalidate_derived_cache()
            return pid, False
        pid = str(_services().uuid.uuid4())
        cols = ["id", *p.keys(), "created_at", "updated_at"]
        vals = [pid, *p.values(), _services().now_iso(), _services().now_iso()]
        db.execute(
            f"INSERT INTO papers({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",
            vals,
        )
        _services().attach_source(
            db, pid, source, external_id or p["doi"] or p["arxiv_id"], query_id, raw
        )
        _services().record_audit(
            db, "paper.created", "paper", pid, {"source": source, "query_id": query_id}
        )
        _services().invalidate_derived_cache()
        return pid, True


def http_json(url: str, timeout=20) -> dict:
    """Fetch a public JSON endpoint with a small transient-error budget.

    Academic indexes are independent of the model request budget.  A single
    502 or socket reset should not make an otherwise healthy multi-source
    search report a permanent source failure, while 4xx responses remain
    visible quickly.  A 429 opens a short host-level circuit so parallel
    expansion lanes do not immediately repeat the same rate-limited request.
    """
    # Serialize only within this host.  The outer search still fans out across
    # independent sources, but a burst of variants cannot defeat OpenAlex's
    # rate limiter before the first response records a cooldown.
    with _services()._public_source_request_lock(url):
        transient_retries = 0
        timeout, retry_budget = _services()._public_source_request_policy(timeout)
        remaining = _services()._public_source_cooldown_remaining(url)
        if remaining:
            raise _services().PublicSourceCooldownError(
                f"{_services()._public_source_key(url)} 暂时限流，约 {max(1, round(remaining))} 秒后重试"
            )
        request_url = url
        request_headers = {
            "User-Agent": _services().USER_AGENT,
            "Accept": "application/json",
        }
        host = _services()._public_source_key(url)
        if host == "api.openalex.org":
            api_key = _services().os.environ.get("LIT_OPENALEX_API_KEY", "").strip()
            existing_keys = (
                _services()
                .parse_qs(_services().urlparse(url).query, keep_blank_values=True)
                .get("api_key", [])
            )
            if api_key and not any(existing_keys):
                request_url += (
                    "&" if "?" in request_url else "?"
                ) + _services().urlencode({"api_key": api_key})
        elif host == "api.semanticscholar.org":
            api_key = (
                _services().os.environ.get("LIT_SEMANTIC_SCHOLAR_API_KEY", "").strip()
            )
            if api_key:
                request_headers["x-api-key"] = api_key
        for _attempt in range(retry_budget + 1):
            req = _services().Request(request_url, headers=request_headers)
            try:
                with _services().urlopen(req, timeout=timeout) as response:
                    return _services().json.loads(
                        response.read().decode("utf-8", "replace")
                    )
            except _services().HTTPError as exc:
                if exc.code == 429:
                    cooldown = _services()._public_retry_after(exc.headers)
                    _services()._cooldown_public_source(url, cooldown)
                    raise
                if exc.code in {401, 403}:
                    # Authentication/robots responses are not fixed by retrying
                    # every query variant. Keep the source visible as unavailable
                    # while allowing the other scholarly indexes to proceed.
                    _services()._cooldown_public_source(
                        url, max(60.0, _services().PUBLIC_SOURCE_COOLDOWN_SECONDS * 4)
                    )
                    raise
                if (
                    exc.code in {408, 425, 500, 502, 503, 504, 524}
                    and transient_retries < retry_budget
                ):
                    _services().time.sleep(
                        min(
                            _services()._model_retry_delay(
                                transient_retries, exc.headers
                            ),
                            2.0,
                        )
                    )
                    transient_retries += 1
                    continue
                raise
            except (_services().URLError, TimeoutError, ConnectionError, OSError):
                if transient_retries >= retry_budget:
                    raise
                _services().time.sleep(
                    min(_services()._model_retry_delay(transient_retries), 2.0)
                )
                transient_retries += 1
            except (
                _services().json.JSONDecodeError,
                UnicodeDecodeError,
                ValueError,
            ) as exc:
                # Empty/HTML responses are common during provider maintenance.
                # Retry once, then preserve a source-level diagnostic instead of
                # letting a parser traceback escape from the worker.
                if transient_retries < retry_budget:
                    _services().time.sleep(
                        min(_services()._model_retry_delay(transient_retries), 1.0)
                    )
                    transient_retries += 1
                    continue
                _services()._cooldown_public_source(
                    url, _services().PUBLIC_SOURCE_COOLDOWN_SECONDS
                )
                raise ValueError(
                    f"{_services()._public_source_key(url)} 返回了非 JSON 内容"
                ) from exc
        raise RuntimeError(f"{_services()._public_source_key(url)} 请求重试次数已用尽")


def http_bytes(url: str, timeout: int | None = None, accept: str = "*/*") -> bytes:
    """Fetch bounded public-source bytes with the same cooldown policy."""
    if timeout is None:
        timeout = _services().PUBLIC_SOURCE_TIMEOUT
    with _services()._public_source_request_lock(url):
        timeout, retry_budget = _services()._public_source_request_policy(timeout)
        remaining = _services()._public_source_cooldown_remaining(url)
        if remaining:
            raise _services().PublicSourceCooldownError(
                f"{_services()._public_source_key(url)} 暂时限流，约 {max(1, round(remaining))} 秒后重试"
            )
        transient_retries = 0
        for _attempt in range(retry_budget + 1):
            req = _services().Request(
                url, headers={"User-Agent": _services().USER_AGENT, "Accept": accept}
            )
            try:
                with _services().urlopen(req, timeout=timeout) as response:
                    return response.read()
            except _services().HTTPError as exc:
                if exc.code == 429:
                    _services()._cooldown_public_source(
                        url, _services()._public_retry_after(exc.headers)
                    )
                    raise
                if exc.code in {401, 403}:
                    _services()._cooldown_public_source(
                        url, max(60.0, _services().PUBLIC_SOURCE_COOLDOWN_SECONDS * 4)
                    )
                    raise
                if (
                    exc.code in {408, 425, 500, 502, 503, 504, 524}
                    and transient_retries < retry_budget
                ):
                    _services().time.sleep(
                        min(
                            _services()._model_retry_delay(
                                transient_retries, exc.headers
                            ),
                            2.0,
                        )
                    )
                    transient_retries += 1
                    continue
                raise
            except (_services().URLError, TimeoutError, ConnectionError, OSError):
                if transient_retries >= retry_budget:
                    raise
                _services().time.sleep(
                    min(_services()._model_retry_delay(transient_retries), 2.0)
                )
                transient_retries += 1
        raise RuntimeError(f"{_services()._public_source_key(url)} 请求重试次数已用尽")


def _openalex_work_reference(
    paper: dict, *, local_paper_id: str = ""
) -> tuple[str, str]:
    """Return a safe OpenAlex lookup URL and the associated library paper ID."""
    doi = _services().norm_doi(paper.get("doi", ""))
    openalex_id = _services().clean_text(
        paper.get("openalex_id") or paper.get("external_id") or "", 300
    )
    if openalex_id.startswith("https://openalex.org/"):
        openalex_id = openalex_id.rstrip("/").split("/")[-1]
    if openalex_id and _services().re.fullmatch(r"W\d+", openalex_id, _services().re.I):
        return (
            "https://api.openalex.org/works/" + _services().quote(openalex_id, safe=""),
            local_paper_id,
        )
    if doi:
        return (
            "https://api.openalex.org/works/https://doi.org/"
            + _services().quote(doi, safe=""),
            local_paper_id,
        )
    raise ValueError("该论文没有 DOI 或 OpenAlex 标识，暂时无法扩展引用关系")


def _openalex_relation_candidate(item: dict, relation: str) -> dict:
    inverted = item.get("abstract_inverted_index") or {}
    words = []
    for word, positions in inverted.items():
        for position in positions or []:
            try:
                words.append((int(position), word))
            except (TypeError, ValueError):
                continue
    primary = item.get("primary_location") or {}
    return {
        "relation": relation,
        "openalex_id": item.get("id", ""),
        "title": _services().clean_text(item.get("title", ""), 1000),
        "authors": ", ".join(
            (entry.get("author") or {}).get("display_name", "")
            for entry in (item.get("authorships") or [])[:12]
        ),
        "year": item.get("publication_year"),
        "abstract": " ".join(word for _, word in sorted(words)),
        "doi": _services().norm_doi(item.get("doi", "")),
        "venue": ((primary.get("source") or {}).get("display_name", "")),
        "url": item.get("doi") or item.get("id", ""),
        "pdf_url": (primary.get("pdf") or {}).get("url", "") or "",
        "source": "openalex",
        "external_id": item.get("id", "") or _services().norm_doi(item.get("doi", "")),
        "type": _services().clean_text(item.get("type", "journal-article"), 100),
        "citation_count": int(item.get("cited_by_count") or 0),
    }


def _expand_openalex_relation_work(
    work_url: str,
    limit: int = 24,
    *,
    persist: bool = False,
    paper_id: str = "",
    request_timeout: float = 15,
    include_cited_by: bool = True,
) -> dict:
    """Fetch a bounded citation neighborhood using one batched work-details request.

    ``include_cited_by`` and ``request_timeout`` are explicit because Agent
    runs must remain responsive.  The forward citation endpoint is useful but
    optional; references and related works from the primary work response are
    enough to keep a bounded discovery lane alive when that endpoint is slow.
    Interactive expansion keeps the complete default behavior.
    """
    limit = min(max(int(limit), 1), 60)
    try:
        timeout = max(2.0, min(float(request_timeout), 30.0))
    except (TypeError, ValueError):
        timeout = 15.0
    work = _services().http_json(work_url, timeout=timeout)
    if not work.get("id") and work.get("results"):
        work = work["results"][0]
    if not work.get("id"):
        raise ValueError("OpenAlex 没有解析到这篇论文")
    work_id = str(work.get("id", "")).rstrip("/").split("/")[-1]
    relation_by_id = {}
    for relation, ids in (
        ("引用", work.get("referenced_works") or []),
        ("相似", work.get("related_works") or []),
    ):
        for target in ids:
            key = str(target).rstrip("/").split("/")[-1]
            if key and key != work_id:
                relation_by_id.setdefault(key, relation)
    cited_url = work.get("cited_by_api_url", "")
    if include_cited_by and cited_url:
        try:
            cited = _services().http_json(
                cited_url
                + ("&" if "?" in cited_url else "?")
                + _services().urlencode(
                    {"per-page": min(limit, 25), "sort": "publication_date:desc"}
                ),
                timeout=timeout,
            )
            for item in cited.get("results", []):
                key = str(item.get("id", "")).rstrip("/").split("/")[-1]
                if key and key != work_id:
                    relation_by_id.setdefault(key, "被引用")
        except (
            _services().HTTPError,
            _services().URLError,
            TimeoutError,
            OSError,
            ValueError,
        ):
            # Citation-forward data is optional; references and related works remain useful.
            pass

    selected_ids = list(relation_by_id)[:limit]
    records_by_id = {}
    if selected_ids:
        batch_url = "https://api.openalex.org/works?" + _services().urlencode(
            {
                "filter": "ids.openalex:" + "|".join(selected_ids),
                "per-page": len(selected_ids),
            }
        )
        try:
            batch = _services().http_json(batch_url, timeout=timeout)
            records_by_id = {
                str(item.get("id", "")).rstrip("/").split("/")[-1]: item
                for item in batch.get("results", [])
                if item.get("id")
            }
            if not records_by_id:
                raise ValueError("OpenAlex 批量接口返回空结果")
        except (
            _services().HTTPError,
            _services().URLError,
            TimeoutError,
            OSError,
            ValueError,
        ):
            # Keep a bounded compatibility path for API proxies that reject OR filters.
            def fetch_one(key):
                try:
                    return key, _services().http_json(
                        "https://api.openalex.org/works/"
                        + _services().quote(key, safe=""),
                        timeout=max(2.0, min(timeout, 8.0)),
                    )
                except (
                    _services().HTTPError,
                    _services().URLError,
                    TimeoutError,
                    OSError,
                    ValueError,
                ):
                    return key, None

            with _services().ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="openalex-details"
            ) as pool:
                for key, item in pool.map(fetch_one, selected_ids):
                    if item:
                        records_by_id[key] = item

    candidates = []
    for key in selected_ids:
        item = records_by_id.get(key)
        if not item or not _services().clean_text(item.get("title", ""), 1000):
            continue
        candidates.append(
            _services()._openalex_relation_candidate(item, relation_by_id[key])
        )
    created = duplicates = 0
    if persist:
        for record in candidates:
            _, is_new = _services().upsert_paper(
                record,
                source="openalex",
                external_id=record.get("external_id", ""),
                raw={"relation": record["relation"], "seed_paper_id": paper_id},
            )
            created += int(is_new)
            duplicates += int(not is_new)
        with _services().DB_LOCK, _services().db_session() as db:
            _services().record_audit(
                db,
                "discovery.expanded",
                "paper",
                paper_id,
                {
                    "candidates": len(candidates),
                    "created": created,
                    "duplicates": duplicates,
                },
            )
    return {
        "paper_id": paper_id,
        "provider": "OpenAlex",
        "candidates": candidates,
        "created": created,
        "duplicates": duplicates,
        "count": len(candidates),
        "persisted": bool(persist),
        "expanded_at": _services().now_iso(),
    }


def sync_openalex_references(paper_id: str) -> dict:
    with _services().DB_LOCK, _services().db_session() as db:
        paper = db.execute(
            "SELECT id,title,doi,arxiv_id FROM papers WHERE id=?", (paper_id,)
        ).fetchone()
    if not paper:
        raise ValueError("论文不存在")
    if not paper["doi"]:
        raise ValueError("该论文还没有 DOI；目前 OpenAlex 引用扩展需要 DOI")
    work_url = "https://api.openalex.org/works/https://doi.org/" + _services().quote(
        paper["doi"], safe=""
    )
    work = _services().http_json(work_url, timeout=25)
    referenced = work.get("referenced_works") or []
    observed = _services().now_iso()
    with _services().DB_LOCK, _services().db_session() as db:
        for target in referenced:
            external_id = _services().clean_text(target, 300)
            if not external_id:
                continue
            db.execute(
                """INSERT INTO citation_edges(source_paper_id,target_external_id,provider,discovered_at,last_seen_at,raw_json)
                VALUES(?,?,?,?,?,?) ON CONFLICT(source_paper_id,target_external_id,provider) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
                (paper_id, external_id, "openalex", observed, observed, "{}"),
            )
        rows = [
            dict(r)
            for r in db.execute(
                "SELECT target_external_id,provider,discovered_at,last_seen_at FROM citation_edges WHERE source_paper_id=? ORDER BY target_external_id",
                (paper_id,),
            )
        ]
    return {
        "paper_id": paper_id,
        "provider": "OpenAlex",
        "count": len(rows),
        "references": rows,
        "synced_at": observed,
    }


def expand_openalex_relations(
    paper_id: str,
    limit: int = 24,
    *,
    persist: bool = True,
    request_timeout: float = 15,
    include_cited_by: bool = True,
) -> dict:
    """Discover backward, forward and semantically related works from OpenAlex.

    This is an explicit user action because citation expansion can make several
    network requests.  New works enter the normal inbox and are still subject
    to the same conservative deduplication rules as keyword search.
    """
    limit = min(max(int(limit), 1), 60)
    with _services().DB_LOCK, _services().db_session() as db:
        paper = db.execute(
            "SELECT id,title,doi,arxiv_id FROM papers WHERE id=?", (paper_id,)
        ).fetchone()
        source_id = db.execute(
            "SELECT external_id FROM source_records WHERE paper_id=? AND source='openalex' AND external_id<>'' ORDER BY fetched_at DESC LIMIT 1",
            (paper_id,),
        ).fetchone()
    if not paper:
        raise ValueError("论文不存在")
    if paper["doi"]:
        work_url = (
            "https://api.openalex.org/works/https://doi.org/"
            + _services().quote(paper["doi"], safe="")
        )
    elif source_id and str(source_id[0]).strip():
        work_url = "https://api.openalex.org/works/" + _services().quote(
            str(source_id[0]).rstrip("/").split("/")[-1], safe=""
        )
    else:
        raise ValueError("该论文没有 DOI 或 OpenAlex 标识，暂时无法扩展引用关系")
    return _services()._expand_openalex_relation_work(
        work_url,
        limit=limit,
        persist=persist,
        paper_id=paper_id,
        request_timeout=request_timeout,
        include_cited_by=include_cited_by,
    )


__all__ = [
    "norm_doi",
    "norm_arxiv",
    "title_key",
    "relevance_tokens",
    "expanded_relevance_terms",
    "search_plan_terms",
    "extract_search_facets",
    "_bridge_query_terms",
    "_bridge_non_latin_terms",
    "search_query_variants",
    "candidate_identity",
    "candidate_identity_variants",
    "record_discovery_search_memory",
    "discovery_intent_key",
    "_json_object",
    "_discovery_feedback_by_key",
    "_discovery_feedback_examples",
    "title_similarity",
    "merge_search_records",
    "relevance_label",
    "relevance_token_weight",
    "score_relevance",
    "_model_web_has_object_task_evidence",
    "filter_model_web_quality",
    "filter_agent_candidate_quality",
    "clean_text",
    "compact_search_raw",
    "compact_candidate_record",
    "normalize_record",
    "paper_pdf_candidates",
    "download_public_pdf",
    "paper_text",
    "text_overlap",
    "connection_tier",
    "_openalex_work_id",
    "_openalex_seed_ids",
    "_semantic_scholar_seed_ids",
    "_semantic_recommendation_record",
    "_load_semantic_scholar_recommendations",
    "_load_openalex_recommendations",
    "_merge_recommendation_provider_results",
    "_refresh_openalex_recommendations",
    "_openalex_recommendation_snapshot",
    "_available_openalex_recommendations",
    "save_openalex_recommendation",
    "research_memory",
    "_research_memory",
    "paper_connections",
    "paper_dict",
    "_hydrate_retrieval_metadata",
    "paper_detail",
    "refresh_search_content",
    "rebuild_search_index",
    "search_library",
    "maintenance_status",
    "get_backup_policy",
    "save_backup_policy",
    "prune_backup_archives",
    "create_backup_archive",
    "scheduled_backup_tick",
    "paper_history",
    "reading_card_dict",
    "record_paper_revision",
    "save_reading_card",
    "batch_update_papers",
    "delete_papers",
    "attach_source",
    "upsert_paper",
    "http_json",
    "http_bytes",
    "_openalex_work_reference",
    "_openalex_relation_candidate",
    "_expand_openalex_relation_work",
    "sync_openalex_references",
    "expand_openalex_relations",
]
