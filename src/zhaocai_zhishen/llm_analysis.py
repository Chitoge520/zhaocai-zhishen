from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

MAX_CANDIDATES = 8
MAX_PAGES_PER_DOCUMENT = 4
MAX_CHARS_PER_PAGE = 1800
MAX_TOTAL_EXCERPT_CHARS = 24000
DEFAULT_LLM_BASE_URL = "https://api.deepseek.com"


def allowed_llm_hosts() -> set[str]:
    configured = os.environ.get("DEEPSEEK_ALLOWED_HOSTS", "api.deepseek.com")
    return {host.strip().lower() for host in configured.split(",") if host.strip()}


def validate_llm_base_url(value: str) -> str:
    base_url = str(value or DEFAULT_LLM_BASE_URL).strip().rstrip("/")
    parsed = urlparse(base_url)
    hostname = (parsed.hostname or "").lower()
    allowed_hosts = allowed_llm_hosts()
    local_http = hostname in {"localhost", "127.0.0.1", "::1"} and os.environ.get("BID_AUDIT_ALLOW_LOCAL_LLM_HTTP") == "1"
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local_http):
        raise ValueError("大模型 API 地址必须使用 HTTPS；仅允许显式开启的本机 HTTP 调试地址")
    if not hostname or hostname not in allowed_hosts:
        raise ValueError(f"大模型 API 域名不在白名单中：{hostname or '空'}")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("大模型 API 地址不能包含账号、密码、查询参数或片段")
    return base_url


def get_llm_config() -> dict:
    return {
        "enabled": os.environ.get("DEEPSEEK_ENABLED", "0").lower() in {"1", "true", "yes"},
        "configured": bool(os.environ.get("DEEPSEEK_API_KEY", "").strip()),
        "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro").strip() or "deepseek-v4-pro",
        "base_url": os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_LLM_BASE_URL).strip().rstrip("/"),
    }


def _token(kind: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"<{kind}:{digest}>"


def redact_text(text: str) -> str:
    """Mask direct identifiers while preserving stable equality across excerpts."""
    text = str(text or "")
    patterns = [
        ("邮箱", re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")),
        ("统一信用代码", re.compile(r"(?<![0-9A-Z])[0-9A-HJ-NPQRTUWXY]{18}(?![0-9A-Z])", re.I)),
        ("证件号", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
        ("电话", re.compile(r"(?<!\d)(?:\+?86[- ]?)?(?:1[3-9]\d{9}|0\d{2,3}[- ]?\d{7,8})(?!\d)")),
    ]
    for kind, pattern in patterns:
        text = pattern.sub(lambda match, label=kind: _token(label, match.group(0)), text)
    return text


def _load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _json_list(value: object) -> list:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _load_pages(processed_dir: Path, wanted: dict[str, set[int]]) -> dict[tuple[str, int], str]:
    pages: dict[tuple[str, int], str] = {}
    path = processed_dir / "pages.jsonl"
    if not path.exists():
        return pages
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            document_id = str(row.get("document_id", ""))
            page_number = int(row.get("page_number") or 0)
            if document_id in wanted and page_number in wanted[document_id]:
                pages[(document_id, page_number)] = str(row.get("text", ""))
    return pages


def _candidate_key(row: dict) -> tuple[str, str]:
    return str(row.get("document_id_a", "")), str(row.get("document_id_b", ""))


def build_evidence_packet(processed_dir: Path, analysis_dir: Path, inference_dir: Path, output_dir: Path) -> dict:
    anomalies = _load_csv(analysis_dir / "anomaly_results.csv")
    scored = _load_csv(inference_dir / "model_scored_pairs.csv")
    anomaly_keys = {_candidate_key(row) for row in anomalies}
    candidates = list(anomalies)
    candidates.extend(
        row for row in scored
        if str(row.get("model_triggered", "")).lower() == "true" and _candidate_key(row) not in anomaly_keys
    )
    candidates.sort(
        key=lambda row: max(float(row.get("anomaly_score") or 0), float(row.get("model_score") or 0)),
        reverse=True,
    )
    candidates = candidates[:MAX_CANDIDATES]

    wanted: dict[str, set[int]] = {}
    for row in candidates:
        for suffix in ("a", "b"):
            document_id = str(row.get(f"document_id_{suffix}", ""))
            page_numbers = [int(value) for value in _json_list(row.get(f"evidence_pages_{suffix}")) if str(value).isdigit()]
            if not page_numbers:
                page_numbers = [1, 2]
            wanted.setdefault(document_id, set()).update(page_numbers[:MAX_PAGES_PER_DOCUMENT])
    pages = _load_pages(processed_dir, wanted)

    excerpts = []
    total_chars = 0
    for (document_id, page_number), text in sorted(pages.items()):
        remaining = MAX_TOTAL_EXCERPT_CHARS - total_chars
        if remaining <= 0:
            break
        redacted = redact_text(text)[:min(MAX_CHARS_PER_PAGE, remaining)]
        if not redacted.strip():
            continue
        excerpts.append({"document_id": document_id, "page_number": page_number, "text": redacted})
        total_chars += len(redacted)

    packet_candidates = []
    for index, row in enumerate(candidates, start=1):
        packet_candidates.append({
            "candidate_id": f"C{index:03d}",
            "project_id": row.get("project_id", ""),
            "document_id_a": row.get("document_id_a", ""),
            "document_id_b": row.get("document_id_b", ""),
            "bidder_a": row.get("bidder_a", ""),
            "bidder_b": row.get("bidder_b", ""),
            "local_anomaly_score": float(row.get("anomaly_score") or 0),
            "model_score": float(row.get("model_score") or 0),
            "similarity": float(row.get("similarity") or 0),
            "local_evidence": [redact_text(str(item)) for item in _json_list(row.get("evidence"))],
        })
    packet = {
        "schema_version": "bid-llm-evidence/v1",
        "privacy": "仅包含脱敏后的局部证据片段，不包含原始压缩包或完整文档",
        "candidates": packet_candidates,
        "excerpts": excerpts,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "llm_input.json").write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
    return packet


def _extract_json(content: str) -> dict:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.I | re.S)
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("大模型响应不是 JSON 对象")
    return parsed


def call_deepseek(packet: dict, timeout: int = 90, retries: int = 2) -> dict:
    config = get_llm_config()
    if not config["enabled"]:
        raise RuntimeError("大模型辅助分析未启用")
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("尚未配置 DeepSeek API 密钥")
    system_prompt = (
        "你是招投标审计辅助分析器。你只能识别待复核异常线索，不能认定串标、围标或违法违规。"
        "只能使用输入中的候选项和脱敏证据片段。每个结论必须引用原文，引用内容必须逐字来自对应页。"
        "输出 JSON 对象，格式为：{summary:string, findings:[{candidate_id,title,explanation,confidence,"
        "recommended_review,citations:[{document_id,page_number,quote}]}]}。confidence 只能是 low、medium、high。"
        "证据不足时 findings 返回空数组。"
    )
    body = json.dumps({
        "model": config["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(packet, ensure_ascii=False)},
        ],
        "temperature": 0.1,
        "max_tokens": 3000,
        "response_format": {"type": "json_object"},
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{config['base_url']}/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return _extract_json(payload["choices"][0]["message"]["content"])
        except (OSError, KeyError, IndexError, ValueError, json.JSONDecodeError, urllib.error.HTTPError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2**attempt)
    raise RuntimeError(f"DeepSeek 调用失败：{type(last_error).__name__}: {last_error}")


def validate_llm_result(packet: dict, result: dict) -> dict:
    candidates = {row["candidate_id"]: row for row in packet.get("candidates", [])}
    excerpts = {
        (str(row.get("document_id", "")), int(row.get("page_number") or 0)): str(row.get("text", ""))
        for row in packet.get("excerpts", [])
    }
    findings = []
    rejected_count = 0
    for raw in result.get("findings", []):
        candidate_id = str(raw.get("candidate_id", ""))
        candidate = candidates.get(candidate_id)
        if not candidate:
            rejected_count += 1
            continue
        allowed_documents = {candidate["document_id_a"], candidate["document_id_b"]}
        citations = []
        for citation in raw.get("citations", []):
            document_id = str(citation.get("document_id", ""))
            try:
                page_number = int(citation.get("page_number") or 0)
            except (TypeError, ValueError):
                continue
            quote = str(citation.get("quote", "")).strip()
            excerpt = excerpts.get((document_id, page_number), "")
            if document_id in allowed_documents and quote and quote in excerpt:
                citations.append({"document_id": document_id, "page_number": page_number, "quote": quote})
        if not citations:
            rejected_count += 1
            continue
        confidence = str(raw.get("confidence", "low")).lower()
        if confidence not in {"low", "medium", "high"}:
            confidence = "low"
        findings.append({
            "candidate_id": candidate_id,
            "project_id": candidate["project_id"],
            "document_id_a": candidate["document_id_a"],
            "document_id_b": candidate["document_id_b"],
            "bidder_a": candidate["bidder_a"],
            "bidder_b": candidate["bidder_b"],
            "title": str(raw.get("title", "大模型辅助待复核线索")),
            "explanation": str(raw.get("explanation", "")),
            "confidence": confidence,
            "recommended_review": str(raw.get("recommended_review", "回到原始文件核对上下文和形成原因")),
            "citations": citations,
            "review_status": "待复核",
            "source": "deepseek-assisted",
        })
    return {
        "schema_version": "bid-llm-analysis/v1",
        "summary": str(result.get("summary", "")),
        "findings": findings,
        "validated_finding_count": len(findings),
        "rejected_finding_count": rejected_count,
        "warning": "大模型结果仅为经过本地引用校验的待复核线索，不构成违规认定。",
    }


def run_llm_analysis(
    processed_dir: Path,
    analysis_dir: Path,
    inference_dir: Path,
    output_dir: Path,
    caller: Callable[[dict], dict] = call_deepseek,
) -> dict:
    config = get_llm_config()
    output_dir.mkdir(parents=True, exist_ok=True)
    if not config["enabled"] or not config["configured"]:
        summary = {
            "status": "skipped",
            "reason": "大模型未启用或未配置密钥，本地分析结果不受影响。",
            "findings": [],
            "validated_finding_count": 0,
        }
    else:
        packet = build_evidence_packet(processed_dir, analysis_dir, inference_dir, output_dir)
        if not packet["candidates"] or not packet["excerpts"]:
            summary = {
                "status": "skipped",
                "reason": "没有可发送的候选线索或证据页片段。",
                "findings": [],
                "validated_finding_count": 0,
            }
        else:
            raw = caller(packet)
            (output_dir / "llm_raw_response.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            summary = {"status": "completed", **validate_llm_result(packet, raw)}
    (output_dir / "llm_analysis.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary
