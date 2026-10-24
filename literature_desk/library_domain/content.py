"""Public PDF retrieval and paper text helpers."""

from __future__ import annotations

import sqlite3

from ..services import app_services as _services

def download_public_pdf(paper_id: str, url: str = "") -> dict:
    """Download one validated public PDF into the managed attachment store.

    This shared implementation is used by the normal paper action and the
    Agent's explicit download action, so both paths apply the same HTTPS,
    size, content and deduplication rules. A candidate is never imported by
    this function; it must already be a paper in the local library.
    """
    with _services().DB_LOCK, _services().db_session() as db:
        paper = db.execute(
            "SELECT id,title,pdf_url,url,arxiv_id,doi,year FROM papers WHERE id=?",
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
    failures = []
    source_url = source_name = ""
    content = b""
    attempted_candidates = set()

    def try_candidates(candidate_list: list[str]) -> None:
        nonlocal content, source_url, source_name
        pending = list(candidate_list)
        while pending and not source_url:
            candidate = pending.pop(0)
            if candidate in attempted_candidates:
                continue
            attempted_candidates.add(candidate)
            parsed = _services().urlparse(candidate)
            host = (parsed.hostname or "unknown host").lower()
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username
                or parsed.password
            ):
                failures.append(f"{host}：地址格式无效")
                continue
            if parsed.scheme == "http" and parsed.hostname not in {
                "127.0.0.1",
                "localhost",
                "::1",
            }:
                failures.append(f"{host}：非本机地址必须使用 HTTPS")
                continue
            request = _services().Request(
                candidate,
                headers={
                    "Accept": "application/pdf,application/octet-stream;q=0.8,*/*;q=0.1",
                    "User-Agent": _services().USER_AGENT,
                },
            )
            chunks, total = [], 0
            resolved_url = candidate
            try:
                with _services().urlopen(request, timeout=30) as response:
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
                downloaded = b"".join(chunks)
                pdf_header = downloaded[:1024].lstrip(
                    b"\xef\xbb\xbf\x00\t\r\n "
                )
                if pdf_header.startswith(b"%PDF-"):
                    content = downloaded
                    source_url = candidate
                    content_disposition = ""
                    try:
                        content_disposition = response.headers.get_filename() or ""
                    except (AttributeError, TypeError):
                        pass
                    source_name = (
                        content_disposition
                        or _services().Path(
                            _services().unquote(parsed.path).rstrip("/")
                        ).name
                        or "paper.pdf"
                    )
                    return
                failures.append(
                    f"{host}：返回的是网页或非 PDF 文件（可能是落地页或需要登录）"
                )
                resolved = _services().urlparse(resolved_url)
                resolved_path = resolved.path.rstrip("/")
                resolved_host = (resolved.hostname or "").lower()
                if (
                    resolved_host.endswith("biorxiv.org")
                    or resolved_host.endswith("medrxiv.org")
                ) and resolved_path.startswith("/content/"):
                    pending.append(
                        f"{resolved.scheme}://{resolved.netloc}{resolved_path}.full.pdf"
                    )
            except _services().HTTPError as exc:
                detail = {
                    401: "需要认证",
                    403: "拒绝访问，可能需要机构权限",
                    404: "该地址没有 PDF",
                    429: "来源限流",
                }.get(exc.code, "来源请求失败")
                failures.append(f"{host}：HTTP {exc.code}（{detail}）")
            except (_services().URLError, TimeoutError, OSError, ValueError) as exc:
                reason = str(exc)[:100] or type(exc).__name__
                failures.append(f"{host}：{reason}")

    try_candidates(candidates)
    if not source_url:
        import concurrent.futures

        pmcid_match = _services().re.search(
            r"(?i)(?:^|[/=:])(PMC\d+)(?:$|[/?.&#])",
            " ".join((paper["url"] or "", paper["pdf_url"] or "")),
        )
        known_pmcid = pmcid_match.group(1).upper() if pmcid_match else ""
        resolvers = [
            (
                "OpenAlex",
                lambda: _services().openalex_public_pdf_candidates(
                    paper["doi"], paper["title"], paper["year"]
                ),
            ),
            (
                "Europe PMC",
                lambda: _services().europepmc_public_pdf_candidates(
                    paper["doi"], known_pmcid
                ),
            ),
            (
                "Semantic Scholar",
                lambda: _services().semantic_scholar_public_pdf_candidates(
                    paper["doi"]
                ),
            ),
        ]
        with _services().ThreadPoolExecutor(
            max_workers=len(resolvers), thread_name_prefix="pdf-open-access"
        ) as executor:
            futures = {
                executor.submit(resolve): source
                for source, resolve in resolvers
            }
            for future in _services().as_completed(futures):
                source = futures[future]
                try:
                    resolved_candidates = future.result()
                except Exception as exc:
                    failures.append(
                        f"{source} 开放版本查询失败：{str(exc)[:100]}"
                    )
                    continue
                if resolved_candidates:
                    try_candidates(resolved_candidates)
                elif source == "OpenAlex":
                    failures.append("OpenAlex 未找到标题/DOI 精确匹配的开放获取 PDF")
                elif source == "Europe PMC" and not known_pmcid:
                    failures.append("Europe PMC 未找到可公开获取的 PMC 全文")
                elif source == "Semantic Scholar":
                    failures.append("Semantic Scholar 未提供开放获取 PDF")
    if not source_url:
        if not failures:
            raise ValueError("没有找到这篇论文的公开 PDF 地址")
        distinct_failures = list(dict.fromkeys(failures))
        message = "PDF 下载失败：" + "；".join(distinct_failures[:6])
        if any("HTTP 403" in failure or "HTTP 401" in failure for failure in failures):
            message += (
                "。出版社拒绝了未认证访问，程序不会绕过机构权限；"
                "请连接学校 VPN/机构账号后下载，或从出版社页面下载后在阅读台上传 PDF。"
            )
        elif not any("查询失败" in failure for failure in failures):
            message += "。可以尝试从论文来源页面手动下载后上传 PDF。"
        raise ValueError(message)
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


