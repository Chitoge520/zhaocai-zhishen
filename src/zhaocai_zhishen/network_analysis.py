from __future__ import annotations

import hashlib
import ipaddress
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable

from .audit_schema import ip_network_prefix, normalize_ip_address, normalize_text, stable_identifier

NETWORK_ANALYSIS_SCHEMA_VERSION = "bid-audit-network-analysis/v1"
NETWORK_FEATURE_SCHEMA_VERSION = "bid-audit-network-feature/v1"
NETWORK_ALGORITHM_VERSION = "network-linkage/1.0.0"
DEFAULT_NETWORK_WINDOW_SECONDS = 30 * 60
DEFAULT_METADATA_WINDOW_SECONDS = 5 * 60

SIGNAL_SPECS: dict[str, dict[str, Any]] = {
    "shared_full_ip": {"label": "共享完整 IP", "weight": 22},
    "shared_subnet_time": {"label": "同网段短时上传", "weight": 12},
    "shared_device": {"label": "共享设备指纹", "weight": 24},
    "shared_account": {"label": "共享投标账号", "weight": 20},
    "shared_file_hash": {"label": "文件 SHA-256 一致", "weight": 30},
    "shared_author": {"label": "文档作者一致", "weight": 8},
    "shared_file_creator": {"label": "文件创建者一致", "weight": 10},
    "shared_pdf_producer": {"label": "PDF Producer 一致", "weight": 5},
    "created_time_close": {"label": "文件创建时间异常接近", "weight": 6},
    "modified_time_close": {"label": "文件修改时间异常接近", "weight": 7},
    "upload_time_close": {"label": "上传时间异常接近", "weight": 8},
}

PUBLIC_EXIT_ROLES = {
    "public_exit", "platform", "platform_exit", "agency", "agency_exit",
    "procurement", "procurement_exit", "公共出口", "平台公共出口", "代理机构", "采购单位",
}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
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
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    temp.replace(path)


def _record_time(record: dict[str, Any], *fields: str) -> datetime | None:
    for field in fields:
        text = normalize_text(record.get(field))
        if not text:
            continue
        candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            continue
    return None


def _seconds_between(left: datetime, right: datetime) -> float:
    if left.tzinfo is None and right.tzinfo is not None:
        left = left.replace(tzinfo=right.tzinfo)
    elif left.tzinfo is not None and right.tzinfo is None:
        right = right.replace(tzinfo=left.tzinfo)
    return abs((left - right).total_seconds())


def _normalized_value(value: object) -> str:
    return normalize_text(value).casefold()


def _source_refs(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for record in records:
        for ref in record.get("source_refs") or []:
            if not isinstance(ref, dict):
                continue
            key = (
                ref.get("source_path", ""), ref.get("source_format", ""),
                int(ref.get("row_number") or 0), ref.get("source_sha256", ""),
            )
            unique[key] = {
                "source_path": key[0], "source_format": key[1],
                "row_number": key[2], "source_sha256": key[3],
            }
    return [unique[key] for key in sorted(unique)]


def _value_hint(signal_type: str, value: str) -> str:
    if signal_type in {"shared_full_ip", "shared_subnet_time"}:
        try:
            address = ipaddress.ip_address(value.split("/")[0])
        except ValueError:
            return "IP:invalid"
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        kind = "subnet" if signal_type == "shared_subnet_time" else "ip"
        return f"{kind}:sha256:{digest};v{address.version}"
    if signal_type == "shared_file_hash":
        return f"sha256:{value[:12]}…"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"sha256:{digest};len={len(value)}"


def _is_public_exit(record: dict[str, Any], excluded_ips: set[str]) -> bool:
    ip = normalize_text(record.get("ip_address"))
    if ip and ip in excluded_ips:
        return True
    if record.get("is_public_exit") is True:
        return True
    role = _normalized_value(record.get("network_role"))
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    role = role or _normalized_value(extra.get("network_role") or extra.get("ip_role") or extra.get("owner_type"))
    extra_flag = _normalized_value(extra.get("is_public_exit") or extra.get("public_exit"))
    return role in PUBLIC_EXIT_ROLES or extra_flag in {"1", "true", "yes", "是"}


def _signal(signal_type: str, value: str, records: list[dict[str, Any]], detail: str) -> dict[str, Any]:
    spec = SIGNAL_SPECS[signal_type]
    return {
        "signal_type": signal_type,
        "label": spec["label"],
        "weight": spec["weight"],
        "value_hint": _value_hint(signal_type, value),
        "detail": detail,
        "source_record_ids": sorted({str(row.get("record_id") or "") for row in records if row.get("record_id")}),
        "evidence_refs": _source_refs(records),
    }


def _records_with(record_rows: list[dict[str, Any]], field: str, *, usable_network: bool = False,
                  excluded_ips: set[str] | None = None) -> list[dict[str, Any]]:
    excluded_ips = excluded_ips or set()
    rows = [row for row in record_rows if normalize_text(row.get(field))]
    if usable_network:
        rows = [row for row in rows if not _is_public_exit(row, excluded_ips)]
    return rows


def _shared_value_signal(signal_type: str, field: str, left: list[dict[str, Any]], right: list[dict[str, Any]],
                         *, usable_network: bool = False, excluded_ips: set[str] | None = None) -> tuple[list[dict[str, Any]], str]:
    excluded_ips = excluded_ips or set()
    left_all = _records_with(left, field)
    right_all = _records_with(right, field)
    left_rows = _records_with(left, field, usable_network=usable_network, excluded_ips=excluded_ips)
    right_rows = _records_with(right, field, usable_network=usable_network, excluded_ips=excluded_ips)
    if not left_all or not right_all:
        return [], "not_provided"
    if usable_network and (not left_rows or not right_rows):
        return [], "excluded"
    left_by_value: dict[str, list[dict[str, Any]]] = defaultdict(list)
    right_by_value: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in left_rows:
        left_by_value[_normalized_value(row.get(field))].append(row)
    for row in right_rows:
        right_by_value[_normalized_value(row.get(field))].append(row)
    signals: list[dict[str, Any]] = []
    for value in sorted(set(left_by_value) & set(right_by_value)):
        if not value:
            continue
        evidence = left_by_value[value] + right_by_value[value]
        signals.append(_signal(signal_type, value, evidence, f"两家投标人在字段 {field} 上出现一致值，需回到原始记录核验。"))
    return signals, "triggered" if signals else "no_signal"


def _subnet_time_signal(left: list[dict[str, Any]], right: list[dict[str, Any]], excluded_ips: set[str],
                        window_seconds: int) -> tuple[list[dict[str, Any]], str]:
    left_all = _records_with(left, "ip_address")
    right_all = _records_with(right, "ip_address")
    if not left_all or not right_all:
        return [], "not_provided"
    left_rows = [row for row in left_all if not _is_public_exit(row, excluded_ips)]
    right_rows = [row for row in right_all if not _is_public_exit(row, excluded_ips)]
    if not left_rows or not right_rows:
        return [], "excluded"
    matches: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for left_row in left_rows:
        left_time = _record_time(left_row, "event_time", "uploaded_at")
        left_ip = normalize_text(left_row.get("ip_address"))
        left_prefix = ip_network_prefix(left_ip)
        if not left_time or not left_prefix:
            continue
        for right_row in right_rows:
            right_time = _record_time(right_row, "event_time", "uploaded_at")
            right_ip = normalize_text(right_row.get("ip_address"))
            if not right_time or right_ip == left_ip or ip_network_prefix(right_ip) != left_prefix:
                continue
            if _seconds_between(left_time, right_time) <= window_seconds:
                matches[left_prefix].extend([left_row, right_row])
    signals = [
        _signal(
            "shared_subnet_time",
            prefix,
            rows,
            f"两家投标人位于同一规范化网段，上传时间差不超过 {window_seconds // 60} 分钟。",
        )
        for prefix, rows in sorted(matches.items())
    ]
    return signals, "triggered" if signals else "no_signal"


def _close_time_signal(signal_type: str, fields: tuple[str, ...], left: list[dict[str, Any]], right: list[dict[str, Any]],
                       window_seconds: int) -> tuple[list[dict[str, Any]], str]:
    left_rows = [row for row in left if _record_time(row, *fields)]
    right_rows = [row for row in right if _record_time(row, *fields)]
    if not left_rows or not right_rows:
        return [], "not_provided"
    best: tuple[float, dict[str, Any], dict[str, Any]] | None = None
    for left_row in left_rows:
        left_time = _record_time(left_row, *fields)
        for right_row in right_rows:
            right_time = _record_time(right_row, *fields)
            if left_time is None or right_time is None:
                continue
            delta = _seconds_between(left_time, right_time)
            if best is None or delta < best[0]:
                best = (delta, left_row, right_row)
    if best is None or best[0] > window_seconds:
        return [], "no_signal"
    delta, left_row, right_row = best
    value = f"{int(delta)}s"
    detail = f"两份记录对应时间相差 {int(delta)} 秒，不超过 {window_seconds} 秒阈值。"
    return [_signal(signal_type, value, [left_row, right_row], detail)], "triggered"


def _risk_level(score: int) -> str:
    if score >= 50:
        return "high"
    if score >= 25:
        return "medium"
    if score > 0:
        return "low"
    return "none"


def _project_bidder_records(records: list[dict[str, Any]]) -> tuple[dict[str, dict[str, list[dict[str, Any]]]], dict[tuple[str, str], str]]:
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    names: dict[tuple[str, str], str] = {}
    for row in records:
        project_id = normalize_text(row.get("project_id"))
        bidder_id = normalize_text(row.get("bidder_id"))
        if not project_id or not bidder_id:
            continue
        grouped[project_id][bidder_id].append(row)
        name = normalize_text(row.get("bidder_name"))
        if name:
            names[(project_id, bidder_id)] = name
    return grouped, names


def analyze_network_records(records: list[dict[str, Any]], *, excluded_ips: Iterable[str] = (),
                            network_window_seconds: int = DEFAULT_NETWORK_WINDOW_SECONDS,
                            metadata_window_seconds: int = DEFAULT_METADATA_WINDOW_SECONDS) -> dict[str, Any]:
    excluded_set = {
        normalized
        for value in excluded_ips
        for normalized, valid in [normalize_ip_address(value)]
        if valid and normalized
    }
    grouped, names = _project_bidder_records(records)
    features: list[dict[str, Any]] = []
    graph_nodes: list[dict[str, Any]] = []
    graph_edges: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    signal_counts: Counter[str] = Counter()
    expected_pairs = 0

    for project_id in sorted(grouped):
        bidder_rows = grouped[project_id]
        bidders = sorted(bidder_rows)
        for bidder_id in bidders:
            graph_nodes.append({
                "id": f"{project_id}:{bidder_id}", "project_id": project_id,
                "bidder_id": bidder_id, "label": names.get((project_id, bidder_id), bidder_id),
                "type": "bidder",
            })
        expected_pairs += len(bidders) * (len(bidders) - 1) // 2
        for left_id, right_id in combinations(bidders, 2):
            left, right = bidder_rows[left_id], bidder_rows[right_id]
            signals: list[dict[str, Any]] = []
            statuses: dict[str, str] = {}
            checks = [
                ("shared_full_ip", _shared_value_signal("shared_full_ip", "ip_address", left, right, usable_network=True, excluded_ips=excluded_set)),
                ("shared_subnet_time", _subnet_time_signal(left, right, excluded_set, network_window_seconds)),
                ("shared_device", _shared_value_signal("shared_device", "device_id", left, right)),
                ("shared_account", _shared_value_signal("shared_account", "account_id", left, right)),
                ("shared_file_hash", _shared_value_signal("shared_file_hash", "file_sha256", left, right)),
                ("shared_author", _shared_value_signal("shared_author", "author", left, right)),
                ("shared_file_creator", _shared_value_signal("shared_file_creator", "file_creator", left, right)),
                ("shared_pdf_producer", _shared_value_signal("shared_pdf_producer", "pdf_producer", left, right)),
                ("created_time_close", _close_time_signal("created_time_close", ("created_at",), left, right, metadata_window_seconds)),
                ("modified_time_close", _close_time_signal("modified_time_close", ("modified_at",), left, right, metadata_window_seconds)),
                ("upload_time_close", _close_time_signal("upload_time_close", ("uploaded_at", "event_time"), left, right, metadata_window_seconds)),
            ]
            for signal_type, (matched, status) in checks:
                statuses[signal_type] = status
                status_counts[f"{signal_type}:{status}"] += 1
                signals.extend(matched)
                signal_counts[signal_type] += len(matched)
            contributions = [
                {"signal_type": row["signal_type"], "label": row["label"], "score": row["weight"]}
                for row in signals
            ]
            score = min(100, sum(row["weight"] for row in signals))
            all_evidence_records = [row for row in left + right if row.get("record_id")]
            feature_id = stable_identifier("network_feature", f"{project_id}|{left_id}|{right_id}")
            feature = {
                "schema_version": NETWORK_FEATURE_SCHEMA_VERSION,
                "algorithm_version": NETWORK_ALGORITHM_VERSION,
                "feature_id": feature_id,
                "project_id": project_id,
                "bidder_a_id": left_id,
                "bidder_a_name": names.get((project_id, left_id), left_id),
                "bidder_b_id": right_id,
                "bidder_b_name": names.get((project_id, right_id), right_id),
                "signal_status": statuses,
                "signals": signals,
                "risk_contributions": contributions,
                "risk_score": score,
                "risk_level": _risk_level(score),
                "source_record_ids": sorted({str(row.get("record_id")) for row in all_evidence_records}),
                "evidence_refs": _source_refs(all_evidence_records),
                "review_status": "pending" if signals else "not_triggered",
                "interpretation": (
                    f"发现 {len(signals)} 项可追溯关联信号，建议结合原始上传日志和文件复核。"
                    if signals else "已完成可用字段比较，未发现匹配信号；未提供的字段不参与低风险判断。"
                ),
            }
            features.append(feature)
            if signals:
                graph_edges.append({
                    "id": feature_id,
                    "project_id": project_id,
                    "source": f"{project_id}:{left_id}",
                    "target": f"{project_id}:{right_id}",
                    "type": "network_metadata_link",
                    "signal_types": sorted({row["signal_type"] for row in signals}),
                    "risk_score": score,
                    "risk_level": _risk_level(score),
                    "source_record_ids": feature["source_record_ids"],
                    "evidence_refs": feature["evidence_refs"],
                })

    available_signal_types = [
        signal_type for signal_type in SIGNAL_SPECS
        if any(feature["signal_status"].get(signal_type) != "not_provided" for feature in features)
    ]
    summary = {
        "schema_version": NETWORK_ANALYSIS_SCHEMA_VERSION,
        "algorithm_version": NETWORK_ALGORITHM_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "record_count": len(records),
        "project_count": len(grouped),
        "bidder_count": len({bidder for project in grouped.values() for bidder in project}),
        "expected_pair_count": expected_pairs,
        "completed_pair_count": len(features),
        "triggered_pair_count": sum(1 for row in features if row["signals"]),
        "high_risk_pair_count": sum(1 for row in features if row["risk_level"] == "high"),
        "medium_risk_pair_count": sum(1 for row in features if row["risk_level"] == "medium"),
        "low_risk_pair_count": sum(1 for row in features if row["risk_level"] == "low"),
        "available_signal_types": available_signal_types,
        "signal_counts": dict(sorted(signal_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
        "excluded_ip_count": len(excluded_set),
        "network_window_seconds": network_window_seconds,
        "metadata_window_seconds": metadata_window_seconds,
        "evidence_trace_rate": round(
            sum(1 for row in features for signal in row["signals"] if signal["evidence_refs"])
            / max(1, sum(len(row["signals"]) for row in features)), 4
        ) if any(row["signals"] for row in features) else 1.0,
        "notice": "风险分数仅用于异常线索排序，单一 IP、设备、账号或元数据不能直接认定围标串标。",
    }
    graph = {
        "schema_version": NETWORK_ANALYSIS_SCHEMA_VERSION,
        "algorithm_version": NETWORK_ALGORITHM_VERSION,
        "nodes": graph_nodes,
        "edges": graph_edges,
        "projects": sorted(grouped),
    }
    return {"summary": summary, "features": features, "graph": graph}


def build_network_analysis(input_path: Path, output_dir: Path, *, excluded_ips: Iterable[str] = (),
                           network_window_seconds: int = DEFAULT_NETWORK_WINDOW_SECONDS,
                           metadata_window_seconds: int = DEFAULT_METADATA_WINDOW_SECONDS) -> dict[str, Any]:
    records_path = input_path / "audit_records.jsonl" if input_path.is_dir() else input_path
    records = _read_jsonl(records_path)
    result = analyze_network_records(
        records,
        excluded_ips=excluded_ips,
        network_window_seconds=network_window_seconds,
        metadata_window_seconds=metadata_window_seconds,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(output_dir / "network_features.jsonl", result["features"])
    _write_json(output_dir / "network_graph.json", result["graph"])
    _write_json(output_dir / "network_analysis_summary.json", result["summary"])
    return {
        "output_dir": str(output_dir),
        "feature_count": len(result["features"]),
        "edge_count": len(result["graph"]["edges"]),
        "summary": result["summary"],
    }


def load_network_analysis(output_dir: Path) -> dict[str, Any]:
    summary_path = output_dir / "network_analysis_summary.json"
    graph_path = output_dir / "network_graph.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {
        "schema_version": NETWORK_ANALYSIS_SCHEMA_VERSION,
        "algorithm_version": NETWORK_ALGORITHM_VERSION,
        "record_count": 0,
        "project_count": 0,
        "bidder_count": 0,
        "expected_pair_count": 0,
        "completed_pair_count": 0,
        "triggered_pair_count": 0,
        "available_signal_types": [],
        "signal_counts": {},
        "notice": "尚未生成 IP、设备和文件元数据关联分析结果。",
    }
    graph = json.loads(graph_path.read_text(encoding="utf-8")) if graph_path.exists() else {
        "schema_version": NETWORK_ANALYSIS_SCHEMA_VERSION, "nodes": [], "edges": [], "projects": [],
    }
    features = _read_jsonl(output_dir / "network_features.jsonl")
    return {"summary": summary, "features": features, "graph": graph}
