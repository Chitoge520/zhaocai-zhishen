from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from .llm_analysis import DEFAULT_LLM_BASE_URL, _token, get_llm_config, validate_llm_base_url

MAX_DOCUMENTS = 3
MAX_PAGES_PER_DOCUMENT = 4
MAX_CHARS_PER_PAGE = 3200
TOKEN_PATTERN = re.compile(r"<(?:邮箱|统一信用代码|证件号|电话):[0-9a-f]{10}>")
SUSPICIOUS_MARKERS = (
    "投标件",
    "Ermail",
    "PostCode",
    "网t",
    "电i话",
    "@)",
    "AANGI",
    "ANGHA",
)


def _patterns() -> list[tuple[str, re.Pattern[str]]]:
    return [
        ("邮箱", re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")),
        ("统一信用代码", re.compile(r"(?<![0-9A-Z])[0-9A-HJ-NPQRTUWXY]{18}(?![0-9A-Z])", re.I)),
        ("证件号", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
        ("电话", re.compile(r"(?<!\d)(?:\+?86[- ]?)?(?:1[3-9]\d{9}|0\d{2,3}[- ]?\d{7,8})(?!\d)")),
    ]


def _redact_with_map(text: str) -> tuple[str, dict[str, str]]:
    mapping: dict[str, str] = {}
    redacted = str(text or "")
    for kind, pattern in _patterns():
        def replace(match: re.Match[str], label: str = kind) -> str:
            token = _token(label, match.group(0))
            mapping[token] = match.group(0)
            return token

        redacted = pattern.sub(replace, redacted)
    return redacted, mapping


def _restore_tokens(text: str, mapping: dict[str, str]) -> str:
    tokens = set(TOKEN_PATTERN.findall(text))
    if not set(mapping).issubset(tokens):
        raise ValueError("大模型修改了脱敏占位符，拒绝应用 OCR 校正")
    unknown = tokens - set(mapping)
    if unknown:
        raise ValueError("大模型生成了未授权的敏感字段占位符，拒绝应用 OCR 校正")
    restored = text
    for token, value in mapping.items():
        restored = restored.replace(token, value)
    return restored


def _needs_ocr_review(text: str) -> bool:
    text = str(text or "")
    marker_hits = sum(marker in text for marker in SUSPICIOUS_MARKERS)
    mixed_fragments = len(re.findall(r"[\u4e00-\u9fff][A-Za-z]{1,3}|[A-Za-z]{1,3}[\u4e00-\u9fff]", text))
    return marker_hits > 0 or mixed_fragments >= 3


def build_ocr_packet(processed_dir: Path, max_documents: int = MAX_DOCUMENTS) -> dict:
    documents_path = processed_dir / "documents.jsonl"
    pages_path = processed_dir / "pages.jsonl"
    if not documents_path.exists() or not pages_path.exists():
        raise FileNotFoundError("处理目录缺少 documents.jsonl 或 pages.jsonl")
    documents = [json.loads(line) for line in documents_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    document_ids = {str(row.get("document_id", "")): row for row in documents}
    pages_by_document: dict[str, list[dict]] = {}
    for line in pages_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        pages_by_document.setdefault(str(row.get("document_id", "")), []).append(row)

    selected = []
    redaction_maps: dict[str, dict[str, str]] = {}
    for document_id, document in list(document_ids.items())[:max_documents]:
        count = 0
        for page in pages_by_document.get(document_id, []):
            text = str(page.get("cleaned_text") or page.get("text") or "")
            if not text.strip() or not _needs_ocr_review(text):
                continue
            text = text[:MAX_CHARS_PER_PAGE]
            redacted, mapping = _redact_with_map(text)
            key = f"{document_id}:{int(page.get('page_number') or 0)}"
            redaction_maps[key] = mapping
            selected.append({
                "document_id": document_id,
                "page_number": int(page.get("page_number") or 0),
                "bidder": document.get("bidder_name", ""),
                "text": redacted,
            })
            count += 1
            if count >= MAX_PAGES_PER_DOCUMENT:
                break

    return {
        "schema_version": "bid-llm-ocr/v1",
        "privacy": "只发送疑似 OCR 错误的脱敏页面片段，不发送 ZIP 或完整文档",
        "pages": selected,
        "redaction_maps": redaction_maps,
    }


def _extract_json(content: str) -> dict:
    content = str(content or "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.I | re.S)
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("大模型响应不是 JSON 对象")
    return parsed


def call_deepseek_ocr(packet: dict, timeout: int = 90, retries: int = 2) -> dict:
    config = get_llm_config()
    if not config["enabled"]:
        raise RuntimeError("大模型辅助功能未启用")
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("尚未配置 DeepSeek API 密钥")
    base_url = validate_llm_base_url(config.get("base_url") or DEFAULT_LLM_BASE_URL)
    system_prompt = (
        "你是 OCR 文本校正器。只能纠正明显的识别错误、断行错误和表格顺序错误，不能新增事实、金额、联系方式或公司信息。"
        "必须保留所有 <电话:...>、<邮箱:...>、<统一信用代码:...>、<证件号:...> 占位符原样不变。"
        "不确定的文字保持原样。只返回 JSON：{pages:[{document_id,page_number,status,corrected_text,changes,fields}]}。"
        "status 只能是 corrected、unchanged 或 uncertain；fields 只能包含 project_name、tender_number、bidder_name、amounts、phones、emails、addresses。"
    )
    body = json.dumps({
        "model": config["model"],
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps({"pages": packet.get("pages", [])}, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            content = payload["choices"][0]["message"]["content"]
            return _extract_json(content)
        except (OSError, KeyError, IndexError, ValueError, json.JSONDecodeError, urllib.error.HTTPError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2**attempt)
    raise RuntimeError(f"DeepSeek OCR 校正失败：{type(last_error).__name__}: {last_error}")


def validate_ocr_result(packet: dict, result: dict) -> dict:
    maps = packet.get("redaction_maps", {})
    allowed = {(str(row["document_id"]), int(row["page_number"])) for row in packet.get("pages", [])}
    corrections = []
    rejected_count = 0
    for raw in result.get("pages", []):
        key = (str(raw.get("document_id", "")), int(raw.get("page_number") or 0))
        if key not in allowed:
            rejected_count += 1
            continue
        corrected = str(raw.get("corrected_text", "")).strip()
        if not corrected or len(corrected) > MAX_CHARS_PER_PAGE * 2:
            rejected_count += 1
            continue
        map_key = f"{key[0]}:{key[1]}"
        try:
            restored = _restore_tokens(corrected, maps.get(map_key, {}))
        except ValueError:
            rejected_count += 1
            continue
        status = str(raw.get("status", "uncertain"))
        if status not in {"corrected", "unchanged", "uncertain"}:
            status = "uncertain"
        corrections.append({
            "document_id": key[0],
            "page_number": key[1],
            "status": status,
            "corrected_text": restored,
            "changes": [str(item) for item in raw.get("changes", [])][:20],
            "fields": raw.get("fields", {}) if isinstance(raw.get("fields", {}), dict) else {},
        })
    return {
        "schema_version": "bid-llm-ocr-result/v1",
        "status": "completed",
        "corrections": corrections,
        "validated_count": len(corrections),
        "rejected_count": rejected_count,
        "warning": "大模型校正只作为候选文本，不覆盖原始 OCR，应用前必须人工或规则复核。",
    }


def run_ocr_enhancement(
    processed_dir: Path,
    output_dir: Path,
    caller: Callable[[dict], dict] = call_deepseek_ocr,
) -> dict:
    config = get_llm_config()
    output_dir.mkdir(parents=True, exist_ok=True)
    if not config["enabled"] or not config["configured"]:
        result = {"status": "skipped", "reason": "大模型未启用或未配置密钥", "corrections": []}
    else:
        packet = build_ocr_packet(processed_dir)
        if not packet["pages"]:
            result = {"status": "skipped", "reason": "没有发现需要大模型复核的 OCR 页面", "corrections": []}
        else:
            raw = caller(packet)
            result = validate_ocr_result(packet, raw)
            result["status"] = "completed"
            (output_dir / "ocr_llm_raw_response.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "ocr_llm_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
