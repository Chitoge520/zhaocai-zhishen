from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .analysis_results import load_unsupervised_results

RISK_FUSION_SCHEMA_VERSION = "bid-audit-risk-fusion/v1"
RISK_FUSION_RESULT_SCHEMA_VERSION = "bid-audit-risk-result/v1"
RISK_FUSION_EVIDENCE_SCHEMA_VERSION = "bid-audit-risk-evidence/v1"
RISK_FUSION_ALGORITHM_VERSION = "multi-signal-risk-fusion-unsupervised/1.0.0"

_STRONG_SIGNALS = {
    "network": {"shared_full_ip", "shared_device", "shared_account", "shared_file_hash"},
    "cooccurrence": {"stable_cooccurrence", "shared_device", "shared_account", "stable_group"},
}
_SUPPORTING_SIGNALS = {
    "network": {"shared_subnet_time", "shared_file_creator", "shared_author", "upload_time_close", "modified_time_close", "created_time_close"},
    "quote": {"pairwise_price_distance", "fixed_difference", "fixed_ratio", "staircase_quote", "accompaniment_structure", "item_price_correlation", "item_rank_consistency", "median_deviation"},
    "cooccurrence": {"consecutive_cooccurrence", "winner_loser_pattern", "alternating_winner", "same_agency", "jaccard", "lift", "pmi", "shared_ip", "shared_contact", "shared_address"},
}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    temporary.replace(path)


def _identifier(prefix: str, value: object) -> str:
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{digest}"


def _text(value: object) -> str:
    return str(value or "").strip()


def _safe_source_refs(rows: Iterable[object]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for value in rows:
        if not isinstance(value, dict):
            continue
        source_path = _text(value.get("source_path"))
        source_hash = _text(value.get("source_sha256"))
        row_number = value.get("row_number")
        source_format = _text(value.get("source_format"))
        key = f"{source_path}|{source_hash}|{row_number}|{source_format}"
        if key in seen:
            continue
        seen.add(key)
        result.append({
            "source_ref_id": _identifier("source_ref", key),
            "source_sha256": source_hash,
            "row_number": row_number if isinstance(row_number, int) else None,
            "source_format": source_format,
        })
    return result


def _safe_record_ids(rows: Iterable[object]) -> list[str]:
    return sorted({_text(value) for value in rows if _text(value)})


def _strength(layer: str, signal_type: str) -> str:
    if signal_type in _STRONG_SIGNALS.get(layer, set()):
        return "strong"
    if signal_type in _SUPPORTING_SIGNALS.get(layer, set()):
        return "supporting"
    return "weak"


def _contribution_score(strength: str, source_score: object) -> int:
    try:
        value = max(0, int(float(source_score or 0)))
    except (TypeError, ValueError):
        value = 0
    caps = {"strong": 30, "supporting": 16, "weak": 8}
    floor = {"strong": 12, "supporting": 6, "weak": 3}
    return min(caps[strength], max(floor[strength], value))


def _contribution(*, layer: str, signal_type: str, source_feature_id: str, project_id: str = "", bidder_ids: Iterable[object] = (), score: object = 0, source_record_ids: Iterable[object] = (), evidence_refs: Iterable[object] = (), calculation_evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    bidder_list = sorted({_text(value) for value in bidder_ids if _text(value)})
    strength = _strength(layer, signal_type)
    seed = f"{layer}|{signal_type}|{source_feature_id}|{project_id}|{'|'.join(bidder_list)}"
    return {
        "contribution_id": _identifier("contribution", seed),
        "layer": layer,
        "signal_type": signal_type,
        "strength": strength,
        "score": _contribution_score(strength, score),
        "source_feature_id": source_feature_id or _identifier("source_feature", seed),
        "project_id": project_id,
        "bidder_ids": bidder_list,
        "evidence_refs": _safe_source_refs(evidence_refs),
        "source_record_ids": _safe_record_ids(source_record_ids),
        "calculation_evidence": calculation_evidence or {},
    }


def _source_contributions(layer: str, feature: dict[str, Any], *, project_id: str = "", bidder_ids: Iterable[object] = ()) -> list[dict[str, Any]]:
    source_feature_id = _text(feature.get("feature_id")) or _identifier("source_feature", json.dumps(feature, ensure_ascii=False, sort_keys=True))
    record_ids = feature.get("source_record_ids") or []
    refs = feature.get("evidence_refs") or []
    result: list[dict[str, Any]] = []
    contributions = feature.get("risk_contributions") or []
    for item in contributions:
        if not isinstance(item, dict):
            continue
        signal_type = _text(item.get("signal_type"))
        if not signal_type:
            continue
        result.append(_contribution(
            layer=layer,
            signal_type=signal_type,
            source_feature_id=source_feature_id,
            project_id=project_id,
            bidder_ids=bidder_ids,
            score=item.get("score", feature.get("risk_score", 0)),
            source_record_ids=record_ids,
            evidence_refs=refs,
            calculation_evidence={"source_algorithm_version": _text(feature.get("algorithm_version")), "source_score": item.get("score", feature.get("risk_score", 0))},
        ))
    return result


def _document_contributions(analysis: dict[str, Any]) -> list[tuple[tuple[str, ...], dict[str, Any]]]:
    result: list[tuple[tuple[str, ...], dict[str, Any]]] = []
    for item in analysis.get("anomalies") or []:
        if not isinstance(item, dict):
            continue
        left, right = _text(item.get("document_id_a")), _text(item.get("document_id_b"))
        if not left or not right:
            continue
        source_feature_id = _text(item.get("finding_id")) or _identifier("document_feature", f"{left}|{right}")
        record_ids = [left, right]
        pages = {"pages_a": item.get("evidence_pages_a") or [], "pages_b": item.get("evidence_pages_b") or []}
        if float(item.get("anomaly_score") or 0) > 0:
            result.append(((left, right), _contribution(
                layer="document", signal_type="document_anomaly", source_feature_id=source_feature_id,
                score=8, source_record_ids=record_ids, calculation_evidence={"anomaly_score": item.get("anomaly_score"), **pages},
            )))
        if item.get("model_triggered"):
            result.append(((left, right), _contribution(
                layer="model", signal_type="model_triggered", source_feature_id=source_feature_id,
                score=8, source_record_ids=record_ids, calculation_evidence={"model_score": item.get("model_score"), "model_threshold": item.get("model_threshold"), **pages},
            )))
    return result


def _deduplicate(contributions: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: dict[tuple[str, str], dict[str, Any]] = {}
    for item in contributions:
        key = (_text(item.get("source_feature_id")), _text(item.get("signal_type")))
        previous = selected.get(key)
        if previous is None or int(item.get("score") or 0) > int(previous.get("score") or 0):
            selected[key] = item
    return sorted(selected.values(), key=lambda row: (-int(row["score"]), row["layer"], row["signal_type"], row["contribution_id"]))


def _risk_assessment(contributions: list[dict[str, Any]]) -> tuple[int, str, dict[str, Any]]:
    if not contributions:
        return 0, "none", {"independent_layers": [], "rule": "no_triggered_signal"}
    layers = sorted({item["layer"] for item in contributions})
    strengths = {item["strength"] for item in contributions}
    base_score = sum(int(item["score"]) for item in contributions)
    score = min(100, base_score + max(0, len(layers) - 1) * 4)
    if strengths == {"weak"}:
        level = "low"
        rule = "weak_only_cap"
    elif "strong" in strengths and score >= 50 and len(layers) >= 2:
        level = "high"
        rule = "strong_plus_independent_dimension"
    elif ("strong" in strengths and score >= 25) or ("supporting" in strengths and score >= 25 and len(layers) >= 2):
        level = "medium"
        rule = "multi_signal_review"
    else:
        level = "low"
        rule = "insufficient_for_medium"
    return score, level, {"base_score": base_score, "independent_layer_bonus": max(0, len(layers) - 1) * 4, "independent_layers": layers, "rule": rule}


def _event(entity_id: str, contribution: dict[str, Any]) -> dict[str, Any]:
    event_id = _identifier("evidence_event", f"{entity_id}|{contribution['contribution_id']}")
    return {
        "schema_version": RISK_FUSION_EVIDENCE_SCHEMA_VERSION,
        "event_id": event_id,
        "entity_id": entity_id,
        "contribution_id": contribution["contribution_id"],
        "layer": contribution["layer"],
        "signal_type": contribution["signal_type"],
        "source_feature_id": contribution["source_feature_id"],
        "source_record_ids": contribution["source_record_ids"],
        "source_refs": contribution["evidence_refs"],
        "calculation_evidence": contribution["calculation_evidence"],
        "notice": "原始值不进入融合产物；请使用记录索引和来源引用在受控环境中回溯复核。",
    }


def _result(entity_type: str, identity: dict[str, Any], contributions: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    merged = _deduplicate(contributions)
    score, level, calculation = _risk_assessment(merged)
    entity_seed = json.dumps(identity, ensure_ascii=False, sort_keys=True)
    entity_id = _identifier(f"risk_{entity_type}", entity_seed)
    events = [_event(entity_id, item) for item in merged]
    result = {
        "schema_version": RISK_FUSION_RESULT_SCHEMA_VERSION,
        "algorithm_version": RISK_FUSION_ALGORITHM_VERSION,
        "entity_id": entity_id,
        "entity_type": entity_type,
        **identity,
        "risk_score": score,
        "risk_level": level,
        "review_status": "pending_review" if merged else "not_triggered",
        "risk_contributions": merged,
        "evidence_chain": {
            "summary": {"risk_level": level, "risk_score": score, "contribution_count": len(merged), "message": "风险分数仅用于待复核异常线索排序，不构成围标、串标或违法违规认定。"},
            "calculation": {"algorithm_version": RISK_FUSION_ALGORITHM_VERSION, **calculation, "contribution_ids": [item["contribution_id"] for item in merged]},
            "source": {"evidence_event_ids": [event["event_id"] for event in events], "source_record_ids": sorted({record_id for item in merged for record_id in item["source_record_ids"]})},
        },
        "notice": "缺失或未提供字段不参与低风险判断；请回到原始材料和受控证据索引交叉复核。",
    }
    return result, events


def fuse_risk_sources(network: dict[str, Any] | None = None, quotes: dict[str, Any] | None = None, cooccurrence: dict[str, Any] | None = None, analysis: dict[str, Any] | None = None) -> dict[str, Any]:
    network, quotes, cooccurrence, analysis = network or {}, quotes or {}, cooccurrence or {}, analysis or {}
    projects: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    pairs: defaultdict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    groups: defaultdict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    documents: defaultdict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)

    for layer, payload in (("network", network), ("quote", quotes)):
        for feature in payload.get("features") or []:
            if not isinstance(feature, dict):
                continue
            project_id = _text(feature.get("project_id"))
            bidder_ids = tuple(sorted({_text(feature.get("bidder_a_id")), _text(feature.get("bidder_b_id"))} - {""}))
            if len(bidder_ids) != 2:
                continue
            rows = _source_contributions(layer, feature, project_id=project_id, bidder_ids=bidder_ids)
            pairs[bidder_ids].extend(rows)
            if project_id:
                projects[project_id].extend(rows)

    for feature in cooccurrence.get("features") or []:
        if not isinstance(feature, dict):
            continue
        bidder_ids = tuple(sorted({_text(feature.get("bidder_a_id")), _text(feature.get("bidder_b_id"))} - {""}))
        if len(bidder_ids) != 2:
            continue
        project_ids = [_text(value) for value in feature.get("project_ids") or [] if _text(value)]
        rows = _source_contributions("cooccurrence", feature, bidder_ids=bidder_ids)
        pairs[bidder_ids].extend(rows)
        for project_id in project_ids:
            projects[project_id].extend([dict(item, project_id=project_id) for item in rows])

    for feature in cooccurrence.get("groups") or []:
        if not isinstance(feature, dict):
            continue
        bidder_ids = tuple(sorted({_text(value) for value in feature.get("bidder_ids") or [] if _text(value)}))
        if not bidder_ids:
            continue
        rows = _source_contributions("cooccurrence", feature, bidder_ids=bidder_ids)
        groups[bidder_ids].extend(rows)
        for project_id in [_text(value) for value in feature.get("stable_project_ids") or [] if _text(value)]:
            projects[project_id].extend([dict(item, project_id=project_id) for item in rows])

    for document_ids, contribution in _document_contributions(analysis):
        documents[document_ids].append(contribution)

    result_rows: dict[str, list[dict[str, Any]]] = {"projects": [], "bidder_pairs": [], "groups": [], "documents": []}
    events: list[dict[str, Any]] = []
    for project_id, rows in projects.items():
        item, item_events = _result("project", {"project_id": project_id}, rows)
        result_rows["projects"].append(item); events.extend(item_events)
    for bidder_ids, rows in pairs.items():
        item, item_events = _result("bidder_pair", {"bidder_ids": list(bidder_ids)}, rows)
        result_rows["bidder_pairs"].append(item); events.extend(item_events)
    for bidder_ids, rows in groups.items():
        item, item_events = _result("group", {"bidder_ids": list(bidder_ids)}, rows)
        result_rows["groups"].append(item); events.extend(item_events)
    for document_ids, rows in documents.items():
        item, item_events = _result("document", {"document_ids": list(document_ids)}, rows)
        result_rows["documents"].append(item); events.extend(item_events)
    for rows in result_rows.values():
        rows.sort(key=lambda item: (-int(item["risk_score"]), item["entity_id"]))
    events.sort(key=lambda item: (item["entity_id"], item["contribution_id"]))
    levels = Counter(item["risk_level"] for rows in result_rows.values() for item in rows)
    project_levels = Counter(item["risk_level"] for item in result_rows["projects"])
    summary = {
        "schema_version": RISK_FUSION_SCHEMA_VERSION,
        "algorithm_version": RISK_FUSION_ALGORITHM_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project_count": len(result_rows["projects"]),
        "bidder_pair_count": len(result_rows["bidder_pairs"]),
        "group_count": len(result_rows["groups"]),
        "document_count": len(result_rows["documents"]),
        "high_risk_count": levels["high"],
        "medium_risk_count": levels["medium"],
        "low_risk_count": levels["low"],
        "high_risk_project_count": project_levels["high"],
        "medium_risk_project_count": project_levels["medium"],
        "low_risk_project_count": project_levels["low"],
        "evidence_event_count": len(events),
        "input_availability": {"network": bool(network.get("features")), "quote": bool(quotes.get("features")), "cooccurrence": bool(cooccurrence.get("features") or cooccurrence.get("groups")), "document": bool(analysis.get("anomalies"))},
        "notice": "融合分数仅用于全样本异常线索排序。高风险需要至少一个强信号及另一独立分析维度佐证；仅弱信号最高为低风险，缺失字段不作为低风险依据。",
    }
    return {"summary": summary, **result_rows, "evidence_events": events}


def _load_model_pairs(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def build_risk_fusion(analysis_dir: Path, network_dir: Path, quote_dir: Path, cooccurrence_dir: Path, output_dir: Path, model_dir: Path | None = None) -> dict[str, Any]:
    from .network_analysis import load_network_analysis
    from .quote_analysis import load_quote_analysis
    from .cooccurrence_analysis import load_cooccurrence_analysis

    model_pairs = _load_model_pairs(model_dir / "model_scored_pairs.csv") if model_dir else []
    analysis = load_unsupervised_results(analysis_dir, model_pairs=model_pairs)
    result = fuse_risk_sources(load_network_analysis(network_dir), load_quote_analysis(quote_dir), load_cooccurrence_analysis(cooccurrence_dir), analysis)
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(output_dir / "project_risk_results.jsonl", result["projects"])
    _write_jsonl(output_dir / "bidder_pair_risk_results.jsonl", result["bidder_pairs"])
    _write_jsonl(output_dir / "group_risk_results.jsonl", result["groups"])
    _write_jsonl(output_dir / "document_risk_results.jsonl", result["documents"])
    _write_jsonl(output_dir / "evidence_events.jsonl", result["evidence_events"])
    _write_json(output_dir / "risk_fusion_summary.json", result["summary"])
    return {"output_dir": str(output_dir), "summary": result["summary"], "project_count": len(result["projects"]), "bidder_pair_count": len(result["bidder_pairs"]), "group_count": len(result["groups"]), "document_count": len(result["documents"])}


def load_risk_fusion(output_dir: Path) -> dict[str, Any]:
    summary_path = output_dir / "risk_fusion_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {
        "schema_version": RISK_FUSION_SCHEMA_VERSION,
        "algorithm_version": RISK_FUSION_ALGORITHM_VERSION,
        "project_count": 0, "bidder_pair_count": 0, "group_count": 0, "document_count": 0, "evidence_event_count": 0,
        "notice": "尚未生成多信号风险融合结果。",
    }
    return {"summary": summary, "projects": _read_jsonl(output_dir / "project_risk_results.jsonl"), "bidder_pairs": _read_jsonl(output_dir / "bidder_pair_risk_results.jsonl"), "groups": _read_jsonl(output_dir / "group_risk_results.jsonl"), "documents": _read_jsonl(output_dir / "document_risk_results.jsonl"), "evidence_events": _read_jsonl(output_dir / "evidence_events.jsonl")}
