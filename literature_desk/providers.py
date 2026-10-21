"""Academic indexes, public web, and model-assisted retrieval providers."""

from __future__ import annotations

from .services import app_services as _services


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
    payload = {
        "api_key": key,
        "query": _services().clean_text(query, 500),
        "search_depth": "advanced",
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
            raise ValueError(f"Tavily 联网发现请求失败（HTTP {exc.code}）") from exc
        except (
            _services().URLError,
            TimeoutError,
            OSError,
            _services().json.JSONDecodeError,
        ) as exc:
            if attempt < tavily_retry_budget:
                _services().time.sleep(0.35)
                continue
            raise ValueError("Tavily 联网发现暂时失败，请稍后重试") from exc
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
        elif (
            doi or arxiv_id
        ) or title_only_verified < _services().TAVILY_VERIFY_MAX_TITLE_ONLY:
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
    data = _deepseek_tool_post(
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
    data = _deepseek_tool_post(
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


class ModelWebSearchUnsupported(ValueError):
    """The configured model endpoint does not expose a web-search tool."""


def _model_search_url(value: object) -> str:
    """Return a usable source URL without accepting model-invented schemes."""
    url = _services().clean_text(value, 1200).strip()
    parsed = _services().urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return url


def _model_search_year(value: object) -> int | None:
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return year if 1800 <= year <= 2200 else None


def normalize_model_search_results(
    raw: object, query: str, provider: str = "", model: str = ""
) -> dict:
    """Normalize provider/tool output into the same candidate contract as connectors.

    A model is allowed to return partial metadata, but a result without a
    title and a verifiable HTTP(S) source is discarded. This prevents a relay
    from turning a prose answer into fake papers or unusable candidates.
    """
    if not isinstance(raw, dict):
        raw = {}
    values = (
        raw.get("results")
        or raw.get("papers")
        or raw.get("items")
        or raw.get("references")
        or []
    )
    if isinstance(values, dict):
        values = list(values.values())
    if not isinstance(values, list):
        values = []
    results, seen = [], set()
    unrelated_filtered = 0
    for item in values:
        if not isinstance(item, dict):
            continue
        title = _services().clean_text(
            item.get("title") or item.get("paper_title") or item.get("name"), 600
        )
        url = _services()._model_search_url(
            item.get("url") or item.get("source_url") or item.get("link")
        )
        citations = (
            item.get("source_citations")
            or item.get("citations")
            or item.get("sources")
            or []
        )
        if isinstance(citations, str):
            citations = [citations]
        citations = (
            [
                _services()._model_search_url(value)
                for value in citations
                if _services()._model_search_url(value)
            ]
            if isinstance(citations, list)
            else []
        )
        if not url and citations:
            url = citations[0]
        doi = _services().norm_doi(item.get("doi") or "")
        arxiv_id = _services().norm_arxiv(
            item.get("arxiv_id") or item.get("arxiv") or ""
        )
        identity = (
            doi
            or arxiv_id
            or _services().clean_text(item.get("external_id") or "", 300)
            or _services().title_key(title)
        )
        if not title or not url or not identity or identity in seen:
            continue
        seen.add(identity)
        authors = item.get("authors") or item.get("author") or ""
        if isinstance(authors, list):
            authors = ", ".join(
                _services().clean_text(
                    author.get("name") if isinstance(author, dict) else author, 160
                )
                for author in authors[:20]
            )
        label = (
            _services()
            .clean_text(
                item.get("relation")
                or item.get("relevance_label")
                or item.get("category")
                or "uncertain",
                40,
            )
            .lower()
        )
        label = {
            "direct": "direct",
            "relevant": "direct",
            "high": "direct",
            "adjacent": "adjacent",
            "nearby": "adjacent",
            "unrelated": "unrelated",
        }.get(label, "uncertain")
        if label == "unrelated":
            unrelated_filtered += 1
            continue
        results.append(
            {
                "title": title,
                "authors": _services().clean_text(authors, 1200),
                "year": _services()._model_search_year(
                    item.get("year") or item.get("publication_year")
                ),
                "abstract": _services().clean_text(
                    item.get("abstract")
                    or item.get("summary")
                    or item.get("description"),
                    8000,
                ),
                "doi": doi,
                "arxiv_id": arxiv_id,
                "venue": _services().clean_text(
                    item.get("venue") or item.get("journal") or item.get("conference"),
                    500,
                ),
                "url": url,
                "pdf_url": _services()._model_search_url(
                    item.get("pdf_url") or item.get("pdf")
                ),
                "source": "model_web",
                "external_id": _services().clean_text(
                    item.get("external_id") or doi or arxiv_id or identity, 500
                ),
                "open_access": bool(item.get("open_access")),
                "citation_count": (
                    max(0, int(item.get("citation_count") or 0))
                    if str(item.get("citation_count") or "").strip().isdigit()
                    else 0
                ),
                "relevance_label": label,
                "relevance_reason": _services().clean_text(
                    item.get("relevance_reason") or item.get("reason") or "", 500
                ),
                "source_citations": citations,
                "source_attested": bool(citations or url),
                "verified": bool(item.get("verification_status") == "verified"),
                "verification_status": _services().clean_text(
                    item.get("verification_status")
                    or ("verified" if item.get("academic_source") else "unverified"),
                    40,
                ),
                "verification_sources": [
                    _services().clean_text(value, 80)
                    for value in (item.get("verification_sources") or [])
                    if _services().clean_text(value, 80)
                ],
                "discovery_only": bool(item.get("discovery_only")),
                "tavily_score": item.get("tavily_score", 0),
                "academic_source": _services().clean_text(
                    item.get("academic_source"), 80
                ),
                "model_provider": provider,
                "model": model,
                # Preserve the bounded local-fill audit marker.  These are real
                # connector candidates which matched the requested object/task
                # but were omitted from the model's short final ranking; the
                # later quality gate must keep them as explicit review items.
                "selection_backfilled": bool(
                    item.get("_selection_backfilled")
                    or item.get("selection_backfilled")
                ),
                # Durable history rows carry the result of the earlier quality
                # gate.  Preserve it through model normalization so a repeat
                # search does not discard accepted history during the next pass.
                "history_quality_accepted": bool(item.get("history_quality_accepted")),
            }
        )
    gaps = raw.get("gaps") or raw.get("limitations") or []
    next_queries = raw.get("next_queries") or raw.get("follow_up_queries") or []
    return {
        "results": results,
        "searches": (
            raw.get("searches") if isinstance(raw.get("searches"), list) else []
        ),
        "citations": (
            raw.get("citations") if isinstance(raw.get("citations"), list) else []
        ),
        "gaps": (
            [
                _services().clean_text(value, 500)
                for value in gaps
                if _services().clean_text(value, 500)
            ][:12]
            if isinstance(gaps, list)
            else []
        ),
        "next_queries": (
            [
                _services().clean_text(value, 500)
                for value in next_queries
                if _services().clean_text(value, 500)
            ][:12]
            if isinstance(next_queries, list)
            else []
        ),
        "provider": provider,
        "model": model,
        "warnings": [],
        "unrelated_results_filtered": unrelated_filtered,
        "ok": bool(results),
    }


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
                for page in range(1, 5):
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

    with _services().ThreadPoolExecutor(
        max_workers=min(3, len(selected_sources)),
        thread_name_prefix="model-search-source",
    ) as pool:
        return list(pool.map(fetch, selected_sources))


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


def _model_search_backfill_selection(
    selected: list[dict],
    inventory: dict,
    selected_ids: set[str],
    query: str,
    target_limit: int,
    options: dict,
) -> tuple[list[dict], int]:
    """Fill a short model selection from candidates the tools already returned.

    The model is asked to rank results, but compatible endpoints often return
    only the few papers they consider strongest.  That is a poor user
    experience when the requested page is larger, and it is especially
    confusing on a repeated topic: the model may omit both historical and
    newly found candidates after trying to avoid repeats.  We can safely fill
    the page from the *real tool inventory* because no metadata is invented;
    the local score only decides order and every filled row is marked as
    uncertain for review.  Strict fresh-only mode still excludes historical
    candidates.
    """
    if len(selected) >= target_limit or not inventory:
        return selected, 0
    facets = _services().extract_search_facets(query)
    has_object_task = bool(facets.get("对象") and facets.get("任务"))
    suppress_seen = bool(options.get("suppress_seen"))
    # The browser enables ``fill_repeats`` together with its normal
    # "prefer unseen" setting.  In that mode historical candidates are a
    # transparent fallback for missing slots; only an explicit strict fresh
    # search should remove them from the local model-tool refill pool.
    strict_fresh_only = suppress_seen and not bool(options.get("fill_repeats"))
    pool = []
    for candidate_id, candidate in inventory.items():
        candidate_id = _services().clean_text(candidate_id, 40)
        if not candidate_id or candidate_id in selected_ids:
            continue
        previously_seen = bool(candidate.get("_previously_seen"))
        if strict_fresh_only and previously_seen:
            continue
        # For structured natural-language questions, do not refill with a
        # method-only paper simply because the model returned it from a broad
        # source.  The normal model-web quality gate performs a second,
        # explainable check later; this first check keeps the queue useful.
        if has_object_task and not _services()._model_web_has_object_task_evidence(
            candidate, facets
        ):
            continue
        triage = _services().score_relevance(candidate, query, [], [])
        score = int(triage.get("score") or 0)
        # Generic keyword searches may have weak lexical overlap even when a
        # provider's semantic index found a useful item.  Natural-language
        # questions need a minimal signal before an automatic refill.
        if has_object_task and score < 12:
            continue
        pool.append(
            (
                previously_seen,
                score,
                int(candidate.get("citation_count") or 0),
                candidate_id,
                triage,
            )
        )
    pool.sort(key=lambda item: (item[0], -item[1], -item[2], item[3]))
    added = 0
    for previously_seen, score, _citations, candidate_id, triage in pool:
        if len(selected) >= target_limit:
            break
        candidate = inventory[candidate_id]
        selected_ids.add(candidate_id)
        label = "adjacent" if score >= 35 else "uncertain"
        origin = "历史候选" if previously_seen else "新候选"
        selected.append(
            {
                **candidate,
                "relevance_label": label,
                "relevance_reason": (
                    f"模型检索工具已返回；模型主筛选未列入，按本地相关性排序补位（{origin}，"
                    f"相关性分数 {score}），建议人工复核。"
                ),
                "source_citations": candidate.get("source_citations")
                or [candidate.get("url", "")],
                "_selection_backfilled": True,
            }
        )
        added += 1
    return selected, added


def _model_search_local_fill(
    selected: list[dict],
    inventory: dict,
    identity_index: dict,
    selected_ids: set[str],
    query: str,
    target_limit: int,
    options: dict,
    preferred_sources: list[str],
    seen_titles: set[str],
    seen_identities: set[str],
    executed_searches: list[dict],
) -> tuple[list[dict], int, int]:
    """Use bounded local connector calls to fill a short model result page.

    A model is useful for choosing search angles and ranking candidates, but a
    compatible endpoint may return only one or two IDs even after the tools
    found more work.  When that happens, run at most two *new* deterministic
    query variants against the user's selected academic connectors.  The
    connector records are real and are passed through the same identity,
    relevance and repeat logic; candidates not explicitly ranked by the model
    are marked as local-fill/uncertain by ``_model_search_backfill_selection``.
    This keeps result counts stable without inventing metadata or spending
    another model request.
    """
    if len(selected) >= target_limit:
        return selected, 0, 0
    try:
        safe_target = min(40, max(1, int(target_limit)))
    except (TypeError, ValueError):
        safe_target = 20
    # The model-web caller normally passes the user's selected connector set.
    # When it is empty, the model's tool calls below provide the authoritative
    # source list; that observed list is used by the bounded refill below.
    source_pool = [
        source
        for source in preferred_sources
        if source in _services().SOURCE_SEARCH and source != "model_web"
    ]
    # In AI-tool mode the user is allowed to leave the source list empty: the
    # model chooses academic connectors in its function calls.  The old local
    # refill treated that as "no sources" and returned immediately, so a
    # short model ranking (or one quality-gate rejection) could make the
    # visible page smaller solely because repeat avoidance was enabled.  Reuse
    # the actual sources observed in this run before falling back to the
    # explicitly preferred list.  Keep academic indexes first and Tavily last;
    # Tavily is useful for recall but its unverified rows should not displace
    # a source-backed paper during a bounded refill.
    if not source_pool:
        observed_sources = []
        for search in executed_searches:
            for summary in (
                search.get("sources", []) if isinstance(search, dict) else []
            ):
                source = summary.get("source") if isinstance(summary, dict) else ""
                if (
                    source in _services().SOURCE_SEARCH
                    and source != "model_web"
                    and source not in observed_sources
                ):
                    observed_sources.append(source)
        source_pool = [source for source in observed_sources if source != "tavily"]
        source_pool.extend(source for source in observed_sources if source == "tavily")
    source_pool = list(dict.fromkeys(source_pool))[
        : _services().MODEL_SEARCH_TOOL_MAX_SOURCES
    ]
    if not source_pool:
        return selected, 0, 0
    try:
        local_plan = _services().search_query_plan(
            query, source_pool, {**options, "adaptive_expansion": False}
        )
    except Exception:
        local_plan = {}
    variants = local_plan.get("query_variants") if isinstance(local_plan, dict) else []
    if not isinstance(variants, list):
        variants = []
    already_searched = {
        _services().clean_text(item.get("query"), 500).casefold()
        for item in executed_searches
        if isinstance(item, dict) and _services().clean_text(item.get("query"), 500)
    }
    local_queries = []
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        variant_query = _services().clean_text(variant.get("query"), 500)
        if not variant_query or variant_query.casefold() in already_searched:
            continue
        local_queries.append(variant_query)
        if len(local_queries) >= 2:
            break
    if not local_queries:
        fallback_query = _services().clean_text(query, 500)
        if not fallback_query or fallback_query.casefold() in already_searched:
            return selected, 0, 0
        local_queries = [fallback_query]

    arguments = [
        {
            "query": variant_query,
            "sources": source_pool,
            "limit": min(_services().MODEL_SEARCH_TOOL_MAX_LIMIT, max(6, safe_target)),
        }
        for variant_query in local_queries
    ]
    with _services().ThreadPoolExecutor(
        max_workers=min(2, len(arguments)), thread_name_prefix="model-search-local-fill"
    ) as pool:
        prefetched_batches = list(
            pool.map(
                lambda item: _services()._model_search_prefetch(
                    item,
                    source_pool,
                    seen_titles,
                    seen_identities,
                    options.get("avoid_repeats", True),
                ),
                arguments,
            )
        )

    for argument, prefetched in zip(arguments, prefetched_batches):
        if not prefetched:
            continue
        output = _services()._model_search_tool_result(
            argument,
            inventory,
            identity_index,
            seen_titles,
            seen_identities,
            options,
            source_pool,
            prefetched,
        )
        executed_searches.append(
            {
                "query": output.get("query") or argument["query"],
                "sources": output.get("source_results", []),
                "result_count": len(output.get("results", [])),
                "excluded_previously_seen": output.get("excluded_previously_seen", 0),
                "local_fill": True,
            }
        )
    before = len(selected)
    selected, added = _services()._model_search_backfill_selection(
        selected,
        inventory,
        selected_ids,
        query,
        safe_target,
        options,
    )
    return selected, added, max(0, len(selected) - before)


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
                early_stop_target = max(10, int(target_limit))
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


def _model_search_history_fill(
    intent_key: str,
    query: str,
    existing: list[dict],
    target_limit: int,
    options: dict | None = None,
) -> list[dict]:
    """Reuse durable candidates when a repeated model search has a short page.

    The model-led path can return a small pool when provider pages overlap or
    a relay ranks only the first few IDs.  The normal connector path already
    has continuation pages, but model search intentionally keeps the tool
    response compact.  Fill the remaining page from *previously visible*
    candidates for the same intent, rather than dropping rows merely because
    they were seen before.  These records remain review candidates and go
    through the same model-web quality gate later in ``run_query``.

    This helper never invents metadata, never imports anything into the
    library, and skips candidates that the user explicitly rejected.  Strict
    ``suppress_seen`` mode remains an explicit fresh-only request.
    """
    options = options or {}
    if (
        options.get("suppress_seen") and not options.get("fill_repeats")
    ) or not intent_key:
        return []
    try:
        target = min(100, max(1, int(target_limit)))
    except (TypeError, ValueError):
        target = 20
    # ``existing`` may contain the same work several times because it was
    # returned by multiple connectors or query lanes.  Comparing its raw
    # length with the requested page size used to make a repeat search look
    # full even when only a few unique papers were available, so the durable
    # history fallback never ran.  Count canonical identities instead; aliases
    # (DOI/arXiv/title) still count as one work.
    existing_unique_keys = {
        _services().candidate_identity(item)
        for item in existing
        if isinstance(item, dict) and _services().candidate_identity(item)
    }
    if len(existing_unique_keys) >= target:
        return []
    existing_keys = set()
    for item in existing:
        if isinstance(item, dict):
            existing_keys.update(_services().candidate_identity_variants(item))
    if not existing_keys:
        existing_keys = {
            _services().candidate_identity(item)
            for item in existing
            if isinstance(item, dict)
        }
    # Candidate rows are deliberately retained as the durable review history.
    # Persisted topic runs are also valid history: long-running saved topics
    # write to ``run_papers`` instead of ``discovery_candidates``.  Include
    # both stores so a repeated AI search cannot become short merely because
    # an earlier run used auto-import mode.  The bounded scan keeps this
    # fallback cheap even after years of searches.
    with _services().DB_LOCK, _services().db_session() as db:
        candidate_rows = db.execute(
            "SELECT id,record_json,created_at FROM discovery_candidates ORDER BY created_at DESC LIMIT 3000"
        ).fetchall()
        # Older candidate snapshots did not persist ``intent_key`` inside
        # record_json.  The search-memory table has always stored the
        # intent-scoped identity, so use it as a durable join key instead of
        # silently dropping those rows from a repeat-search refill.  Without
        # this bridge, a topic with enough historical papers could still
        # render a short page merely because its candidates came from an old
        # schema/version.
        memory_identity_rows = db.execute(
            "SELECT identity_key FROM discovery_search_memory WHERE intent_key=? AND identity_key<>'' LIMIT 5000",
            (intent_key,),
        ).fetchall()
        persisted_rows = db.execute(
            """SELECT p.*,rp.metadata_json AS run_metadata_json,
                      rs.plan_json AS run_plan_json,rs.summary_json AS run_summary_json,
                      rs.created_at AS run_created_at
               FROM run_papers rp
               JOIN papers p ON p.id=rp.paper_id
               JOIN run_snapshots rs ON rs.run_id=rp.run_id
               ORDER BY rs.created_at DESC LIMIT 3000"""
        ).fetchall()
    memory_identity_keys = {
        _services().clean_text(row[0], 700)
        for row in memory_identity_rows
        if _services().clean_text(row[0], 700)
    }
    rows = [
        {"kind": "candidate", "row": row, "created_at": row["created_at"]}
        for row in candidate_rows
    ]
    for row in persisted_rows:
        summary = _services()._json_object(row["run_summary_json"])
        plan = _services()._json_object(row["run_plan_json"])
        row_intent = _services().clean_text(
            summary.get("intent_key") or plan.get("discovery_intent_key"), 100
        )
        if row_intent == intent_key:
            rows.append(
                {"kind": "persisted", "row": row, "created_at": row["run_created_at"]}
            )
    rows.sort(
        key=lambda item: _services().clean_text(item.get("created_at"), 40),
        reverse=True,
    )
    rows = rows[:3000]
    candidates = []
    seen = set(existing_keys)
    facets = _services().extract_search_facets(query)
    for entry in rows:
        row = entry["row"]
        if entry["kind"] == "candidate":
            record = _services()._json_object(row["record_json"])
        else:
            # ``paper_dict`` preserves the normalized library metadata; run
            # metadata adds search-only provenance and prior review labels.
            record = _services().paper_dict(row)
            run_metadata = _services()._json_object(row["run_metadata_json"])
            for key, value in run_metadata.items():
                if key not in record or record.get(key) in (None, "", [], {}):
                    record[key] = value
            record["intent_key"] = intent_key
        # Older rows may contain recursive ``raw`` payloads created before
        # provenance was bounded.  Collapse them while the durable history is
        # being read so one bad row cannot amplify on every repeat search.
        record = _services().compact_candidate_record(record)
        if not isinstance(record, dict):
            continue
        record_intent = _services().clean_text(record.get("intent_key"), 100)
        if record_intent != intent_key:
            # Candidate records from before intent persistence can still be
            # safely reused when at least one canonical identity was observed
            # in the current intent's indexed memory.  Do not broaden this to
            # title-only matching: identity aliases (DOI, arXiv, normalized
            # title/year) are required to avoid cross-topic leakage.
            if record_intent or not (
                _services().candidate_identity_variants(record) & memory_identity_keys
            ):
                continue
            record["intent_key"] = intent_key
        if record.get("review_decision") in {"irrelevant", "ignored"} or record.get(
            "user_decision"
        ) in {"irrelevant", "ignored"}:
            continue
        title = _services().clean_text(record.get("title"), 600)
        url = _services()._model_search_url(
            record.get("url") or record.get("source_url") or record.get("link")
        )
        if not title or not url:
            continue
        if not _services().record_matches_search_options(record, options):
            continue
        identities = _services().candidate_identity_variants(record)
        if not identities or identities & seen:
            continue
        item = dict(record)
        previous_model_label = (
            _services()
            .clean_text(
                item.get("model_relevance_label") or item.get("relevance_label"), 40
            )
            .casefold()
        )
        # Older candidate snapshots predate ``history_quality_accepted``.
        # Their direct/adjacent model verdict is still an auditable acceptance
        # decision for this exact intent, so migrate it in memory on first
        # reuse.  Uncertain legacy rows remain subject to the current lexical
        # evidence guard and are never promoted solely to hit a count.
        legacy_quality_accepted = previous_model_label in {
            "direct",
            "adjacent",
            "relevant",
            "high",
            "直接相关",
            "高相关",
            "邻近方向",
        }
        has_object_task_evidence = (
            _services()._model_web_has_object_task_evidence(item, facets)
            if facets.get("对象") and facets.get("任务")
            else True
        )
        # A history refill is allowed to widen recall only within the same
        # research intent.  For a structured question, reject obvious
        # cross-domain rows before they consume the refill budget; accepted
        # direct/adjacent rows remain eligible even if a newer synonym rule
        # would no longer recognize their wording.
        if (
            not legacy_quality_accepted
            and not item.get("history_quality_accepted")
            and not has_object_task_evidence
        ):
            continue
        triage = _services().score_relevance(item, query, [], [])
        try:
            local_score = int(triage.get("score") or 0)
        except (TypeError, ValueError):
            local_score = 0
        try:
            citation_count = max(0, int(item.get("citation_count") or 0))
        except (TypeError, ValueError):
            citation_count = 0
        item.update(
            {
                "title": title,
                "url": url,
                "source": "model_web",
                "_previously_seen": True,
                "_history_fill": True,
                # This row was already accepted into the durable review queue for
                # the same intent.  Carry the decision forward so the later
                # model-web quality pass does not mistake a repeat for a new,
                # lower-confidence hit and remove it again.
                "history_quality_accepted": bool(
                    record.get("history_quality_accepted") or legacy_quality_accepted
                ),
                "external_id": _services().clean_text(
                    item.get("external_id")
                    or item.get("doi")
                    or item.get("arxiv_id")
                    or url,
                    500,
                ),
            }
        )
        # Keep a stable source citation even for legacy candidate rows that
        # predate provider_sources/search_paths.
        item.setdefault("source_citations", [url])
        item.setdefault(
            "provider_sources",
            [
                {
                    "source": "history",
                    "external_id": item.get("external_id", ""),
                    "url": url,
                }
            ],
        )
        item.setdefault("search_paths", [])
        candidates.append(
            {
                "_history_sort": (
                    (
                        0
                        if (
                            item.get("history_quality_accepted")
                            or legacy_quality_accepted
                        )
                        else 1
                    ),
                    0 if has_object_task_evidence else 1,
                    -local_score,
                    -citation_count,
                    _services().clean_text(entry.get("created_at"), 40),
                ),
                "_history_item": item,
            }
        )
        seen.update(identities)
    candidates.sort(key=lambda value: value["_history_sort"])
    return [
        value["_history_item"]
        for value in candidates[: max(0, target - len(existing_unique_keys))]
    ]


__all__ = [
    "search_crossref",
    "search_arxiv",
    "search_openalex",
    "search_europepmc",
    "search_semanticscholar",
    "search_dblp",
    "_openreview_value",
    "search_openreview",
    "_tavily_api_key",
    "_tavily_title_similarity",
    "_tavily_extract_identifiers",
    "_verify_tavily_result",
    "search_tavily",
    "search_deepseek",
    "deepseek_search_available",
    "model_search_available_connectors",
    "DeepSeekResponseTooLargeError",
    "fetch_deepseek_page",
    "_deepseek_tool_post",
    "_safe_public_web_url",
    "browse_public_web",
    "ModelWebSearchUnsupported",
    "_model_search_url",
    "_model_search_year",
    "normalize_model_search_results",
    "_model_search_source_cooldown",
    "_model_search_source_error",
    "_model_search_tool_schema",
    "_model_search_payload",
    "_model_search_tool_calls",
    "_model_search_append_assistant_turn",
    "_model_search_selection_rows",
    "_model_search_tool_arguments",
    "_fast_model_search_source_call",
    "_model_search_prefetch",
    "_model_search_tool_result",
    "_model_search_backfill_selection",
    "_model_search_local_fill",
    "_model_web_search_unbounded",
    "model_web_search",
    "_model_search_history_fill",
]
