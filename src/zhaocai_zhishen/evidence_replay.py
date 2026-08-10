from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .analysis_results import load_unsupervised_results
from .finding_ids import finding_id


@lru_cache(maxsize=8)
def _load_pages_cached(path_string: str, modified_ns: int) -> dict[tuple[str, int], str]:
    del modified_ns
    pages: dict[tuple[str, int], str] = {}
    path = Path(path_string)
    if not path.exists():
        return pages
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            document_id = str(row.get("document_id") or "")
            try:
                page_number = int(row.get("page_number") or 0)
            except (TypeError, ValueError):
                page_number = 0
            if document_id and page_number > 0:
                pages[(document_id, page_number)] = str(row.get("text") or "")
    return pages


def _page_index(processed_dir: Path) -> dict[tuple[str, int], str]:
    path = (processed_dir / "pages.jsonl").resolve()
    try:
        modified_ns = path.stat().st_mtime_ns
    except OSError:
        modified_ns = 0
    return _load_pages_cached(str(path), modified_ns)


def _load_documents(processed_dir: Path) -> dict[str, dict]:
    path = processed_dir / "documents.jsonl"
    documents = {}
    if not path.exists():
        return documents
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            document_id = str(row.get("document_id") or "")
            if document_id:
                documents[document_id] = row
    return documents


def _page_payload(document: dict, document_id: str, page_number: int, pages: dict[tuple[str, int], str]) -> dict:
    text = pages.get((document_id, page_number), "")
    return {
        "document_id": document_id,
        "bidder": document.get("bidder") or document.get("bidder_name") or "未识别投标人",
        "file_path": document.get("file_path", ""),
        "page_number": page_number,
        "text": text,
        "excerpt": text[:4000],
        "available": bool(text),
    }


def load_evidence_detail(analysis_dir: Path, processed_dir: Path, finding_key: str) -> dict | None:
    results = load_unsupervised_results(analysis_dir)
    finding = next((row for row in results.get("anomalies", []) if finding_id(row) == finding_key), None)
    if finding is None:
        return None
    documents = _load_documents(processed_dir)
    pages = _page_index(processed_dir)
    document_a_id = str(finding.get("document_id_a") or "")
    document_b_id = str(finding.get("document_id_b") or "")
    document_a = documents.get(document_a_id, {})
    document_b = documents.get(document_b_id, {})
    pages_a = finding.get("evidence_pages_a", []) or [1]
    pages_b = finding.get("evidence_pages_b", []) or [1]
    page_a = _page_payload(document_a, document_a_id, int(pages_a[0]), pages)
    page_b = _page_payload(document_b, document_b_id, int(pages_b[0]), pages)
    citations = [
        _page_payload(document_a, document_a_id, int(page), pages) for page in pages_a[:8]
    ] + [
        _page_payload(document_b, document_b_id, int(page), pages) for page in pages_b[:8]
    ]
    return {
        "finding": finding,
        "document_a": page_a,
        "document_b": page_b,
        "pages_a": [_page_payload(document_a, document_a_id, int(page), pages) for page in pages_a[:8]],
        "pages_b": [_page_payload(document_b, document_b_id, int(page), pages) for page in pages_b[:8]],
        "shared_entities": [item for item in finding.get("evidence", []) if "相似度" not in str(item)],
        "citations": citations,
        "warning": "原文片段仅用于辅助复核，最终判断必须回到原始投标文件。",
    }
