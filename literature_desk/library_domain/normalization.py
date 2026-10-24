"""Candidate record normalization and public PDF URL discovery."""

from __future__ import annotations

from ..services import app_services as _services

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
    """Build direct public-PDF candidates from stored paper metadata."""
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
    doi = _services().norm_doi(get("doi", ""))
    landing = _services().clean_text(get("url", ""), 3000).strip()
    parsed = _services().urlparse(landing)
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")
    arxiv_id = _services().norm_arxiv(get("arxiv_id", ""))
    if not arxiv_id and host.endswith("arxiv.org") and path.startswith(("/abs/", "/pdf/", "/html/")):
        arxiv_id = _services().norm_arxiv(path.split("/", 2)[2])
    if not arxiv_id and doi.startswith("10.48550/arxiv."):
        arxiv_id = _services().norm_arxiv(doi.split("/", 1)[1].removeprefix("arxiv."))
    if arxiv_id:
        candidates.append("https://arxiv.org/pdf/" + _services().quote(arxiv_id))
    query = _services().parse_qs(parsed.query, keep_blank_values=True)
    pdf_query = any(
        key.casefold() in {"pdf", "download"}
        or (
            key.casefold() in {"format", "type"}
            and any("pdf" in value.casefold() for value in values)
        )
        or any(".pdf" in value.casefold() for value in values)
        for key, values in query.items()
    )
    if path.lower().endswith(".pdf") or path.lower().endswith("/pdf") or pdf_query:
        candidates.append(landing)
    if host.endswith("biorxiv.org") or host.endswith("medrxiv.org"):
        if path.startswith("/content/"):
            candidates.append(f"{parsed.scheme}://{parsed.netloc}{path}.full.pdf")
    if doi.lower().startswith("10.64898/"):
        candidates.append(
            "https://www.biorxiv.org/content/"
            + _services().quote(doi, safe="/")
            + ".full.pdf"
        )
    pmcid_match = _services().re.search(
        r"(?i)(?:^|[/=:])(?P<pmcid>PMC\d+)(?:$|[/?.&#])",
        " ".join((landing, _services().clean_text(get("pdf_url", ""), 3000))),
    )
    if pmcid_match:
        pmcid = pmcid_match.group("pmcid").upper()
        candidates.extend(
            [
                f"https://europepmc.org/articles/{pmcid}?pdf=render",
                f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/",
            ]
        )
    return list(dict.fromkeys(item for item in candidates if item))


def openalex_public_pdf_candidates(
    doi: str = "", title: str = "", year: int | None = None
) -> list[str]:
    """Resolve open-access PDF locations by DOI or a verified title match."""
    doi = _services().norm_doi(doi)
    title = _services().clean_text(title, 1000)
    if not doi and not title:
        return []

    works = []
    if doi:
        endpoint = (
            "https://api.openalex.org/works/https://doi.org/"
            + _services().quote(doi, safe="")
        )
        try:
            work = _services().http_json(endpoint, timeout=15)
            if isinstance(work, dict) and work.get("id"):
                works = [work]
        except _services().HTTPError as exc:
            if exc.code != 404 or not title:
                raise
    if not works and title:
        search_url = "https://api.openalex.org/works?" + _services().urlencode(
            {"search": title, "per-page": 5}
        )
        data = _services().http_json(search_url, timeout=15)
        matches = []
        for work in data.get("results", []) if isinstance(data, dict) else []:
            work_title = _services().clean_text(work.get("title", ""), 1000)
            similarity = _services().title_similarity(title, work_title)
            work_year = work.get("publication_year")
            if similarity < 0.94:
                continue
            if year and work_year and abs(int(year) - int(work_year)) > 1:
                continue
            matches.append((similarity, work))
        matches.sort(key=lambda match: match[0], reverse=True)
        if matches:
            best_score = matches[0][0]
            works = [
                work for score, work in matches if score >= max(0.94, best_score - 0.02)
            ]

    locations = []
    for work in works:
        best = work.get("best_oa_location")
        if isinstance(best, dict):
            locations.append(best)
        primary = work.get("primary_location")
        if isinstance(primary, dict) and primary.get("is_oa"):
            locations.append(primary)
        locations.extend(
            location
            for location in (work.get("locations") or [])
            if isinstance(location, dict) and location.get("is_oa")
        )
    candidates = []
    for location in locations:
        pdf = location.get("pdf_url")
        if isinstance(pdf, str) and pdf.strip():
            candidates.append(pdf.strip())
    return list(dict.fromkeys(candidates))


def europepmc_public_pdf_candidates(doi: str, pmcid: str = "") -> list[str]:
    """Resolve an exact DOI to a Europe PMC/PMC full-text PDF when available."""
    doi = _services().norm_doi(doi)
    pmcid = _services().clean_text(pmcid, 32).upper()
    if pmcid and not _services().re.fullmatch(r"PMC\d+", pmcid):
        pmcid = ""
    if doi:
        endpoint = "https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + _services().urlencode(
            {
                "query": f"EXT_ID:{doi}",
                "format": "json",
                "resultType": "core",
                "pageSize": 5,
            }
        )
        data = _services().http_json(endpoint, timeout=8)
        results = (data.get("resultList") or {}).get("result", [])
        for result in results:
            if _services().norm_doi(result.get("doi", "")) != doi:
                continue
            candidate_pmcid = _services().clean_text(result.get("pmcid", ""), 32).upper()
            if _services().re.fullmatch(r"PMC\d+", candidate_pmcid):
                pmcid = candidate_pmcid
                break
    if not pmcid:
        return []

    candidates = [
        f"https://europepmc.org/articles/{pmcid}?pdf=render",
        f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/",
    ]
    # The official PMC Open Access service returns the exact PDF file in its
    # package manifest. Subscription-only PMC records simply return no PDF.
    endpoint = "https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi?" + _services().urlencode(
        {"id": pmcid}
    )
    try:
        manifest = _services().http_bytes(
            endpoint, timeout=8, accept="application/xml,text/xml;q=0.9,*/*;q=0.1"
        )
        root = _services().ET.fromstring(manifest[:1024 * 1024])
        for link in root.findall(".//link"):
            if (link.get("format") or "").casefold() != "pdf":
                continue
            href = _services().clean_text(link.get("href", ""), 2000)
            if href.startswith("ftp://ftp.ncbi.nlm.nih.gov/"):
                href = "https://ftp.ncbi.nlm.nih.gov/" + href.removeprefix(
                    "ftp://ftp.ncbi.nlm.nih.gov/"
                )
            if href.startswith("https://"):
                candidates.insert(0, href)
    except Exception:
        # The direct, stable PMC routes above remain available when the OA
        # package catalog is down or the article is outside its collection.
        pass
    return list(dict.fromkeys(candidates))


def semantic_scholar_public_pdf_candidates(doi: str) -> list[str]:
    """Look up Semantic Scholar's open-access copy for an exact DOI."""
    doi = _services().norm_doi(doi)
    if not doi:
        return []
    endpoint = (
        "https://api.semanticscholar.org/graph/v1/paper/DOI:"
        + _services().quote(doi, safe="")
        + "?fields=title,year,openAccessPdf"
    )
    data = _services().http_json(endpoint, timeout=8)
    open_pdf = data.get("openAccessPdf") or {}
    candidate = _services().clean_text(open_pdf.get("url", ""), 3000)
    return [candidate] if candidate.startswith("https://") else []


__all__ = ['clean_text', 'compact_search_raw', 'compact_candidate_record', 'normalize_record', 'paper_pdf_candidates', 'openalex_public_pdf_candidates', 'europepmc_public_pdf_candidates', 'semantic_scholar_public_pdf_candidates']
