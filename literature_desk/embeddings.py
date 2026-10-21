"""Small, provider-neutral helpers for OpenAI-compatible embeddings."""

from __future__ import annotations

import math
import re


def split_text(text: str, chunk_chars: int = 1800, overlap: int = 180) -> list[tuple[str, int, int]]:
    """Split extracted PDF text into bounded, overlapping evidence chunks."""
    value = str(text or "")
    chunk_chars = max(256, int(chunk_chars))
    overlap = min(max(0, int(overlap)), chunk_chars // 3)
    chunks = []
    start = 0
    while start < len(value):
        end = min(len(value), start + chunk_chars)
        if end < len(value):
            floor = start + chunk_chars * 2 // 3
            boundary = max(value.rfind(mark, floor, end) for mark in ("\n", ". ", "。", "; ", "；"))
            if boundary >= floor:
                end = boundary + 1
        while start < end and value[start].isspace():
            start += 1
        body = value[start:end].strip()
        if body:
            left_trim = len(value[start:end]) - len(value[start:end].lstrip())
            right_trim = len(value[start:end]) - len(value[start:end].rstrip())
            chunks.append((body, start + left_trim, end - right_trim))
        if end >= len(value):
            break
        next_start = max(start + 1, end - overlap)
        start = next_start
    return chunks


def validate_embedding_response(payload, expected_count: int) -> tuple[list[list[float]], int]:
    """Validate and order an OpenAI-style embeddings response."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Embedding 接口没有返回 data 列表")
    rows = payload["data"]
    if len(rows) != expected_count:
        raise ValueError(f"Embedding 返回数量不匹配：需要 {expected_count} 条，实际 {len(rows)} 条")
    indexed = []
    for fallback_index, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get("embedding"), list):
            raise ValueError("Embedding 响应包含无效向量")
        try:
            index = int(row.get("index", fallback_index))
        except (TypeError, ValueError):
            raise ValueError("Embedding 响应包含无效索引")
        vector = row["embedding"]
        try:
            normalized = [float(value) for value in vector]
        except (TypeError, ValueError, OverflowError):
            raise ValueError("Embedding 响应包含非数值向量")
        if not normalized or any(not math.isfinite(value) for value in normalized):
            raise ValueError("Embedding 向量为空或包含非有限数值")
        if not any(normalized):
            raise ValueError("Embedding 向量不能全为零")
        indexed.append((index, normalized))
    if sorted(index for index, _ in indexed) != list(range(expected_count)):
        raise ValueError("Embedding 响应索引不连续或重复")
    indexed.sort(key=lambda item: item[0])
    dimensions = {len(vector) for _, vector in indexed}
    if len(dimensions) != 1:
        raise ValueError("Embedding 响应向量维度不一致")
    return [vector for _, vector in indexed], dimensions.pop()


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def reciprocal_rank_fusion(*rankings: list[dict], k: int = 60) -> list[dict]:
    """Fuse ranked evidence lanes while preserving source ranks and scores."""
    fused = {}
    lane_keys = []
    for lane_name, lane in zip(("lexical", "semantic"), rankings):
        keys = set()
        for rank, source in enumerate(lane, 1):
            key = (source.get("attachment_id"), source.get("page_number"))
            keys.add(key)
            item = fused.setdefault(key, dict(source))
            item[f"{lane_name}_rank"] = rank
            score = source.get("semantic_score" if lane_name == "semantic" else "lexical_score")
            if score is not None:
                item[f"{lane_name}_score"] = score
            item["rrf_score"] = item.get("rrf_score", 0.0) + 1.0 / (k + rank)
        lane_keys.append(keys)
    lexical_keys = lane_keys[0] if lane_keys else set()
    semantic_keys = lane_keys[1] if len(lane_keys) > 1 else set()
    for key, item in fused.items():
        if key in lexical_keys and key in semantic_keys:
            item["retrieval_method"] = "hybrid"
        elif key in semantic_keys:
            item["retrieval_method"] = "semantic"
    return sorted(fused.values(), key=lambda item: (-item.get("rrf_score", 0.0), -item.get("semantic_score", -1.0), item.get("lexical_rank", 10**9), item.get("page_number", 0)))


def clean_embedding_text(text: str) -> str:
    """Normalize layout noise without removing mathematical or multilingual text."""
    value = str(text or "").replace("\x00", " ")
    value = re.sub(r"[\t\r\f\v]+", " ", value)
    return re.sub(r" {2,}", " ", value).strip()
