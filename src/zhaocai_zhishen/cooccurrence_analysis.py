from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable

from .audit_schema import normalize_text, stable_identifier

COOCCURRENCE_ANALYSIS_SCHEMA_VERSION = "bid-audit-cooccurrence-analysis/v1"
COOCCURRENCE_FEATURE_SCHEMA_VERSION = "bid-audit-cooccurrence-feature/v1"
COOCCURRENCE_GROUP_SCHEMA_VERSION = "bid-audit-cooccurrence-group/v1"
COOCCURRENCE_ALGORITHM_VERSION = "global-cooccurrence-unsupervised/1.0.0"
MIN_STABLE_COOCCURRENCES = 2

_SIGNAL_SPECS = {
    "stable_cooccurrence": ("跨项目重复共同投标", 22),
    "jaccard": ("共同投标占比偏高", 10),
    "lift": ("共同投标提升度偏高", 8),
    "pmi": ("共同投标信息强度偏高", 8),
    "consecutive_cooccurrence": ("按时间连续共同投标", 10),
    "winner_loser_pattern": ("一方中标、另一方落标模式重复", 12),
    "alternating_winner": ("共同投标中的中标主体轮换", 15),
    "same_agency": ("同一代理机构下重复共同投标", 8),
    "shared_ip": ("跨项目共享网络地址", 12),
    "shared_device": ("跨项目共享设备标识", 12),
    "shared_contact": ("跨项目共享联系人", 8),
    "shared_address": ("跨项目共享地址", 8),
    "shared_account": ("跨项目共享账号", 12),
}
_METADATA_FIELDS = {
    "shared_ip": ("ip_address", ("ip", "upload_ip")),
    "shared_device": ("device_id", ("device", "device_fingerprint")),
    "shared_contact": ("contact", ("contact", "contact_name", "联系人", "联系人姓名")),
    "shared_address": ("address", ("address", "registered_address", "注册地址", "联系地址")),
    "shared_account": ("account_id", ("account", "bid_account", "账号", "投标账号")),
}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _refs(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, int]] = set()
    output: list[dict[str, Any]] = []
    for row in rows:
        for ref in row.get("source_refs") or []:
            key = (str(ref.get("source_path", "")), str(ref.get("source_sha256", "")), int(ref.get("row_number") or 0))
            if key not in seen:
                seen.add(key)
                output.append(ref)
    return output


def _record_ids(rows: Iterable[dict[str, Any]]) -> list[str]:
    return sorted({normalize_text(row.get("record_id")) for row in rows if normalize_text(row.get("record_id"))})


def _parse_time(value: object) -> datetime | None:
    text = normalize_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _row_time(row: dict[str, Any]) -> datetime | None:
    for field in ("event_time", "uploaded_at", "created_at", "modified_at"):
        parsed = _parse_time(row.get(field))
        if parsed is not None:
            return parsed
    return None


def _value(row: dict[str, Any], field: str, aliases: Iterable[str] = ()) -> str:
    direct = normalize_text(row.get(field))
    if direct:
        return direct
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    for key in (field, *aliases):
        value = normalize_text(extra.get(key))
        if value:
            return value
    return ""


def _winner(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    text = normalize_text(value).casefold()
    if text in {"1", "true", "yes", "y", "是", "中标", "winner", "awarded", "win"}:
        return True
    if text in {"0", "false", "no", "n", "否", "未中标", "落标", "non-winner", "lost"}:
        return False
    return None


def _rank(value: object) -> int | None:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _risk_level(score: int) -> str:
    if score >= 52:
        return "high"
    if score >= 28:
        return "medium"
    if score > 0:
        return "low"
    return "none"


def _status_for_strength(co_count: int, value: float, threshold: float) -> str:
    if co_count < MIN_STABLE_COOCCURRENCES:
        return "excluded"
    return "triggered" if value >= threshold else "no_signal"


def _participants(records: list[dict[str, Any]]) -> tuple[dict[str, dict[str, list[dict[str, Any]]]], dict[str, str], dict[str, datetime | None], dict[str, str]]:
    projects: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    names: dict[str, str] = {}
    project_times: dict[str, datetime | None] = {}
    agencies: dict[str, str] = {}
    for row in records:
        project_id, bidder_id = normalize_text(row.get("project_id")), normalize_text(row.get("bidder_id"))
        if project_id:
            row_time = _row_time(row)
            current = project_times.get(project_id)
            if row_time is not None and (current is None or row_time < current):
                project_times[project_id] = row_time
            elif project_id not in project_times:
                project_times[project_id] = None
            agency = _value(row, "agency_id", ("agency", "procurement_agency_id", "代理机构", "采购代理机构", "招标代理机构"))
            if agency:
                agencies.setdefault(project_id, agency)
        if not project_id or not bidder_id:
            continue
        projects[project_id][bidder_id].append(row)
        name = normalize_text(row.get("bidder_name"))
        if name:
            names.setdefault(bidder_id, name)
    return projects, names, project_times, agencies


def _pair_metadata(records: list[dict[str, Any]]) -> tuple[dict[tuple[str, str], dict[str, list[dict[str, Any]]]], set[tuple[str, str]]]:
    by_field_value: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    pairs: set[tuple[str, str]] = set()
    for row in records:
        project_id, bidder_id = normalize_text(row.get("project_id")), normalize_text(row.get("bidder_id"))
        if not project_id or not bidder_id:
            continue
        for signal_type, (field, aliases) in _METADATA_FIELDS.items():
            value = _value(row, field, aliases)
            if value:
                by_field_value[(signal_type, value)][bidder_id].append(row)
    pair_signals: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for (signal_type, _), bidder_rows in by_field_value.items():
        bidders = sorted(bidder_rows)
        for left, right in combinations(bidders, 2):
            left_projects = {normalize_text(row.get("project_id")) for row in bidder_rows[left]}
            right_projects = {normalize_text(row.get("project_id")) for row in bidder_rows[right]}
            if not any(a != b for a in left_projects for b in right_projects):
                continue
            pair = (left, right)
            pairs.add(pair)
            pair_signals[pair][signal_type].extend(bidder_rows[left] + bidder_rows[right])
    return pair_signals, pairs


def _outcome(rows: list[dict[str, Any]]) -> tuple[bool | None, int | None]:
    winners = [_winner(row.get("is_winner", row.get("award_status"))) for row in rows]
    winner = True if True in winners else False if False in winners else None
    ranks = [_rank(row.get("rank", row.get("award_rank"))) for row in rows]
    usable = [value for value in ranks if value is not None]
    return winner, min(usable) if usable else None


def _project_evidence(project_id: str, left_rows: list[dict[str, Any]], right_rows: list[dict[str, Any]], project_time: datetime | None, agency: str) -> dict[str, Any]:
    rows = left_rows + right_rows
    return {
        "project_id": project_id,
        "project_time": project_time.isoformat(timespec="seconds") if project_time else "",
        "agency_id_hash": stable_identifier("agency", agency, 12) if agency else "",
        "source_record_ids": _record_ids(rows),
        "evidence_refs": _refs(rows),
    }


def _consecutive_count(projects: list[tuple[str, datetime | None]]) -> int:
    ordered = sorted(projects, key=lambda item: (item[1] is None, item[1] or datetime.max, item[0]))
    longest = current = 0
    previous: datetime | None = None
    for _, value in ordered:
        if value is None:
            continue
        if previous is None or (value - previous).days <= 180:
            current += 1
        else:
            current = 1
        longest = max(longest, current)
        previous = value
    return longest


def _group_components(pair_features: list[dict[str, Any]]) -> list[list[str]]:
    adjacency: dict[str, set[str]] = defaultdict(set)
    for feature in pair_features:
        if feature.get("risk_score", 0) <= 0:
            continue
        left, right = feature["bidder_a_id"], feature["bidder_b_id"]
        adjacency[left].add(right)
        adjacency[right].add(left)
    seen: set[str] = set()
    components: list[list[str]] = []
    for start in sorted(adjacency):
        if start in seen:
            continue
        stack, component = [start], []
        seen.add(start)
        while stack:
            node = stack.pop()
            component.append(node)
            for neighbor in adjacency[node]:
                if neighbor not in seen:
                    seen.add(neighbor)
                    stack.append(neighbor)
        if len(component) >= 2:
            components.append(sorted(component))
    return components


def analyze_cooccurrence_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    project_bidders, names, project_times, agencies = _participants(records)
    bidder_projects: dict[str, set[str]] = defaultdict(set)
    pair_projects: dict[tuple[str, str], list[str]] = defaultdict(list)
    pair_rows: dict[tuple[str, str], dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]]] = defaultdict(dict)
    for project_id, bidders in project_bidders.items():
        for bidder_id in bidders:
            bidder_projects[bidder_id].add(project_id)
        for left, right in combinations(sorted(bidders), 2):
            pair = (left, right)
            pair_projects[pair].append(project_id)
            pair_rows[pair][project_id] = (bidders[left], bidders[right])

    metadata_rows, metadata_pairs = _pair_metadata(records)
    candidate_pairs = sorted(set(pair_projects) | metadata_pairs)
    project_count = len(project_bidders)
    features: list[dict[str, Any]] = []
    signal_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()

    for left, right in candidate_pairs:
        common = sorted(pair_projects.get((left, right), []), key=lambda project: (project_times.get(project) is None, project_times.get(project) or datetime.max, project))
        left_projects, right_projects = bidder_projects.get(left, set()), bidder_projects.get(right, set())
        co_count = len(common)
        left_count, right_count = len(left_projects), len(right_projects)
        union_count = len(left_projects | right_projects)
        jaccard = co_count / union_count if union_count else 0.0
        lift = (co_count * project_count / (left_count * right_count)) if project_count and left_count and right_count else 0.0
        pmi = math.log2(lift) if lift > 0 else None
        evidence_rows: list[dict[str, Any]] = []
        project_evidence: list[dict[str, Any]] = []
        outcomes: list[tuple[bool | None, bool | None]] = []
        agency_co_count = 0
        for project_id in common:
            left_rows, right_rows = pair_rows[(left, right)][project_id]
            evidence_rows.extend(left_rows + right_rows)
            project_evidence.append(_project_evidence(project_id, left_rows, right_rows, project_times.get(project_id), agencies.get(project_id, "")))
            outcomes.append((_outcome(left_rows)[0], _outcome(right_rows)[0]))
            if agencies.get(project_id):
                agency_co_count += 1
        metadata = metadata_rows.get((left, right), {})
        for rows in metadata.values():
            evidence_rows.extend(rows)

        winner_loser_events = sum(1 for first, second in outcomes if first is not None and second is not None and first != second)
        outcome_events = sum(1 for first, second in outcomes if first is not None and second is not None)
        winner_loser_ratio = winner_loser_events / outcome_events if outcome_events else None
        winner_sequence = [left if first is True and second is False else right if second is True and first is False else "" for first, second in outcomes]
        winner_sequence = [item for item in winner_sequence if item]
        alternating_count = sum(1 for first, second in zip(winner_sequence, winner_sequence[1:]) if first != second)
        consecutive_count = _consecutive_count([(project, project_times.get(project)) for project in common])

        statuses: dict[str, str] = {}
        contributions: list[dict[str, Any]] = []
        def check(signal: str, status: str, detail: str, inputs: dict[str, Any]) -> None:
            statuses[signal] = status
            status_counts[f"{signal}:{status}"] += 1
            if status == "triggered":
                label, score = _SIGNAL_SPECS[signal]
                signal_counts[signal] += 1
                contributions.append({"signal_type": signal, "label": label, "score": score, "detail": detail, "inputs": inputs})

        check("stable_cooccurrence", "triggered" if co_count >= MIN_STABLE_COOCCURRENCES else "no_signal", f"共同参与 {co_count} 个项目。", {"cooccurrence_count": co_count})
        check("jaccard", _status_for_strength(co_count, jaccard, 0.45), f"Jaccard={jaccard:.4f}。", {"jaccard": round(jaccard, 6), "common_projects": co_count, "union_projects": union_count})
        check("lift", _status_for_strength(co_count, lift, 1.8), f"Lift={lift:.4f}，低基数共同投标不参与该信号触发。", {"lift": round(lift, 6), "project_count": project_count})
        check("pmi", _status_for_strength(co_count, pmi or 0.0, 0.85), f"PMI={pmi:.4f}" if pmi is not None else "缺少可计算的 PMI。", {"pmi": round(pmi, 6) if pmi is not None else None})
        check("consecutive_cooccurrence", "triggered" if consecutive_count >= 2 else "no_signal" if co_count else "not_provided", f"按项目时间排序的最长连续共同投标为 {consecutive_count} 次。", {"consecutive_count": consecutive_count})
        outcome_status = "not_provided" if outcome_events == 0 else "triggered" if winner_loser_events >= 2 and (winner_loser_ratio or 0) >= 0.75 else "no_signal"
        check("winner_loser_pattern", outcome_status, "中标和落标结果仅用于待复核模式，不作为单独认定依据。", {"winner_loser_count": winner_loser_events, "outcome_event_count": outcome_events, "ratio": round(winner_loser_ratio, 6) if winner_loser_ratio is not None else None})
        alternating_status = "not_provided" if len(winner_sequence) < 2 else "triggered" if alternating_count >= 2 and len(winner_sequence) >= 3 else "no_signal"
        check("alternating_winner", alternating_status, f"可比较中标序列发生 {alternating_count} 次主体切换。", {"alternating_count": alternating_count, "comparable_winner_count": len(winner_sequence)})
        agency_status = "not_provided" if not any(agencies.get(project) for project in common) else "triggered" if agency_co_count >= 2 else "no_signal"
        check("same_agency", agency_status, f"具备代理机构数据的共同项目为 {agency_co_count} 个。", {"same_agency_cooccurrence_count": agency_co_count})
        for signal in _METADATA_FIELDS:
            rows = metadata.get(signal, [])
            status = "triggered" if rows else "not_provided"
            check(signal, status, "跨项目共享标识已脱敏，仅保留可回溯来源记录。" if rows else "未提供可用于跨项目共享分析的字段。", {"matched_record_count": len(_record_ids(rows))})

        score = min(100, sum(int(item["score"]) for item in contributions))
        all_ids, all_refs = _record_ids(evidence_rows), _refs(evidence_rows)
        feature = {
            "schema_version": COOCCURRENCE_FEATURE_SCHEMA_VERSION,
            "algorithm_version": COOCCURRENCE_ALGORITHM_VERSION,
            "feature_id": stable_identifier("cooccurrence_pair", f"{left}|{right}"),
            "bidder_a_id": left,
            "bidder_a_name": names.get(left, left),
            "bidder_b_id": right,
            "bidder_b_name": names.get(right, right),
            "cooccurrence_count": co_count,
            "bidder_a_project_count": left_count,
            "bidder_b_project_count": right_count,
            "jaccard": round(jaccard, 6),
            "lift": round(lift, 6),
            "pmi": round(pmi, 6) if pmi is not None else None,
            "consecutive_cooccurrence_count": consecutive_count,
            "winner_loser_count": winner_loser_events,
            "winner_loser_ratio": round(winner_loser_ratio, 6) if winner_loser_ratio is not None else None,
            "alternating_winner_count": alternating_count,
            "same_agency_cooccurrence_count": agency_co_count,
            "project_ids": common,
            "project_evidence": project_evidence,
            "signal_status": statuses,
            "risk_contributions": contributions,
            "risk_score": score,
            "risk_level": _risk_level(score),
            "source_record_ids": all_ids,
            "evidence_refs": all_refs,
            "review_status": "pending" if contributions else "not_triggered",
            "interpretation": "发现跨项目共现异常模式，需结合项目证据交叉复核。" if contributions else "未发现达到最小支持度的跨项目共现异常模式。",
            "low_support_warning": co_count < MIN_STABLE_COOCCURRENCES,
        }
        features.append(feature)

    components = _group_components(features)
    groups: list[dict[str, Any]] = []
    for members in components:
        member_pairs = [feature for feature in features if feature["bidder_a_id"] in members and feature["bidder_b_id"] in members and feature["risk_score"] > 0]
        common_projects = set.intersection(*(bidder_projects[member] for member in members)) if members else set()
        stable = len(members) >= 3 and len(common_projects) >= MIN_STABLE_COOCCURRENCES
        group_refs = _refs(
            [{"source_refs": pair.get("evidence_refs") or []} for pair in member_pairs]
        )
        groups.append({
            "schema_version": COOCCURRENCE_GROUP_SCHEMA_VERSION,
            "algorithm_version": COOCCURRENCE_ALGORITHM_VERSION,
            "group_id": stable_identifier("cooccurrence_group", "|".join(members)),
            "bidder_ids": members,
            "bidder_names": [names.get(member, member) for member in members],
            "member_count": len(members),
            "edge_count": len(member_pairs),
            "stable_common_project_count": len(common_projects),
            "stable_project_ids": sorted(common_projects),
            "group_type": "stable_multi_bidder_group" if stable else "connected_review_group",
            "signal_status": {"stable_group": "triggered" if stable else "no_signal"},
            "risk_contributions": [{"signal_type": "stable_group", "label": "稳定多人共同投标团体", "score": 16}] if stable else [],
            "risk_score": min(100, sum(item["risk_score"] for item in member_pairs) // max(1, len(member_pairs)) + (16 if stable else 0)),
            "review_status": "pending" if member_pairs else "not_triggered",
            "source_record_ids": sorted({record_id for pair in member_pairs for record_id in pair["source_record_ids"]}),
            "evidence_refs": group_refs,
            "interpretation": "多人团体基于重复共现关系聚合，需回跳项目证据交叉复核。",
        })

    membership = []
    for project_id, bidders in sorted(project_bidders.items()):
        for bidder_id, rows in sorted(bidders.items()):
            membership.append({
                "project_id": project_id,
                "bidder_id": bidder_id,
                "bidder_name": names.get(bidder_id, bidder_id),
                "project_time": project_times.get(project_id).isoformat(timespec="seconds") if project_times.get(project_id) else "",
                "agency_id_hash": stable_identifier("agency", agencies[project_id], 12) if agencies.get(project_id) else "",
                "source_record_ids": _record_ids(rows),
                "evidence_refs": _refs(rows),
            })
    summary = {
        "schema_version": COOCCURRENCE_ANALYSIS_SCHEMA_VERSION,
        "algorithm_version": COOCCURRENCE_ALGORITHM_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "record_count": len(records),
        "project_count": project_count,
        "bidder_count": len(bidder_projects),
        "bidder_project_edge_count": len(membership),
        "candidate_pair_count": len(features),
        "triggered_pair_count": sum(1 for row in features if row["risk_score"] > 0),
        "high_risk_pair_count": sum(1 for row in features if row["risk_level"] == "high"),
        "medium_risk_pair_count": sum(1 for row in features if row["risk_level"] == "medium"),
        "group_count": len(groups),
        "stable_multi_bidder_group_count": sum(1 for row in groups if row["group_type"] == "stable_multi_bidder_group"),
        "signal_counts": dict(sorted(signal_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
        "evidence_trace_rate": round(sum(1 for row in features if row["risk_score"] == 0 or row["evidence_refs"]) / max(1, len(features)), 4),
        "notice": "风险分数仅用于待复核异常线索排序；单次共同投标、低基数 Lift/PMI 或单一中标关系不得单独定性。",
    }
    return {"summary": summary, "features": features, "groups": groups, "membership": membership}


def build_cooccurrence_analysis(input_path: Path, output_dir: Path) -> dict[str, Any]:
    from .global_evidence_graph import build_global_evidence_graph
    records_path = input_path / "audit_records.jsonl" if input_path.is_dir() else input_path
    result = analyze_cooccurrence_records(_read_jsonl(records_path))
    graph = build_global_evidence_graph(result["membership"], result["features"], result["groups"])
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(output_dir / "bidder_pair_features.jsonl", result["features"])
    _write_jsonl(output_dir / "group_features.jsonl", result["groups"])
    _write_json(output_dir / "global_graph.json", graph)
    _write_json(output_dir / "cooccurrence_analysis_summary.json", result["summary"])
    return {"output_dir": str(output_dir), "feature_count": len(result["features"]), "group_count": len(result["groups"]), "node_count": len(graph["nodes"]), "edge_count": len(graph["edges"]), "summary": result["summary"]}


def load_cooccurrence_analysis(output_dir: Path) -> dict[str, Any]:
    summary_path, graph_path = output_dir / "cooccurrence_analysis_summary.json", output_dir / "global_graph.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {
        "schema_version": COOCCURRENCE_ANALYSIS_SCHEMA_VERSION, "algorithm_version": COOCCURRENCE_ALGORITHM_VERSION,
        "record_count": 0, "project_count": 0, "bidder_count": 0, "candidate_pair_count": 0, "triggered_pair_count": 0,
        "group_count": 0, "notice": "尚未生成跨项目投标人共现分析结果。",
    }
    graph = json.loads(graph_path.read_text(encoding="utf-8")) if graph_path.exists() else {
        "schema_version": COOCCURRENCE_ANALYSIS_SCHEMA_VERSION, "nodes": [], "edges": [], "groups": [],
    }
    return {"summary": summary, "features": _read_jsonl(output_dir / "bidder_pair_features.jsonl"), "groups": _read_jsonl(output_dir / "group_features.jsonl"), "graph": graph}
