from __future__ import annotations

from collections import defaultdict
from typing import Any

from .audit_schema import stable_identifier

GLOBAL_EVIDENCE_GRAPH_SCHEMA_VERSION = "bid-audit-global-evidence-graph/v1"


def build_global_evidence_graph(membership: list[dict[str, Any]], pair_features: list[dict[str, Any]], groups: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a global bidder-project graph without binding bidder nodes to one project.

    Raw identifiers are deliberately omitted from shared-entity links. Each graph edge
    retains source record IDs and source references so the dashboard can return to the
    project-level evidence graph and the original source row.
    """
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    bidder_node_ids: dict[str, str] = {}
    project_node_ids: dict[str, str] = {}

    def bidder_node(bidder_id: str, name: str) -> str:
        if bidder_id not in bidder_node_ids:
            node_id = stable_identifier("global_bidder", bidder_id)
            bidder_node_ids[bidder_id] = node_id
            nodes[node_id] = {"id": node_id, "type": "bidder", "bidder_id": bidder_id, "label": name or bidder_id}
        return bidder_node_ids[bidder_id]

    def project_node(project_id: str, project_time: str = "") -> str:
        if project_id not in project_node_ids:
            node_id = stable_identifier("global_project", project_id)
            project_node_ids[project_id] = node_id
            nodes[node_id] = {"id": node_id, "type": "project", "project_id": project_id, "label": project_id, "project_time": project_time}
        return project_node_ids[project_id]

    for row in membership:
        bidder_id, project_id = str(row.get("bidder_id") or ""), str(row.get("project_id") or "")
        if not bidder_id or not project_id:
            continue
        source, target = bidder_node(bidder_id, str(row.get("bidder_name") or bidder_id)), project_node(project_id, str(row.get("project_time") or ""))
        edges.append({
            "id": stable_identifier("bidder_project", f"{bidder_id}|{project_id}"),
            "source": source, "target": target, "type": "participates",
            "project_id": project_id, "label": "参与项目",
            "source_record_ids": row.get("source_record_ids") or [], "evidence_refs": row.get("evidence_refs") or [],
        })

    for feature in pair_features:
        if not feature.get("risk_score"):
            continue
        source = bidder_node(str(feature["bidder_a_id"]), str(feature.get("bidder_a_name") or feature["bidder_a_id"]))
        target = bidder_node(str(feature["bidder_b_id"]), str(feature.get("bidder_b_name") or feature["bidder_b_id"]))
        edges.append({
            "id": stable_identifier("bidder_cooccurrence", str(feature.get("feature_id") or "")),
            "source": source, "target": target, "type": "cooccurrence_anomaly",
            "label": "跨项目共现异常模式", "risk_score": feature.get("risk_score", 0), "risk_level": feature.get("risk_level", "none"),
            "project_ids": feature.get("project_ids") or [], "signal_types": [item.get("signal_type") for item in feature.get("risk_contributions") or []],
            "source_record_ids": feature.get("source_record_ids") or [], "evidence_refs": feature.get("evidence_refs") or [],
            "review_status": feature.get("review_status", "pending"),
        })

    for group in groups:
        group_id = str(group.get("group_id") or "")
        if not group_id:
            continue
        node_id = stable_identifier("cooccurrence_group", group_id)
        nodes[node_id] = {"id": node_id, "type": "group", "group_id": group_id, "label": f"待复核团体（{group.get('member_count', 0)}）", "group_type": group.get("group_type", "connected_review_group"), "risk_score": group.get("risk_score", 0)}
        for bidder_id, bidder_name in zip(group.get("bidder_ids") or [], group.get("bidder_names") or []):
            bidder = bidder_node(str(bidder_id), str(bidder_name or bidder_id))
            edges.append({"id": stable_identifier("group_member", f"{group_id}|{bidder_id}"), "source": node_id, "target": bidder, "type": "group_member", "label": "待复核团体成员", "source_record_ids": group.get("source_record_ids") or [], "evidence_refs": group.get("evidence_refs") or []})

    project_ids = sorted(project_node_ids)
    return {
        "schema_version": GLOBAL_EVIDENCE_GRAPH_SCHEMA_VERSION,
        "node_count": len(nodes), "edge_count": len(edges), "project_count": len(project_ids), "bidder_count": len(bidder_node_ids),
        "projects": project_ids, "groups": groups,
        "nodes": sorted(nodes.values(), key=lambda row: (row["type"], row.get("label", ""))),
        "edges": edges,
        "warning": "全局图谱展示待复核异常线索和共现模式；必须回跳项目证据交叉复核，不直接认定围标、串标或违法违规。",
    }
