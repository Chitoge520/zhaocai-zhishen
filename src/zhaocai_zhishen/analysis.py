from __future__ import annotations

from dataclasses import asdict

from .models import BidDocument
from .parsing import parse_directory


def _count(values: list[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        if value:
            result[value] = result.get(value, 0) + 1
    return result


def build_analysis(data_dir):
    documents = parse_directory(data_dir)
    all_prices = [doc.price for doc in documents if doc.price]
    project_prices: dict[str, list[float]] = {}
    for doc in documents:
        if doc.price:
            project_prices.setdefault(doc.dataset_project, []).append(doc.price)
    author_counts = _count([doc.author for doc in documents])
    modifier_counts = _count([doc.last_modified_by for doc in documents])
    contact_counts = _count([doc.contact for doc in documents])

    records = []
    for doc in documents:
        prices = project_prices.get(doc.dataset_project, [])
        avg_price = sum(prices) / len(prices) if prices else None
        min_price = min(prices) if prices else None
        price_gap = abs((doc.price or 0) - avg_price) / avg_price if avg_price and doc.price else 0
        low_gap = ((doc.price or 0) - min_price) / min_price if min_price and doc.price else 0
        service_ratio = (doc.service_price or 0) / doc.price if doc.price else 0
        install_ratio = (doc.install_price or 0) / doc.price if doc.price else 0
        metadata_score = 0
        if doc.author and author_counts.get(doc.author, 0) > 1:
            metadata_score += 12
        if doc.last_modified_by and modifier_counts.get(doc.last_modified_by, 0) > 1:
            metadata_score += 8
        if doc.contact and contact_counts.get(doc.contact, 0) > 1:
            metadata_score += 15

        score = 25 * price_gap
        score += 20 * min(low_gap, 0.15) / 0.15
        score += min(doc.negative_deviations * 6, 24)
        score += min(abs(service_ratio - 0.06) * 120, 12)
        score += min(abs(install_ratio - 0.28) * 70, 12)
        score += metadata_score
        score = max(0, min(100, round(score, 1)))
        level = "高" if score >= 65 else "中" if score >= 35 else "低"

        evidence = []
        if avg_price and doc.price:
            evidence.append(f"投标总价较平均报价偏离 {price_gap * 100:.2f}%")
            evidence.append(f"较最低报价高 {low_gap * 100:.2f}%")
        if doc.negative_deviations:
            evidence.append(f"投标文件出现 {doc.negative_deviations} 处负偏离描述")
        if metadata_score:
            evidence.append("文件作者、修改人或联系人与其他投标文件存在重复线索")
        if doc.references:
            evidence.append(f"识别到 {len(doc.references)} 条类似项目/业绩记录")

        records.append(
            {
                **asdict(doc),
                "risk_score": score,
                "risk_level": level,
                "price_gap_pct": round(price_gap * 100, 2),
                "low_gap_pct": round(low_gap * 100, 2),
                "service_ratio_pct": round(service_ratio * 100, 2),
                "install_ratio_pct": round(install_ratio * 100, 2),
                "evidence": evidence,
            }
        )

    records.sort(key=lambda item: item["risk_score"], reverse=True)
    nodes, edges = build_graph(records)
    return {
        "data_root": str(data_dir),
        "project_count": len({r["dataset_project"] for r in records}),
        "bidder_count": len(records),
        "avg_price": round(sum(all_prices) / len(all_prices), 2) if all_prices else None,
        "spread_pct": round((max(all_prices) - min(all_prices)) / (sum(all_prices) / len(all_prices)) * 100, 2)
        if len(all_prices) > 1
        else 0,
        "records": records,
        "projects": build_project_summaries(records),
        "graph": {"nodes": nodes, "edges": edges},
    }


def build_project_summaries(records: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for record in records:
        grouped.setdefault(record["dataset_project"], []).append(record)

    summaries = []
    for project_name, rows in grouped.items():
        prices = [row["price"] for row in rows if row["price"]]
        avg_price = sum(prices) / len(prices) if prices else None
        summaries.append(
            {
                "dataset_project": project_name,
                "display_project": rows[0]["project"] if rows else project_name,
                "bidder_count": len(rows),
                "avg_price": round(avg_price, 2) if avg_price else None,
                "spread_pct": round((max(prices) - min(prices)) / avg_price * 100, 2) if avg_price and len(prices) > 1 else 0,
                "max_risk_score": max((row["risk_score"] for row in rows), default=0),
            }
        )
    summaries.sort(key=lambda item: item["max_risk_score"], reverse=True)
    return summaries


def build_graph(records: list[dict]):
    nodes = []
    edges = []
    seen = set()

    def add_node(node_id: str, label: str, kind: str, risk: float = 0):
        if node_id in seen:
            return
        seen.add(node_id)
        nodes.append({"id": node_id, "label": label, "kind": kind, "risk": risk})

    project = records[0]["project"] if records else "项目"
    add_node("project", project, "项目", 0)
    authors: dict[str, list[str]] = {}
    for record in records:
        bidder_id = "bidder:" + record["bidder"]
        add_node(bidder_id, record["bidder"], "投标人", record["risk_score"])
        edges.append({"source": "project", "target": bidder_id, "label": "参与投标", "weight": 3})
        if record["contact"]:
            contact_id = "contact:" + record["contact"]
            add_node(contact_id, record["contact"], "联系人", 10)
            edges.append({"source": bidder_id, "target": contact_id, "label": "联系人", "weight": 1})
        if record["author"]:
            author_id = "author:" + record["author"]
            add_node(author_id, record["author"], "文件作者", 8)
            edges.append({"source": bidder_id, "target": author_id, "label": "文件作者", "weight": 1})
            authors.setdefault(record["author"], []).append(bidder_id)
        for index, ref in enumerate(record["references"][:2]):
            ref_id = f"ref:{record['bidder']}:{index}"
            add_node(ref_id, ref.replace("项目名称", "")[:18], "业绩", 3)
            edges.append({"source": bidder_id, "target": ref_id, "label": "类似业绩", "weight": 1})

    for linked in authors.values():
        if len(linked) <= 1:
            continue
        for i in range(len(linked)):
            for j in range(i + 1, len(linked)):
                edges.append({"source": linked[i], "target": linked[j], "label": "共同作者线索", "weight": 2})
    return nodes, edges
