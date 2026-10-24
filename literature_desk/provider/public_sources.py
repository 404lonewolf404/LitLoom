"""Connectors for public academic indexes and web retrieval services."""

from __future__ import annotations

from ..services import app_services as _services



def search_crossref(query: str, limit: int, offset: int = 0) -> list[dict]:
    direct_doi = (
        _services().norm_doi(query)
        if _services().re.match(
            r"(?:https?://(?:dx\.)?doi\.org/|doi:\s*)?10\.\d{4,9}/\S+",
            str(query).strip(),
            _services().re.I,
        )
        else ""
    )
    if direct_doi:
        url = "https://api.crossref.org/works/" + _services().quote(direct_doi, safe="")
    else:
        url = "https://api.crossref.org/works?" + _services().urlencode(
            {
                "query.bibliographic": query,
                "rows": min(limit, 50),
                "offset": max(0, offset),
                "select": "DOI,title,author,published,container-title,URL,abstract,type",
            }
        )
    data = _services().http_json(url)
    if direct_doi:
        data = {"message": {"items": [data.get("message", {})]}}
    out = []
    for item in data.get("message", {}).get("items", []):
        title = (item.get("title") or [""])[0]
        if not title:
            continue
        title_norm = _services().title_key(title)
        if _services().re.search(
            r"(?:\.pdf|_supp\d|supplementary|supplement|session details|table of contents|front matter)",
            title.lower(),
        ):
            continue
        if item.get("type") not in {
            "journal-article",
            "proceedings-article",
            "posted-content",
            "book-chapter",
            "report",
        }:
            continue
        query_terms = (
            []
            if direct_doi
            else [
                term
                for term in _services().re.findall(r"[a-z0-9]{3,}", query.lower())
                if term
                not in {
                    "and",
                    "or",
                    "the",
                    "for",
                    "with",
                    "from",
                    "via",
                    "using",
                    "based",
                    "model",
                    "models",
                }
            ]
        )
        title_hits = sum(1 for term in set(query_terms) if term in title_norm)
        abstract_text = (
            _services().re.sub(r"<[^>]+>", " ", item.get("abstract", "")).lower()
        )
        abstract_hits = sum(1 for term in set(query_terms) if term in abstract_text)
        if (
            query_terms
            and not title_hits
            and abstract_hits < min(2, len(set(query_terms)))
        ):
            continue
        date_parts = item.get("published", {}).get("date-parts", [[]])
        year = date_parts[0][0] if date_parts and date_parts[0] else None
        authors = ", ".join(
            " ".join(filter(None, [a.get("given", ""), a.get("family", "")]))
            for a in item.get("author", [])[:12]
        )
        out.append(
            {
                "title": title,
                "authors": authors,
                "year": year,
                "abstract": abstract_text,
                "doi": item.get("DOI", ""),
                "venue": (item.get("container-title") or [""])[0],
                "url": item.get("URL", ""),
                "source": "crossref",
                "external_id": item.get("DOI", ""),
                "type": item.get("type", ""),
                "citation_count": item.get("is-referenced-by-count", 0),
                "open_access": bool((item.get("link") or [])),
            }
        )
    return out


def search_arxiv(query: str, limit: int, offset: int = 0) -> list[dict]:
    # Use the official Atom API. Plain phrases are quoted; advanced syntax can be supplied as an arXiv query.
    arxiv_query = query.strip()
    direct_arxiv = (
        _services().norm_arxiv(arxiv_query)
        if _services().re.match(
            r"(?:https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/)?\d{4}\.\d{4,5}(?:v\d+)?$",
            arxiv_query,
            _services().re.I,
        )
        else ""
    )
    if direct_arxiv:
        arxiv_query = "id_list:" + direct_arxiv
    elif not _services().re.search(
        r"\b(AND|OR|ANDNOT)\b|\b(all|ti|au|abs|cat):", arxiv_query, _services().re.I
    ):
        arxiv_query = f'all:"{arxiv_query}"'
    url = "https://export.arxiv.org/api/query?" + _services().urlencode(
        {
            "search_query": arxiv_query,
            "start": max(0, offset),
            "max_results": min(limit, 50),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
    )
    body = _services().http_bytes(
        url,
        timeout=_services().PUBLIC_SOURCE_TIMEOUT,
        accept="application/atom+xml, application/xml;q=0.9, */*;q=0.1",
    )
    root = _services().ET.fromstring(body)
    ns = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    out = []
    for entry in root.findall("a:entry", ns):
        title = " ".join((entry.findtext("a:title", default="", namespaces=ns)).split())
        if not title:
            continue
        link = entry.find("a:id", ns)
        abs_url = link.text.strip() if link is not None and link.text else ""
        arxiv_external_id = abs_url.rsplit("/", 1)[-1]
        arxiv_id = _services().norm_arxiv(arxiv_external_id)
        authors = ", ".join(
            a.findtext("a:name", default="", namespaces=ns)
            for a in entry.findall("a:author", ns)
        )
        published = entry.findtext("a:published", default="", namespaces=ns)
        pdf = next(
            (
                x.get("href", "")
                for x in entry.findall("a:link", ns)
                if x.get("title") == "pdf"
            ),
            "",
        )
        out.append(
            {
                "title": title,
                "authors": authors,
                "year": int(published[:4]) if published[:4].isdigit() else None,
                "abstract": " ".join(
                    entry.findtext("a:summary", default="", namespaces=ns).split()
                ),
                "arxiv_id": arxiv_id,
                "arxiv_version": arxiv_external_id,
                "venue": "arXiv preprint",
                "url": abs_url,
                "pdf_url": pdf
                or ("https://arxiv.org/pdf/" + _services().quote(arxiv_external_id)),
                "source": "arxiv",
                "external_id": arxiv_external_id,
                "type": "preprint",
                "open_access": True,
            }
        )
    return out


def search_openalex(query: str, limit: int, offset: int = 0) -> list[dict]:
    """Search OpenAlex for broad, date-sorted scholarly coverage.

    OpenAlex is used as a metadata connector only. Its work records often
    include DOI, venue and an inverted abstract index, which we normalize to
    the same paper shape as the other connectors.
    """
    page = (max(0, offset) // max(1, limit)) + 1
    url = "https://api.openalex.org/works?" + _services().urlencode(
        {
            "search": _services().clean_text(query, 500),
            "per-page": min(max(int(limit), 1), 50),
            "page": page,
            "sort": "publication_date:desc",
            "mailto": "local@example.invalid",
        }
    )
    data = _services().http_json(url)
    out = []
    for item in data.get("results", []):
        title = _services().clean_text(item.get("title", ""), 1000)
        if not title:
            continue
        inverted = item.get("abstract_inverted_index") or {}
        abstract_words = []
        for word, positions in inverted.items():
            for position in positions or []:
                abstract_words.append((int(position), word))
        abstract = " ".join(word for _, word in sorted(abstract_words))
        authors = ", ".join(
            (author.get("author") or {}).get("display_name", "")
            for author in (item.get("authorships") or [])[:12]
            if (author.get("author") or {}).get("display_name")
        )
        primary = item.get("primary_location") or {}
        source = primary.get("source") or {}
        year = item.get("publication_year")
        doi = _services().norm_doi(item.get("doi", ""))
        external_id = item.get("id", "") or doi
        out.append(
            {
                "title": title,
                "authors": authors,
                "year": year,
                "abstract": abstract,
                "doi": doi,
                "venue": source.get("display_name", "") or "",
                "url": item.get("doi") or item.get("id", ""),
                "pdf_url": (primary.get("pdf") or {}).get("url", "") or "",
                "source": "openalex",
                "external_id": external_id,
                "type": _services().clean_text(item.get("type", ""), 100),
                "citation_count": int(item.get("cited_by_count") or 0),
                "open_access": bool(
                    (primary.get("is_oa") or False)
                    or (primary.get("pdf") or {}).get("url")
                ),
                "is_retracted": bool(item.get("is_retracted")),
            }
        )
    return out


def search_europepmc(query: str, limit: int, offset: int = 0) -> list[dict]:
    """Search Europe PMC's stable REST API for biomedical and life-science work."""
    page_size = min(max(int(limit), 1), 100)
    page = max(1, (max(0, int(offset)) // page_size) + 1)
    url = (
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search?"
        + _services().urlencode(
            {
                "query": _services().clean_text(query, 500),
                "format": "json",
                "resultType": "core",
                "pageSize": page_size,
                "page": page,
                "sort": "CITED desc",
            }
        )
    )
    data = _services().http_json(url, timeout=25)
    out = []
    for item in (data.get("resultList") or {}).get("result", []):
        title = _services().clean_text(item.get("title", ""), 1000)
        if not title:
            continue
        year = item.get("pubYear") or (
            item.get("firstPublicationDate", "")[:4]
            if item.get("firstPublicationDate")
            else None
        )
        try:
            year = int(year) if year else None
        except (TypeError, ValueError):
            year = None
        external_id = _services().clean_text(item.get("id", ""), 300)
        pmid = _services().clean_text(item.get("pmid", ""), 100)
        pmcid = _services().clean_text(item.get("pmcid", ""), 100)
        doi = _services().norm_doi(item.get("doi", ""))
        article_url = (
            f"https://europepmc.org/article/{item.get('source', 'MED')}/{external_id}"
            if external_id
            else "https://europepmc.org/"
        )
        pdf_url = f"https://europepmc.org/articles/{pmcid}?pdf=render" if pmcid else ""
        out.append(
            {
                "title": title,
                "authors": _services().clean_text(item.get("authorString", ""), 4000),
                "year": year,
                "abstract": _services().clean_text(item.get("abstractText", ""), 30000),
                "doi": doi,
                "venue": _services().clean_text(item.get("journalTitle", ""), 1000),
                "url": article_url,
                "pdf_url": pdf_url,
                "source": "europepmc",
                "external_id": external_id or doi or pmid,
                "type": "journal-article",
                "open_access": bool(item.get("isOpenAccess") == "Y" or pmcid),
                "pmid": pmid,
                "pmcid": pmcid,
                "citation_count": int(item.get("citedByCount") or 0),
            }
        )
    return out


def search_semanticscholar(query: str, limit: int, offset: int = 0) -> list[dict]:
    """Search Semantic Scholar when its public quota is available.

    The connector is deliberately optional: Semantic Scholar can return 429
    without an API key.  The federated run records that source as failed while
    preserving results from every other connector.
    """
    fields = "title,authors,year,abstract,externalIds,venue,url,openAccessPdf,publicationTypes"
    url = (
        "https://api.semanticscholar.org/graph/v1/paper/search?"
        + _services().urlencode(
            {
                "query": _services().clean_text(query, 500),
                "limit": min(max(int(limit), 1), 100),
                "offset": max(0, int(offset)),
                "fields": fields,
            }
        )
    )
    data = _services().http_json(url, timeout=_services().PUBLIC_SOURCE_TIMEOUT)
    out = []
    for item in data.get("data", []):
        title = _services().clean_text(item.get("title", ""), 1000)
        if not title:
            continue
        external = item.get("externalIds") or {}
        doi = _services().norm_doi(external.get("DOI", ""))
        arxiv_id = _services().norm_arxiv(external.get("ArXiv", ""))
        open_pdf = item.get("openAccessPdf") or {}
        out.append(
            {
                "title": title,
                "authors": ", ".join(
                    (author or {}).get("name", "")
                    for author in (item.get("authors") or [])[:12]
                ),
                "year": item.get("year"),
                "abstract": _services().clean_text(item.get("abstract", ""), 30000),
                "doi": doi,
                "arxiv_id": arxiv_id,
                "venue": _services().clean_text(item.get("venue", ""), 1000),
                "url": item.get("url", "")
                or (
                    f"https://www.semanticscholar.org/paper/{item.get('paperId', '')}"
                    if item.get("paperId")
                    else ""
                ),
                "pdf_url": open_pdf.get("url", ""),
                "source": "semanticscholar",
                "external_id": item.get("paperId", "") or doi or arxiv_id,
                "type": ((item.get("publicationTypes") or [""])[0] or "").lower(),
                "open_access": bool(open_pdf.get("url")),
                "citation_count": int(item.get("citationCount") or 0),
            }
        )
    return out


def search_dblp(query: str, limit: int, offset: int = 0) -> list[dict]:
    """Search DBLP's keyless computer-science bibliography API."""
    url = "https://dblp.org/search/publ/api?" + _services().urlencode(
        {
            "q": _services().clean_text(query, 500),
            "h": min(max(int(limit), 1), 100),
            "f": max(0, int(offset)),
            "format": "json",
        }
    )
    data = _services().http_json(url, timeout=25)
    hits = ((data.get("result") or {}).get("hits") or {}).get("hit", [])
    if isinstance(hits, dict):
        hits = [hits]
    out = []
    for hit in hits:
        info = hit.get("info") or {}
        title = _services().clean_text(info.get("title", ""), 1000).rstrip(".")
        if not title:
            continue
        authors = info.get("authors") or {}
        author_items = (
            authors.get("author", []) if isinstance(authors, dict) else authors
        )
        if isinstance(author_items, dict):
            author_items = [author_items]
        author_names = []
        for author in author_items or []:
            if isinstance(author, dict):
                author_names.append(
                    _services().clean_text(
                        author.get("text") or author.get("name") or "", 200
                    )
                )
            else:
                author_names.append(_services().clean_text(author, 200))
        doi = _services().norm_doi(info.get("doi", ""))
        year = info.get("year")
        try:
            year = int(year) if year else None
        except (TypeError, ValueError):
            year = None
        entry_type = _services().clean_text(info.get("type", ""), 200).lower()
        out.append(
            {
                "title": title,
                "authors": ", ".join(name for name in author_names if name),
                "year": year,
                "abstract": "",
                "doi": doi,
                "venue": _services().clean_text(info.get("venue", ""), 1000),
                "url": _services().clean_text(
                    info.get("ee") or info.get("url") or "", 3000
                ),
                "source": "dblp",
                "external_id": _services().clean_text(
                    info.get("key") or info.get("url") or doi, 500
                ),
                "type": entry_type,
                "open_access": False,
            }
        )
    return out


def _openreview_value(content: dict, key: str, default=""):
    value = (content or {}).get(key, default)
    if isinstance(value, dict):
        return value.get("value", default)
    return value


def search_openreview(query: str, limit: int, offset: int = 0) -> list[dict]:
    """Search OpenReview notes for current conference and workshop papers.

    OpenReview is intentionally optional.  Its public API changes more often
    than the archival indexes, so an unavailable endpoint is surfaced as a
    source-level error while the other connectors continue normally.
    """
    page_size = min(max(int(limit), 1), 100)
    params = {
        "limit": page_size,
        "offset": max(0, int(offset)),
        "sort": "tmdate:desc",
        "content.title": _services().clean_text(query, 500),
    }
    url = "https://api2.openreview.net/notes?" + _services().urlencode(params)
    data = _services().http_json(url, timeout=25)
    notes = data.get("notes") or data.get("results") or []
    out = []
    for note in notes:
        content = note.get("content") or {}
        title = _services().clean_text(
            _services()._openreview_value(content, "title"), 1000
        )
        if not title:
            continue
        abstract = _services().clean_text(
            _services()._openreview_value(content, "abstract"), 30000
        )
        authors = _services()._openreview_value(content, "authors", [])
        if isinstance(authors, str):
            authors = [authors]
        author_names = [
            _services().clean_text(author, 200)
            for author in (authors or [])
            if _services().clean_text(author, 200)
        ]
        timestamp = note.get("pdate") or note.get("cdate") or note.get("tmdate")
        try:
            year = (
                int(
                    _services()
                    .datetime.fromtimestamp(
                        float(timestamp) / 1000, tz=_services().timezone.utc
                    )
                    .year
                )
                if timestamp
                else None
            )
        except (TypeError, ValueError, OSError, OverflowError):
            year = None
        forum = _services().clean_text(note.get("forum") or note.get("id") or "", 300)
        invitation = _services().clean_text(note.get("invitation") or "", 500)
        note_id = _services().clean_text(note.get("id") or forum, 500)
        url = (
            "https://openreview.net/forum?id="
            + _services().quote(forum or note_id, safe="")
            if (forum or note_id)
            else "https://openreview.net/"
        )
        out.append(
            {
                "title": title,
                "authors": ", ".join(author_names[:12]),
                "year": year,
                "abstract": abstract,
                "venue": _services().clean_text(
                    _services()._openreview_value(content, "venue")
                    or invitation.rsplit("/", 1)[-1],
                    1000,
                ),
                "url": url,
                "source": "openreview",
                "external_id": note_id,
                "type": "conference-paper",
                "open_access": True,
                "openreview_id": note_id,
            }
        )
    return out


def _tavily_api_key() -> str:
    """Read an optional Tavily key from an explicit local file.

    The key is intentionally kept outside LitLoom's database and is never
    returned in diagnostics.  ``LIT_TAVILY_API_KEY_FILE`` is the preferred
    override; for this local-first workspace we also accept a sibling
    ``tavily.txt`` so upgrades do not require re-entering the key.
    """
    configured = _services().os.environ.get("LIT_TAVILY_API_KEY_FILE", "").strip()
    candidates = [_services().Path(configured).expanduser()] if configured else []
    candidates.extend(
        [_services().ROOT / "tavily.txt", _services().ROOT.parent / "tavily.txt"]
    )
    for path in candidates:
        if not path:
            continue
        try:
            value = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            continue
        if value and not value.startswith("#"):
            # Accept either a bare key or KEY=value without exposing it.
            if "=" in value and value.split("=", 1)[0].strip().upper() in {
                "TAVILY_API_KEY",
                "LIT_TAVILY_API_KEY",
            }:
                value = value.split("=", 1)[1].strip().strip("'\"")
            if value:
                return value
    return _services().os.environ.get("LIT_TAVILY_API_KEY", "").strip()


def _tavily_title_similarity(left: str, right: str) -> float:
    """Cheap, deterministic title check used before accepting web metadata."""
    a = {
        term
        for term in _services().re.findall(r"[a-z0-9]{3,}", _services().title_key(left))
        if term not in _services().REL_STOPWORDS
    }
    b = {
        term
        for term in _services().re.findall(
            r"[a-z0-9]{3,}", _services().title_key(right)
        )
        if term not in _services().REL_STOPWORDS
    }
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _tavily_extract_identifiers(url: str) -> tuple[str, str]:
    value = _services().unquote(url or "")
    doi_match = _services().re.search(
        r"(?:doi\.org/|doi[:/])((?:10)\.\d{4,9}/[^\s?#]+)", value, _services().re.I
    )
    # Tavily frequently returns arXiv's HTML reader URLs.  Treat abs/pdf/html
    # equally and strip a trailing version in norm_arxiv so all representations
    # map to one stable work identity.
    arxiv_match = _services().re.search(
        r"arxiv\.org/(?:abs|pdf|html)/([0-9]{4}\.\d{4,5}(?:v\d+)?)",
        value,
        _services().re.I,
    )
    return (
        _services().norm_doi(doi_match.group(1)) if doi_match else "",
        _services().norm_arxiv(arxiv_match.group(1)) if arxiv_match else "",
    )


def _verify_tavily_result(
    item: dict, *, source: str = "tavily", score_field: str = "score"
) -> dict:
    """Attach canonical academic metadata when a web hit can be verified.

    A web hit is still useful for discovery, but it remains explicitly
    ``unverified`` when neither an arXiv record nor a matching Crossref/
    OpenAlex work can be found.  This keeps it visible to the AI reviewer
    without silently treating a blog, repository or news page as a paper.
    """
    title = _services().clean_text(item.get("title"), 800)
    url = _services()._model_search_url(item.get("url"))
    doi, arxiv_id = _services()._tavily_extract_identifiers(url)
    canonical = None
    verification_sources = []
    if arxiv_id:
        try:
            rows = _services().search_arxiv(arxiv_id, 1)
            canonical = rows[0] if rows else None
            if canonical:
                verification_sources.append("arxiv")
        except Exception:
            pass
    if doi and canonical is None:
        try:
            rows = _services().search_crossref(doi, 1)
            canonical = rows[0] if rows else None
            if canonical:
                verification_sources.append("crossref")
        except Exception:
            pass
    if canonical is None and title:
        # One bounded title lookup is enough to catch publisher URLs without
        # making Tavily discovery fan out into an unbounded crawler.
        for source_name, searcher in (
            ("crossref", _services().search_crossref),
            ("openalex", _services().search_openalex),
        ):
            try:
                rows = searcher(title, 3)
            except Exception:
                rows = []
            match = next(
                (
                    row
                    for row in rows
                    if _services()._tavily_title_similarity(title, row.get("title", ""))
                    >= 0.5
                ),
                None,
            )
            if match:
                canonical = match
                verification_sources.append(source_name)
                break
    record = dict(item)
    record["source"] = source
    record["url"] = url
    record["external_id"] = url
    record["verification_status"] = "verified" if canonical else "unverified"
    record["verification_sources"] = verification_sources
    record["discovery_only"] = not bool(canonical)
    score = float(item.get(score_field) or item.get("score") or 0)
    if source == "tavily":
        record["tavily_score"] = score
    else:
        record["discovery_score"] = score
    if canonical:
        # Canonical fields win; retain Tavily evidence as a provider source.
        for field in (
            "title",
            "authors",
            "year",
            "abstract",
            "doi",
            "arxiv_id",
            "venue",
            "pdf_url",
            "type",
            "open_access",
            "citation_count",
        ):
            if canonical.get(field) not in (None, "", []):
                record[field] = canonical[field]
        # A title match verifies the paper identity, not the Tavily result URL.
        # Prefer the paper's stable scholarly identifier so a search snippet
        # that points to an unrelated conference page cannot become the paper
        # link shown to the user.
        canonical_doi = _services().norm_doi(canonical.get("doi") or "")
        canonical_arxiv_id = _services().norm_arxiv(canonical.get("arxiv_id") or "")
        canonical_url = _services()._model_search_url(canonical.get("url"))
        if canonical_doi:
            canonical_url = f"https://doi.org/{canonical_doi}"
        elif canonical_arxiv_id:
            canonical_url = f"https://arxiv.org/abs/{canonical_arxiv_id}"
        if canonical_url and canonical_url != url:
            record["discovery_url"] = url
            record["url"] = canonical_url
        record["external_id"] = (
            canonical.get("external_id")
            or canonical.get("doi")
            or canonical.get("arxiv_id")
            or url
        )
        record["academic_source"] = canonical.get("source", "")
    return record


def search_tavily(query: str, limit: int, offset: int = 0) -> list[dict]:
    """Discover academic candidates through Tavily, then verify their identity."""
    key = _services()._tavily_api_key()
    if not key:
        raise ValueError(
            "Tavily 未配置；可设置 LIT_TAVILY_API_KEY_FILE 或放置 tavily.txt"
        )
    tavily_url = "https://api.tavily.com/search"
    remaining = _services()._public_source_cooldown_remaining(tavily_url)
    if remaining:
        raise _services().PublicSourceCooldownError(
            f"api.tavily.com 暂时限流，约 {max(1, round(remaining))} 秒后重试"
        )
    # Model-led discovery already fans out across several academic sources.
    # Tavily's advanced crawler is valuable for a deliberate web search, but
    # it adds several seconds to every tool turn and is unnecessary when the
    # model is only collecting paper leads. Use the lighter index in that
    # bounded lane; the normal user-facing Tavily search keeps advanced mode.
    fast_search = bool(getattr(_services().MODEL_SEARCH_FAST_SOURCE, "enabled", False))
    payload = {
        "api_key": key,
        "query": _services().clean_text(query, 500),
        "search_depth": "basic" if fast_search else "advanced",
        "topic": "general",
        "max_results": min(max(int(limit), 1), 10),
        "include_answer": False,
    }
    request = _services().Request(
        tavily_url,
        data=_services().json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _services().USER_AGENT,
        },
        method="POST",
    )
    data = None
    tavily_timeout, tavily_retry_budget = _services()._public_source_request_policy(
        _services().TAVILY_TIMEOUT
    )
    for attempt in range(tavily_retry_budget + 1):
        try:
            with _services().urlopen(request, timeout=tavily_timeout) as response:
                data = _services().json.loads(
                    response.read().decode("utf-8", "replace")
                )
            break
        except _services().HTTPError as exc:
            if attempt < tavily_retry_budget and exc.code in {
                408,
                425,
                429,
                500,
                502,
                503,
                504,
            }:
                if exc.code == 429:
                    _services()._cooldown_public_source(
                        tavily_url, _services()._public_retry_after(exc.headers)
                    )
                    raise
                _services().time.sleep(0.35)
                continue
            if exc.code in {401, 403}:
                _services()._cooldown_public_source(
                    tavily_url,
                    max(60.0, _services().PUBLIC_SOURCE_COOLDOWN_SECONDS * 4),
                )
            raise ValueError(f"Tavily 外部发现请求失败（HTTP {exc.code}）") from exc
        except (
            _services().URLError,
            TimeoutError,
            OSError,
            _services().json.JSONDecodeError,
        ) as exc:
            if attempt < tavily_retry_budget:
                _services().time.sleep(0.35)
                continue
            raise ValueError("Tavily 外部发现暂时失败，请稍后重试") from exc
    if not isinstance(data, dict):
        raise ValueError("Tavily 返回了无法识别的结果")
    rows, seen = [], set()
    # Build the verification queue first, then fan out the bounded title
    # lookups.  The previous implementation verified every Tavily result in
    # the response loop, which made Crossref/OpenAlex calls strictly serial
    # and dominated search latency.  Results are assembled in original order
    # after the pool completes so ranking and deduplication remain stable.
    prepared = []
    verify_queue = []
    title_only_verified = 0
    title_only_verify_limit = (
        1 if fast_search else _services().TAVILY_VERIFY_MAX_TITLE_ONLY
    )
    for item in (data.get("results") or [])[max(0, int(offset)) :]:
        if not isinstance(item, dict) or not _services()._model_search_url(
            item.get("url")
        ):
            continue
        compact = {
            "title": _services().clean_text(item.get("title"), 800),
            "url": item.get("url"),
            "abstract": _services().clean_text(item.get("content"), 8000),
            "published_date": _services().clean_text(item.get("published_date"), 40),
            "score": item.get("score"),
        }
        cache_key = (
            _services().clean_text(compact.get("url"), 1200)
            or _services().clean_text(compact.get("title"), 800)
        ).casefold()
        with _services().TAVILY_VERIFY_CACHE_LOCK:
            cached = (
                dict(_services().TAVILY_VERIFY_CACHE.get(cache_key) or {})
                if cache_key
                else {}
            )
        doi, arxiv_id = _services()._tavily_extract_identifiers(compact.get("url", ""))
        if cached:
            prepared.append((compact, cache_key, cached))
        elif (doi or arxiv_id) or title_only_verified < title_only_verify_limit:
            # Identifier-bearing hits are always eligible; title-only hits are
            # bounded to protect Crossref/OpenAlex and keep Tavily useful as a
            # discovery source even when the result set is large.
            verify_queue.append((len(prepared), compact, cache_key))
            prepared.append((compact, cache_key, None))
            if not doi and not arxiv_id:
                title_only_verified += 1
        else:
            # Keep the web lead visible as an explicitly unverified discovery
            # item without paying another academic-index lookup.
            verified = dict(compact)
            verified.update(
                {
                    "source": "tavily",
                    "url": _services()._model_search_url(compact.get("url")),
                    "external_id": _services()._model_search_url(compact.get("url")),
                    "verification_status": "unverified",
                    "verification_sources": [],
                    "discovery_only": True,
                    "tavily_score": float(compact.get("score") or 0),
                }
            )
            prepared.append((compact, cache_key, verified))

    if verify_queue:
        worker_count = min(4, len(verify_queue))
        with _services().ThreadPoolExecutor(
            max_workers=worker_count, thread_name_prefix="tavily-verify"
        ) as pool:
            # The source fast-budget flag is thread-local.  Tavily verification
            # runs in a nested pool, so pass the same bounded/no-retry policy
            # explicitly; otherwise title-only verification silently falls
            # back to the normal 12-second timeout and can hold a model search
            # for a minute when public indexes are rate-limited.
            def verify_fast(compact):
                with _services().fast_model_search_source_budget():
                    return _services()._verify_tavily_result(compact)

            future_map = {
                pool.submit(verify_fast, compact): (index, cache_key)
                for index, compact, cache_key in verify_queue
            }
            for future in _services().as_completed(future_map):
                index, cache_key = future_map[future]
                try:
                    verified = future.result()
                except Exception:
                    # A failed identity lookup must not discard the web lead;
                    # retain it as discovery-only and let the caller surface
                    # partial-source diagnostics where applicable.
                    compact = prepared[index][0]
                    verified = dict(compact)
                    verified.update(
                        {
                            "source": "tavily",
                            "url": _services()._model_search_url(compact.get("url")),
                            "external_id": _services()._model_search_url(
                                compact.get("url")
                            ),
                            "verification_status": "unverified",
                            "verification_sources": [],
                            "discovery_only": True,
                            "tavily_score": float(compact.get("score") or 0),
                        }
                    )
                if cache_key:
                    with _services().TAVILY_VERIFY_CACHE_LOCK:
                        if len(_services().TAVILY_VERIFY_CACHE) >= 512:
                            _services().TAVILY_VERIFY_CACHE.pop(
                                next(iter(_services().TAVILY_VERIFY_CACHE)), None
                            )
                        _services().TAVILY_VERIFY_CACHE[cache_key] = dict(verified)
                compact, _, _ = prepared[index]
                prepared[index] = (compact, cache_key, verified)

    for compact, cache_key, verified in prepared:
        if not verified:
            continue
        identity = _services().candidate_identity(verified)
        if identity in seen:
            continue
        seen.add(identity)
        rows.append(verified)
    return rows


def _deepseek_search_endpoint(kind: str) -> str:
    """Resolve DeepSeek Search/Fetch without borrowing the chat endpoint."""
    settings = _services().model_settings()
    if not settings.get("deepseek_search_enabled"):
        return ""
    configured = settings.get(
        "deepseek_search_api_url" if kind == "search" else "deepseek_fetch_api_url"
    )
    if configured:
        return _services()._model_search_url(configured)
    host = (_services().urlparse(settings.get("base_url") or "").hostname or "").casefold()
    if host == "api.deepseek.com":
        return (
            _services().DEEPSEEK_SEARCH_DEFAULT_URL
            if kind == "search"
            else _services().DEEPSEEK_FETCH_DEFAULT_URL
        )
    return ""


def deepseek_search_available() -> bool:
    """Whether the configured DeepSeek account can use its web tools."""
    return bool(
        _services().model_settings().get("deepseek_search_enabled")
        and _services().model_api_key()
        and _deepseek_search_endpoint("search")
    )


def model_search_available_connectors() -> dict:
    """Expose only search connectors that have their required credentials."""
    connectors = dict(_services().MODEL_SEARCH_CONNECTORS)
    if not _services()._tavily_api_key():
        connectors.pop("tavily", None)
    if not _services().deepseek_search_available():
        connectors.pop("deepseek_search", None)
    return connectors


class DeepSeekResponseTooLargeError(ValueError):
    """A provider response exceeded the local memory bound."""


def _deepseek_tool_post(
    endpoint: str, payload: dict, *, tool_name: str = "Search"
) -> dict:
    """Call the read-only DeepSeek web tool with bounded retries."""
    key = _services().model_api_key()
    if not key:
        raise ValueError(f"DeepSeek {tool_name} 需要已配置的模型 API Key")
    if not endpoint:
        raise ValueError(f"DeepSeek {tool_name} 未启用或没有可用的 API 地址")
    with _services()._public_source_request_lock(endpoint):
        remaining = _services()._public_source_cooldown_remaining(endpoint)
        if remaining:
            raise _services().PublicSourceCooldownError(
                f"{_services()._public_source_key(endpoint)} 暂时限流，约 {max(1, round(remaining))} 秒后重试"
            )
        timeout, retry_budget = _services()._public_source_request_policy(
            _services().MODEL_SEARCH_SOURCE_TIMEOUT
        )
        for attempt in range(retry_budget + 1):
            request = _services().Request(
                endpoint,
                data=_services().json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Authorization": "Bearer " + key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": _services().USER_AGENT,
                },
                method="POST",
            )
            try:
                with _services().urlopen(request, timeout=timeout) as response:
                    try:
                        raw = response.read(2_000_001)
                    except TypeError:
                        raw = response.read()
                if len(raw) > 2_000_000:
                    raise _services().DeepSeekResponseTooLargeError(
                        f"DeepSeek {tool_name} 返回内容超过 2 MB"
                    )
                result = _services().json.loads(raw.decode("utf-8", "replace"))
                if not isinstance(result, dict):
                    raise ValueError(f"DeepSeek {tool_name} 返回格式无法识别")
                return result
            except _services().HTTPError as exc:
                if exc.code == 429:
                    _services()._cooldown_public_source(endpoint, _services()._public_retry_after(exc.headers))
                    raise _services().PublicSourceCooldownError(
                        f"DeepSeek {tool_name} 触发限流，请稍后重试"
                    ) from exc
                if exc.code in {408, 425, 500, 502, 503, 504, 524} and attempt < retry_budget:
                    _services().time.sleep(min(_services()._model_retry_delay(attempt, exc.headers), 2.0))
                    continue
                if exc.code in {401, 403}:
                    _services()._cooldown_public_source(endpoint, max(60.0, _services().PUBLIC_SOURCE_COOLDOWN_SECONDS * 4))
                raise ValueError(f"DeepSeek {tool_name} 请求失败（HTTP {exc.code}）") from exc
            except (_services().URLError, TimeoutError, ConnectionError, OSError) as exc:
                if attempt < retry_budget:
                    _services().time.sleep(min(_services()._model_retry_delay(attempt), 2.0))
                    continue
                raise ValueError(f"DeepSeek {tool_name} 网络请求失败") from exc
            except _services().DeepSeekResponseTooLargeError as exc:
                raise ValueError(str(exc)) from exc
            except (_services().json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                if attempt < retry_budget:
                    _services().time.sleep(min(_services()._model_retry_delay(attempt), 1.0))
                    continue
                raise ValueError(f"DeepSeek {tool_name} 返回了无效 JSON") from exc
    raise ValueError(f"DeepSeek {tool_name} 请求重试次数已用尽")


def _compat_deepseek_tool_post(
    endpoint: str, payload: dict, *, tool_name: str = "Search"
) -> dict:
    """Resolve the legacy facade hook so existing integrations can patch it."""
    try:
        from .. import providers as facade

        callback = getattr(facade, "_deepseek_tool_post", _deepseek_tool_post)
    except (ImportError, AttributeError):
        callback = _deepseek_tool_post
    return callback(endpoint, payload, tool_name=tool_name)


def search_deepseek(query: str, limit: int, offset: int = 0) -> list[dict]:
    """Search live web pages through DeepSeek, then verify paper identity.

    DeepSeek Search is a discovery source rather than an academic index. Every
    hit is therefore sent through the same DOI/arXiv/title verification path
    used by Tavily, and unverifiable pages stay explicitly discovery-only.
    """
    offset = max(0, int(offset))
    requested_limit = min(max(int(limit), 1), 50)
    endpoint = _deepseek_search_endpoint("search")
    count = min(requested_limit + offset, 50)
    data = _compat_deepseek_tool_post(
        endpoint,
        {
            "query": _services().clean_text(query, 128),
            "count": count,
            "need_content": False,
            "ori_question": _services().clean_text(query, 128),
        },
    )
    rows, prepared, verify_queue = [], [], []
    title_only_verified = 0
    for item in (data.get("results") or [])[offset : offset + requested_limit]:
        if not isinstance(item, dict):
            continue
        url = _services()._model_search_url(item.get("url"))
        title = _services().clean_text(item.get("title"), 800)
        if not url or not title:
            continue
        try:
            score = float(item.get("rerank_score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        candidate = {
            "title": title,
            "url": url,
            "abstract": _services().clean_text(item.get("summary") or item.get("snippet"), 8000),
            "published_date": _services().clean_text(item.get("date"), 40),
            "score": score,
        }
        doi, arxiv_id = _services()._tavily_extract_identifiers(url)
        if doi or arxiv_id or title_only_verified < _services().TAVILY_VERIFY_MAX_TITLE_ONLY:
            verify_queue.append((len(prepared), candidate))
            prepared.append(None)
            if not doi and not arxiv_id:
                title_only_verified += 1
        else:
            prepared.append(
                {
                    **candidate,
                    "source": "deepseek_search",
                    "external_id": url,
                    "verification_status": "unverified",
                    "verification_sources": [],
                    "discovery_only": True,
                    "discovery_score": score,
                }
            )

    if verify_queue:
        def verify_fast(candidate):
            with _services().fast_model_search_source_budget():
                return _services()._verify_tavily_result(
                    candidate, source="deepseek_search", score_field="score"
                )

        with _services().ThreadPoolExecutor(
            max_workers=min(4, len(verify_queue)),
            thread_name_prefix="deepseek-verify",
        ) as pool:
            future_map = {
                pool.submit(verify_fast, candidate): index
                for index, candidate in verify_queue
            }
            for future in _services().as_completed(future_map):
                index = future_map[future]
                try:
                    verified = future.result()
                except Exception:
                    candidate = next(
                        candidate for item_index, candidate in verify_queue
                        if item_index == index
                    )
                    verified = {
                        **candidate,
                        "source": "deepseek_search",
                        "external_id": candidate["url"],
                        "verification_status": "unverified",
                        "verification_sources": [],
                        "discovery_only": True,
                    }
                verified.pop("tavily_score", None)
                verified["source"] = "deepseek_search"
                verified["discovery_provider"] = "DeepSeek Search"
                verified["discovery_score"] = float(verified.get("score") or 0)
                prepared[index] = verified

    seen = set()
    for verified in prepared:
        if not verified:
            continue
        verified.setdefault("discovery_provider", "DeepSeek Search")
        identity = _services().candidate_identity(verified)
        if identity in seen:
            continue
        seen.add(identity)
        rows.append(verified)
    return rows


def fetch_deepseek_page(url: str, max_chars: int = 12000) -> dict:
    """Fetch one public page as Markdown through DeepSeek Fetch."""
    endpoint = _deepseek_search_endpoint("fetch")
    public_url = _services()._safe_public_web_url(url)
    data = _compat_deepseek_tool_post(
        endpoint, {"url": public_url}, tool_name="Fetch"
    )
    content = _services().clean_text(
        data.get("content") or "", max(500, min(int(max_chars), 20000))
    )
    return {
        "status": "completed" if content else "empty",
        "url": public_url,
        "title": _services().clean_text(data.get("title") or "", 800),
        "content": content,
        "provider": "DeepSeek Fetch",
        "source": "deepseek_fetch",
        "retrieved_at": _services().now_iso(),
        "evidence_scope": "external_web_page",
    }


def _safe_public_web_url(value: object) -> str:
    """Accept only public HTTP(S) URLs for the Agent's read-only browser tool."""
    raw = _services().clean_text(value, 2400)
    parsed = _services().urlparse(raw)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("网页地址必须是公开的 HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("网页地址不能包含账号或密码")
    host = parsed.hostname.casefold().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("出于安全原因不能访问本机或局域网地址")
    try:
        address = _services().ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
    ):
        raise ValueError("出于安全原因不能访问私有或保留 IP 地址")
    return raw


def browse_public_web(urls: list[str], max_chars: int = 12000) -> dict:
    """Extract bounded public web pages through Tavily's maintained extractor.

    This deliberately does not implement a local crawler. Tavily handles page
    rendering and boilerplate removal; LitLoom only validates URLs, bounds the
    request, normalizes the response, and preserves provider provenance.
    """
    key = _services()._tavily_api_key()
    if not key:
        raise ValueError(
            "网页浏览需要 Tavily Key；可设置 LIT_TAVILY_API_KEY_FILE 或放置 tavily.txt"
        )
    normalized = []
    for value in urls if isinstance(urls, list) else []:
        try:
            url = _services()._safe_public_web_url(value)
        except ValueError:
            continue
        if url not in normalized:
            normalized.append(url)
    normalized = normalized[:3]
    if not normalized:
        raise ValueError("请提供 1 到 3 个公开网页地址")
    endpoint = "https://api.tavily.com/extract"
    remaining = _services()._public_source_cooldown_remaining(endpoint)
    if remaining:
        raise _services().PublicSourceCooldownError(
            f"api.tavily.com 暂时限流，约 {max(1, round(remaining))} 秒后重试"
        )
    payload = {
        "api_key": key,
        "urls": normalized,
        "extract_depth": "basic",
        "include_images": False,
    }
    request = _services().Request(
        endpoint,
        data=_services().json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _services().USER_AGENT,
        },
        method="POST",
    )
    data = None
    timeout, retry_budget = _services()._public_source_request_policy(
        _services().TAVILY_TIMEOUT
    )
    for attempt in range(retry_budget + 1):
        try:
            with _services().urlopen(request, timeout=timeout) as response:
                data = _services().json.loads(
                    response.read().decode("utf-8", "replace")
                )
            break
        except _services().HTTPError as exc:
            if exc.code == 429:
                _services()._cooldown_public_source(
                    endpoint, _services()._public_retry_after(exc.headers)
                )
                raise _services().PublicSourceCooldownError(
                    "Tavily 网页提取触发限流，请稍后重试"
                ) from exc
            if attempt < retry_budget and exc.code in {408, 425, 500, 502, 503, 504}:
                _services().time.sleep(
                    min(_services()._model_retry_delay(attempt, exc.headers), 2.0)
                )
                continue
            if exc.code in {401, 403}:
                _services()._cooldown_public_source(
                    endpoint, max(60.0, _services().PUBLIC_SOURCE_COOLDOWN_SECONDS * 4)
                )
            raise ValueError(f"Tavily 网页提取失败（HTTP {exc.code}）") from exc
        except (
            _services().URLError,
            TimeoutError,
            OSError,
            _services().json.JSONDecodeError,
        ) as exc:
            if attempt < retry_budget:
                _services().time.sleep(
                    min(_services()._model_retry_delay(attempt), 2.0)
                )
                continue
            raise ValueError("Tavily 网页提取暂时失败，请稍后重试") from exc
    if not isinstance(data, dict):
        raise ValueError("Tavily 网页提取返回了无法识别的结果")
    rows = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        url = _services()._safe_public_web_url(item.get("url"))
        content = _services().clean_text(
            item.get("raw_content") or item.get("content") or "",
            max(500, min(int(max_chars), 20000)),
        )
        if not content:
            continue
        rows.append(
            {
                "url": url,
                "title": _services().clean_text(item.get("title") or "", 800),
                "content": content,
                "provider": "Tavily Extract",
                "source": "tavily_extract",
                "retrieved_at": _services().now_iso(),
                "evidence_scope": "external_web_page",
            }
        )
    failed = [
        _services().clean_text(
            item.get("url") if isinstance(item, dict) else item, 2400
        )
        for item in (data.get("failed_results") or [])
    ]
    return {
        "status": "completed" if rows else ("partial" if failed else "empty"),
        "pages": rows,
        "failed_urls": [item for item in failed if item],
        "provider": "Tavily Extract",
        "url_count": len(normalized),
    }




__all__ = ['search_crossref', 'search_arxiv', 'search_openalex', 'search_europepmc', 'search_semanticscholar', 'search_dblp', '_openreview_value', 'search_openreview', '_tavily_api_key', '_tavily_title_similarity', '_tavily_extract_identifiers', '_verify_tavily_result', 'search_tavily', 'search_deepseek', 'deepseek_search_available', 'model_search_available_connectors', 'DeepSeekResponseTooLargeError', 'fetch_deepseek_page', '_deepseek_tool_post', '_safe_public_web_url', 'browse_public_web']

