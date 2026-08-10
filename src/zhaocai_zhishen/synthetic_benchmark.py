from __future__ import annotations

import csv
import hashlib
import json
import math
import random
from collections import Counter, defaultdict
from pathlib import Path


SCHEMA_VERSION = "bid-synthetic-benchmark/v2"
TRANSFORMS = (
    "clean_pair",
    "shared_phone",
    "shared_address",
    "shared_email",
    "shared_credit_code",
    "copied_custom_error",
    "high_text_similarity",
    "multi_signal",
    "same_file_author",
    "same_network_fingerprint",
    "same_bid_account",
    "mixed_bid_documents",
    "price_pattern",
    "coordinated_price_pattern",
)

_LIST_FIELDS = (
    "shared_phones",
    "shared_emails",
    "shared_credit_codes",
    "shared_contacts",
    "shared_addresses",
    "shared_file_authors",
    "shared_network_fingerprints",
    "shared_bid_accounts",
    "mixed_bid_documents",
)
_BASE_COLUMNS = (
    "project_id",
    "document_id_a",
    "document_id_b",
    "bidder_a",
    "bidder_b",
    "same_bidder",
    "similarity",
    "shared_phones",
    "shared_emails",
    "shared_credit_codes",
    "shared_contacts",
    "shared_addresses",
    "shared_file_authors",
    "shared_network_fingerprints",
    "shared_bid_accounts",
    "mixed_bid_documents",
    "repeated_segment_count",
    "repeated_segment_chars",
    "repeated_segments",
    "price_pattern_score",
    "price_deviation_span",
    "anomaly_score",
)

SCENARIO_DEFINITIONS = {
    "same_file_author": "同一项目内不同投标文件的作者或创建者元数据一致",
    "same_network_fingerprint": "同一项目内不同投标文件出现相同网络或设备指纹",
    "same_bid_account": "同一项目内不同投标人出现相同收款或投标账户线索",
    "mixed_bid_documents": "同一项目内投标文件出现混装、错放或跨单位文件痕迹",
    "price_pattern": "同一项目内报价偏差呈现异常一致的模式",
    "coordinated_price_pattern": "同一项目内多份报价呈现协同式阶梯或固定差额模式",
}
_METADATA_COLUMNS = (
    "dataset_type",
    "synthetic_label",
    "synthetic_is_positive",
    "transform_type",
    "evidence_fields",
    "source_project_id",
    "source_document_id_a",
    "source_document_id_b",
    "source_row_id",
    "split",
    "generation_version",
)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: list[dict[str, str]], fieldnames: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_jsonl(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return default


def _json_list(value: object) -> list:
    if isinstance(value, list):
        return value
    try:
        result = json.loads(str(value or "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    return result if isinstance(result, list) else []


def _json_dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _source_row_id(row: dict[str, str], index: int) -> str:
    payload = json.dumps(
        {key: row.get(key, "") for key in sorted(row)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:12]
    return f"{row.get('project_id', 'unknown')}__{index:04d}__{digest}"


def _synthetic_token(source_row_id: str, transform: str) -> str:
    digest = hashlib.sha256(f"{source_row_id}:{transform}".encode("utf-8")).hexdigest()[:10]
    return f"SYN_{transform.upper()}_{digest}"


def _clean_control(row: dict[str, str]) -> dict[str, str]:
    """Create a known-low-signal control; it is not a claim about the source pair."""
    result = dict(row)
    similarity = _number(row.get("similarity"))
    result["similarity"] = f"{min(0.30, max(0.08, similarity * 0.5)):.6f}"
    for field in _LIST_FIELDS:
        result[field] = "[]"
    result["repeated_segment_count"] = "0"
    result["repeated_segment_chars"] = "0"
    result["repeated_segments"] = "[]"
    result["price_pattern_score"] = "0"
    result["price_deviation_span"] = "0"
    result["anomaly_score"] = "0"
    return result


def _apply_transform(
    row: dict[str, str],
    transform: str,
    source_row_id: str,
) -> tuple[dict[str, str], list[str]]:
    result = _clean_control(row)
    token = _synthetic_token(source_row_id, transform)
    evidence_fields: list[str] = []

    if transform == "shared_phone":
        result["shared_phones"] = _json_dump([token])
        evidence_fields.append("shared_phones")
    elif transform == "shared_address":
        result["shared_addresses"] = _json_dump([f"合成地址_{token}"])
        evidence_fields.append("shared_addresses")
    elif transform == "shared_email":
        result["shared_emails"] = _json_dump([f"{token.lower()}@example.invalid"])
        evidence_fields.append("shared_emails")
    elif transform == "shared_credit_code":
        result["shared_credit_codes"] = _json_dump([token])
        evidence_fields.append("shared_credit_codes")
    elif transform == "copied_custom_error":
        result["similarity"] = "0.880000"
        result["repeated_segment_count"] = "6"
        result["repeated_segment_chars"] = "720"
        result["repeated_segments"] = _json_dump([f"合成复制片段_{token}"])
        evidence_fields.extend(("similarity", "repeated_segments"))
    elif transform == "high_text_similarity":
        result["similarity"] = "0.950000"
        result["repeated_segment_count"] = "3"
        result["repeated_segment_chars"] = "480"
        result["repeated_segments"] = _json_dump([f"合成高相似片段_{token}"])
        evidence_fields.extend(("similarity", "repeated_segments"))
    elif transform == "multi_signal":
        result["similarity"] = "0.900000"
        result["shared_phones"] = _json_dump([f"{token}_PHONE"])
        result["shared_addresses"] = _json_dump([f"合成地址_{token}"])
        result["shared_emails"] = _json_dump([f"{token.lower()}@example.invalid"])
        evidence_fields.extend(("similarity", "shared_phones", "shared_addresses", "shared_emails"))
    elif transform == "same_file_author":
        result["shared_file_authors"] = _json_dump([f"{token}_AUTHOR"])
        evidence_fields.append("shared_file_authors")
    elif transform == "same_network_fingerprint":
        result["shared_network_fingerprints"] = _json_dump([f"{token}_IP", f"{token}_DEVICE"])
        evidence_fields.append("shared_network_fingerprints")
    elif transform == "same_bid_account":
        result["shared_bid_accounts"] = _json_dump([f"{token}_ACCOUNT"])
        evidence_fields.append("shared_bid_accounts")
    elif transform == "mixed_bid_documents":
        result["mixed_bid_documents"] = _json_dump([f"{token}_AUTHOR_FIELD", f"{token}_CERTIFICATE"])
        evidence_fields.append("mixed_bid_documents")
    elif transform == "price_pattern":
        result["price_pattern_score"] = "0.960000"
        result["price_deviation_span"] = "0.030000"
        evidence_fields.extend(("price_pattern_score", "price_deviation_span"))
    elif transform == "coordinated_price_pattern":
        result["price_pattern_score"] = "0.900000"
        result["price_deviation_span"] = "0.010000"
        evidence_fields.extend(("price_pattern_score", "price_deviation_span"))
    elif transform != "clean_pair":
        raise ValueError(f"未知合成变换：{transform}")

    return result, evidence_fields


def split_projects(
    projects: list[str], test_fraction: float = 0.25, seed: int = 20260808
) -> tuple[list[str], list[str]]:
    unique = sorted({project for project in projects if project})
    if len(unique) < 2:
        raise ValueError("至少需要 2 个项目才能划分训练集和测试集")
    if not 0 < test_fraction < 1:
        raise ValueError("test_fraction 必须在 0 和 1 之间")
    shuffled = list(unique)
    random.Random(seed).shuffle(shuffled)
    test_count = max(1, min(len(unique) - 1, math.ceil(len(unique) * test_fraction)))
    test_projects = sorted(shuffled[:test_count])
    train_projects = sorted(shuffled[test_count:])
    return train_projects, test_projects


def _decorate(
    row: dict[str, str],
    source: dict[str, str],
    source_row_id: str,
    transform: str,
    split: str,
    evidence_fields: list[str],
) -> dict[str, str]:
    result = {key: row.get(key, "") for key in _BASE_COLUMNS}
    result.update(
        {
            "dataset_type": "synthetic_benchmark",
            "synthetic_label": "clean_pair" if transform == "clean_pair" else transform,
            "synthetic_is_positive": "0" if transform == "clean_pair" else "1",
            "transform_type": transform,
            "evidence_fields": _json_dump(evidence_fields),
            "source_project_id": source.get("project_id", ""),
            "source_document_id_a": source.get("document_id_a", ""),
            "source_document_id_b": source.get("document_id_b", ""),
            "source_row_id": source_row_id,
            "split": split,
            "generation_version": SCHEMA_VERSION,
        }
    )
    return result


def generate_benchmark(
    analysis_csv: Path,
    output_dir: Path,
    test_fraction: float = 0.25,
    seed: int = 20260808,
) -> dict:
    analysis_csv = analysis_csv.resolve()
    output_dir = output_dir.resolve()
    source_rows = [
        row
        for row in _read_csv(analysis_csv)
        if row.get("project_id") and str(row.get("same_bidder", "")).lower() != "true"
    ]
    if not source_rows:
        raise ValueError(f"分析文件没有可用的跨投标人两两样本：{analysis_csv}")

    train_projects, test_projects = split_projects(
        [row.get("project_id", "") for row in source_rows], test_fraction=test_fraction, seed=seed
    )
    test_project_set = set(test_projects)
    generated: list[dict[str, str]] = []
    source_counts: Counter[str] = Counter()
    for index, source in enumerate(source_rows, 1):
        source_row_id = _source_row_id(source, index)
        split = "test" if source.get("project_id") in test_project_set else "train"
        for transform in TRANSFORMS:
            transformed, evidence_fields = _apply_transform(source, transform, source_row_id)
            generated.append(_decorate(transformed, source, source_row_id, transform, split, evidence_fields))
        source_counts[source.get("project_id", "")] += 1

    fieldnames = _BASE_COLUMNS + _METADATA_COLUMNS
    train_rows = [row for row in generated if row["split"] == "train"]
    test_rows = [row for row in generated if row["split"] == "test"]
    _write_csv(output_dir / "manifest.csv", generated, fieldnames)
    _write_csv(output_dir / "train" / "pairs.csv", train_rows, fieldnames)
    _write_csv(output_dir / "test" / "pairs.csv", test_rows, fieldnames)
    _write_jsonl(output_dir / "train" / "pairs.jsonl", train_rows)
    _write_jsonl(output_dir / "test" / "pairs.jsonl", test_rows)

    counts_by_split = {
        split: dict(Counter(row["transform_type"] for row in rows))
        for split, rows in (("train", train_rows), ("test", test_rows))
    }
    project_rows: dict[str, dict[str, int]] = defaultdict(lambda: {"source_rows": 0, "generated_rows": 0})
    for project, count in source_counts.items():
        project_rows[project]["source_rows"] = count
        project_rows[project]["generated_rows"] = count * len(TRANSFORMS)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "dataset_type": "synthetic_benchmark",
        "source_analysis_csv": str(analysis_csv),
        "source_row_count": len(source_rows),
        "generated_row_count": len(generated),
        "train_row_count": len(train_rows),
        "test_row_count": len(test_rows),
        "transform_count": len(TRANSFORMS),
        "transforms": list(TRANSFORMS),
        "scenario_definitions": SCENARIO_DEFINITIONS,
        "seed": seed,
        "test_fraction": test_fraction,
        "train_projects": train_projects,
        "test_projects": test_projects,
        "project_isolation_check": not (set(train_projects) & set(test_projects)),
        "counts_by_split": counts_by_split,
        "projects": dict(sorted(project_rows.items())),
        "label_warning": "synthetic_is_positive 仅表示受控变换，不代表真实串标、围标或违法违规结论。",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "generation_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def load_benchmark_rows(path: Path) -> list[dict[str, str]]:
    """Load either a benchmark CSV or a benchmark directory."""
    target = path / "test" / "pairs.csv" if path.is_dir() else path
    return _read_csv(target)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="生成可追溯的招投标合成基准训练/测试集")
    parser.add_argument("--analysis", type=Path, default=Path("data/analysis/pairwise_similarity.csv"))
    parser.add_argument("--output", type=Path, default=Path("data/synthetic_benchmark"))
    parser.add_argument("--test-fraction", type=float, default=0.25)
    parser.add_argument("--seed", type=int, default=20260808)
    args = parser.parse_args()
    print(json.dumps(generate_benchmark(args.analysis, args.output, args.test_fraction, args.seed), ensure_ascii=False, indent=2))
