from __future__ import annotations

import csv
import json
from pathlib import Path

from .finding_ids import finding_id
from .model_training import load_model, score_pair
from .unsupervised_analysis import locate_evidence, page_index


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _number(value: object, *, integer: bool = False) -> int | float:
    try:
        return int(float(str(value))) if integer else float(str(value))
    except (TypeError, ValueError):
        return 0


def _json_list(value: object) -> list:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _normalize_pair(row: dict) -> dict:
    normalized = dict(row)
    normalized["same_bidder"] = str(row.get("same_bidder", "")).lower() == "true"
    normalized["similarity"] = _number(row.get("similarity"))
    normalized["anomaly_score"] = _number(row.get("anomaly_score"))
    normalized["repeated_segment_count"] = _number(row.get("repeated_segment_count"), integer=True)
    normalized["repeated_segment_chars"] = _number(row.get("repeated_segment_chars"), integer=True)
    normalized["repeated_segments"] = _json_list(row.get("repeated_segments"))
    normalized["model_score"] = _number(row.get("model_score"))
    normalized["model_threshold"] = _number(row.get("model_threshold"))
    normalized["model_triggered"] = str(row.get("model_triggered", "")).lower() == "true"
    try:
        normalized["model_zscores"] = json.loads(row.get("model_zscores") or "{}")
    except (TypeError, json.JSONDecodeError):
        normalized["model_zscores"] = {}
    for key in ("shared_phones", "shared_emails", "shared_credit_codes", "shared_contacts", "shared_addresses"):
        normalized[key] = _json_list(row.get(key))
    return normalized


def _normalize_anomaly(row: dict) -> dict:
    normalized = dict(row)
    normalized["finding_id"] = finding_id(row)
    normalized["similarity"] = _number(row.get("similarity"))
    normalized["anomaly_score"] = _number(row.get("anomaly_score"))
    normalized["rule_score"] = _number(row.get("rule_score", row.get("anomaly_score")))
    normalized["model_score"] = _number(row.get("model_score"))
    normalized["model_threshold"] = _number(row.get("model_threshold"))
    normalized["model_triggered"] = str(row.get("model_triggered", "")).lower() == "true"
    normalized["repeated_segment_count"] = _number(row.get("repeated_segment_count"), integer=True)
    normalized["repeated_segment_chars"] = _number(row.get("repeated_segment_chars"), integer=True)
    normalized["repeated_segments"] = _json_list(row.get("repeated_segments"))
    normalized["evidence"] = _json_list(row.get("evidence"))
    normalized["evidence_pages_a"] = [_number(value, integer=True) for value in _json_list(row.get("evidence_pages_a"))]
    normalized["evidence_pages_b"] = [_number(value, integer=True) for value in _json_list(row.get("evidence_pages_b"))]
    return normalized


def _pair_key(row: dict) -> tuple[str, str]:
    return tuple(sorted((str(row.get("document_id_a", "")), str(row.get("document_id_b", "")))))


def _model_evidence(row: dict) -> list[str]:
    evidence = [f"冻结模型异常分数为 {float(row.get('model_score') or 0):.2f}，阈值为 {float(row.get('model_threshold') or 0):.2f}"]
    if float(row.get("similarity") or 0) > 0:
        evidence.append(f"文档字符级 TF-IDF 相似度为 {float(row.get('similarity') or 0):.2%}")
    labels = {
        "shared_phones": "共同电话",
        "shared_emails": "共同邮箱",
        "shared_credit_codes": "共同统一社会信用代码",
        "shared_contacts": "共同联系人",
        "shared_addresses": "共同地址",
    }
    for key, label in labels.items():
        values = _json_list(row.get(key))
        if values:
            evidence.append(f"{label}：{'、'.join(str(value) for value in values[:5])}")
    if int(float(row.get("repeated_segment_count") or 0)) > 0:
        evidence.append(
            f"发现重复片段 {int(float(row.get('repeated_segment_count') or 0))} 段，"
            "仅作为辅助证据，不能单独认定异常。"
        )
    return evidence


def _apply_model_scores(
    analysis_dir: Path,
    pairs: list[dict],
    anomalies: list[dict],
    *,
    model_path: Path | None = None,
    model_pairs: list[dict] | None = None,
) -> tuple[list[dict], list[dict], int]:
    """把冻结模型分数合并进项目内分析，避免页面和新任务使用两套结果。"""
    scored_by_key: dict[tuple[str, str], dict] = {}
    if model_pairs is not None:
        scored_by_key = {_pair_key(_normalize_pair(row)): _normalize_pair(row) for row in model_pairs}
    elif model_path and model_path.exists():
        model = load_model(model_path)
        if model:
            for pair in pairs:
                result = score_pair(pair, model)
                scored_by_key[_pair_key(pair)] = {
                    **pair,
                    "model_score": result["model_score"],
                    "model_threshold": result["model_threshold"],
                    "model_triggered": result["model_score"] >= result["model_threshold"],
                    "model_zscores": result["model_zscores"],
                }

    if not scored_by_key:
        return pairs, anomalies, 0

    merged_pairs: list[dict] = []
    for pair in pairs:
        scored = scored_by_key.get(_pair_key(pair))
        merged = {**pair, **({
            "model_score": scored.get("model_score", 0),
            "model_threshold": scored.get("model_threshold", 0),
            "model_triggered": bool(scored.get("model_triggered")),
            "model_zscores": scored.get("model_zscores", {}),
        } if scored else {})}
        merged_pairs.append(merged)

    page_map = page_index(analysis_dir / "pages.jsonl")
    findings_by_key = {_pair_key(row): row for row in anomalies}
    for pair in merged_pairs:
        if not pair.get("model_triggered"):
            continue
        key = _pair_key(pair)
        finding = findings_by_key.get(key)
        if finding is None:
            evidence_values = [
                value
                for name in ("shared_phones", "shared_emails", "shared_credit_codes", "shared_contacts", "shared_addresses")
                for value in _json_list(pair.get(name))
            ] + _json_list(pair.get("repeated_segments"))
            finding = {
                **pair,
                "finding_id": finding_id(pair),
                "rule_score": _number(pair.get("anomaly_score")),
                "anomaly_score": float(pair.get("model_score") or 0),
                "risk_level": "中",
                "evidence": _model_evidence(pair),
                "evidence_pages_a": locate_evidence(page_map, pair.get("document_id_a", ""), evidence_values),
                "evidence_pages_b": locate_evidence(page_map, pair.get("document_id_b", ""), evidence_values),
                "review_status": "待复核",
            }
            anomalies.append(finding)
            findings_by_key[key] = finding
        else:
            finding["rule_score"] = finding.get("rule_score", finding.get("anomaly_score", 0))
            finding["model_score"] = pair.get("model_score", 0)
            finding["model_threshold"] = pair.get("model_threshold", 0)
            finding["model_triggered"] = True
            finding["model_zscores"] = pair.get("model_zscores", {})
            finding["anomaly_score"] = max(float(finding.get("anomaly_score") or 0), float(pair.get("model_score") or 0))

    anomalies.sort(key=lambda row: float(row.get("anomaly_score") or 0), reverse=True)
    return merged_pairs, anomalies, sum(bool(row.get("model_triggered")) for row in merged_pairs)


def load_unsupervised_results(
    analysis_dir: Path,
    *,
    model_path: Path | None = None,
    model_pairs: list[dict] | None = None,
) -> dict:
    analysis_dir = analysis_dir.resolve()
    summary_path = analysis_dir / "analysis_summary.json"
    if not summary_path.exists():
        return {
            "ready": False,
            "analysis_dir": str(analysis_dir),
            "message": "尚未生成无监督分析结果，请先运行 .\\run.ps1 analyze。",
            "summary": {},
            "entities": [],
            "pairs": [],
            "anomalies": [],
        }
    try:
        summary = _load_json(summary_path)
        entities = _load_jsonl(analysis_dir / "document_entities.jsonl")
        pairs = [_normalize_pair(row) for row in _load_csv(analysis_dir / "pairwise_similarity.csv")]
        anomalies = [_normalize_anomaly(row) for row in _load_csv(analysis_dir / "anomaly_results.csv")]
        pairs, anomalies, model_triggered_count = _apply_model_scores(
            analysis_dir,
            pairs,
            anomalies,
            model_path=model_path,
            model_pairs=model_pairs,
        )
        summary = dict(summary)
        summary["rule_anomaly_count"] = len(_load_csv(analysis_dir / "anomaly_results.csv"))
        summary["model_triggered_count"] = model_triggered_count
        summary["anomaly_count"] = len(anomalies)
    except (OSError, UnicodeError, json.JSONDecodeError, csv.Error) as exc:
        return {
            "ready": False,
            "analysis_dir": str(analysis_dir),
            "message": f"无监督分析结果读取失败：{type(exc).__name__}",
            "summary": {},
            "entities": [],
            "pairs": [],
            "anomalies": [],
        }
    return {
        "ready": True,
        "analysis_dir": str(analysis_dir),
        "message": "结果仅为异常线索，必须结合原始文件和页码进行人工复核。",
        "summary": summary,
        "entities": entities,
        "pairs": pairs,
        "anomalies": anomalies,
        "model_triggered_count": model_triggered_count,
    }
