from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from .analysis_results import load_unsupervised_results
from .finding_ids import finding_id


ENTITY_FIELDS = {
    "phones": "phone",
    "emails": "email",
    "credit_codes": "credit_code",
    "contacts": "contact",
    "addresses": "address",
}


def _stable_id(prefix: str, *parts: object) -> str:
    import hashlib

    raw = "\x1f".join(str(part or "") for part in parts)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}:{digest}"


def _entity_label(entity_type: str, value: str) -> str:
    labels = {
        "phone": "电话",
        "email": "邮箱",
        "credit_code": "统一社会信用代码",
        "contact": "联系人",
        "address": "地址",
    }
    if entity_type == "phone" and len(value) > 4:
        display = f"···{value[-4:]}"
    elif entity_type == "email" and "@" in value:
        display = f"···{value[value.index('@'):]}"
    else:
        display = value[:28]
    return f"{labels.get(entity_type, entity_type)}：{display}"


def build_evidence_graph(entities: list[dict], pairs: list[dict], anomalies: list[dict]) -> dict:
    """Build a project-scoped graph from extracted entities and pair evidence."""

    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    bidder_ids: dict[tuple[str, str], str] = {}
    document_ids: dict[tuple[str, str], str] = {}
    findings = []
    anomaly_by_pair = {}

    for row in anomalies:
        finding = dict(row)
        finding["finding_id"] = finding_id(row)
        findings.append(finding)
        anomaly_by_pair[(row.get("project_id", ""), row.get("document_id_a", ""), row.get("document_id_b", ""))] = finding

    def add_node(node_id: str, node_type: str, label: str, project_id: str, **extra: object) -> None:
        if node_id not in nodes:
            nodes[node_id] = {"id": node_id, "type": node_type, "label": label, "project_id": project_id, **extra}

    def bidder_node(project_id: str, bidder: str) -> str:
        key = (project_id, bidder or "未识别投标人")
        if key not in bidder_ids:
            node_id = _stable_id("bidder", project_id, key[1])
            bidder_ids[key] = node_id
            add_node(node_id, "bidder", key[1], project_id)
        return bidder_ids[key]

    def document_node(project_id: str, document_id: str, file_path: str = "") -> str:
        key = (project_id, document_id)
        if key not in document_ids:
            node_id = _stable_id("document", project_id, document_id)
            document_ids[key] = node_id
            add_node(node_id, "document", Path(file_path).name or document_id, project_id, document_id=document_id, file_path=file_path)
        return document_ids[key]

    for row in entities:
        project_id = str(row.get("project_id") or "未识别项目")
        project_node = _stable_id("project", project_id)
        add_node(project_node, "project", project_id, project_id)
        bidder = str(row.get("bidder") or row.get("bidder_raw") or "未识别投标人")
        bidder_id = bidder_node(project_id, bidder)
        edges.append({"source": project_node, "target": bidder_id, "type": "participates", "project_id": project_id, "label": "参与项目"})
        doc_id = str(row.get("document_id") or "")
        if doc_id:
            doc_node = document_node(project_id, doc_id, str(row.get("file_path") or ""))
            edges.append({"source": bidder_id, "target": doc_node, "type": "submitted_document", "project_id": project_id, "label": "提交文件"})
        for field, entity_type in ENTITY_FIELDS.items():
            for value in row.get(field, []) or []:
                value = str(value).strip()
                if not value:
                    continue
                entity_node = _stable_id(entity_type, project_id, value)
                add_node(entity_node, entity_type, _entity_label(entity_type, value), project_id, value=value)
                edges.append({
                    "source": bidder_id,
                    "target": entity_node,
                    "type": "shared_entity",
                    "project_id": project_id,
                    "entity_type": entity_type,
                    "entity_value": value,
                    "label": f"共同{_entity_label(entity_type, value).split('：', 1)[0]}",
                })

    for row in pairs:
        if row.get("same_bidder"):
            continue
        project_id = str(row.get("project_id") or "未识别项目")
        project_node = _stable_id("project", project_id)
        add_node(project_node, "project", project_id, project_id)
        bidder_a = bidder_node(project_id, str(row.get("bidder_a") or "未识别投标人 A"))
        bidder_b = bidder_node(project_id, str(row.get("bidder_b") or "未识别投标人 B"))
        finding = anomaly_by_pair.get((project_id, row.get("document_id_a", ""), row.get("document_id_b", "")))
        edges.append({
            "source": bidder_a,
            "target": bidder_b,
            "type": "text_similarity",
            "project_id": project_id,
            "similarity": row.get("similarity", 0),
            "anomaly_score": row.get("anomaly_score", 0),
            "document_id_a": row.get("document_id_a", ""),
            "document_id_b": row.get("document_id_b", ""),
            "finding_id": finding.get("finding_id") if finding else None,
            "evidence": finding.get("evidence", []) if finding else [],
            "label": "文本相似比较",
        })

    project_ids = sorted({node["project_id"] for node in nodes.values() if node["type"] == "project"})
    return {
        "schema_version": "evidence-graph/v1",
        "node_count": len(nodes),
        "edge_count": len(edges),
        "project_count": len(project_ids),
        "projects": project_ids,
        "nodes": sorted(nodes.values(), key=lambda node: (node["project_id"], node["type"], node["label"])),
        "edges": edges,
        "findings": findings,
        "warning": "关系图用于定位可疑关联和证据入口，不直接认定串标、围标或违规。",
    }


def load_evidence_graph(analysis_dir: Path, *, model_path: Path | None = None) -> dict:
    """Load the graph from the same rule/model-fused result set as the main API."""
    results = load_unsupervised_results(analysis_dir, model_path=model_path)
    graph = build_evidence_graph(results.get("entities", []), results.get("pairs", []), results.get("anomalies", []))
    graph["ready"] = bool(results.get("ready"))
    graph["analysis_dir"] = str(Path(analysis_dir).resolve())
    return graph
