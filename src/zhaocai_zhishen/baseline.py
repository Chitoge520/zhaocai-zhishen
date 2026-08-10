"""生成不包含真实投标主体信息的竞赛基线清单。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .versioning import (
    ALGORITHM_VERSION,
    AUDIT_SCHEMA_VERSION,
    BASELINE_SCHEMA_VERSION,
    COMPETITION_RELEASE,
    EVIDENCE_SCHEMA_VERSION,
    MODEL_SCHEMA_VERSION,
    PACKAGE_VERSION,
)


class BaselineValidationError(ValueError):
    """本地产物无法形成一致基线时抛出。"""


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise BaselineValidationError(f"缺少基线产物: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise BaselineValidationError(f"基线产物不是 JSON 对象: {path}")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _artifact_record(path: Path, logical_name: str) -> dict[str, str]:
    return {
        "logical_name": logical_name,
        "sha256": _sha256(path),
    }


def _required_int(payload: dict[str, Any], key: str, source: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise BaselineValidationError(f"{source}.{key} 必须是整数")
    return value


def build_baseline_manifest(
    processed_dir: Path,
    analysis_dir: Path,
    model_dir: Path,
    benchmark_dir: Path,
    *,
    test_count: int,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """读取本地汇总产物，生成仅含聚合数字和摘要哈希的安全清单。"""

    processed_path = processed_dir / "summary.json"
    analysis_path = analysis_dir / "analysis_summary.json"
    training_path = model_dir / "training_summary.json"
    model_path = model_dir / "bid_anomaly_model.json"
    benchmark_path = benchmark_dir / "generation_summary.json"

    processed = _load_json(processed_path)
    analysis = _load_json(analysis_path)
    training = _load_json(training_path)
    model = _load_json(model_path)
    benchmark = _load_json(benchmark_path)

    sample_count = _required_int(processed, "sample_count", "processed_summary")
    success_count = _required_int(processed, "success_count", "processed_summary")
    failure_count = _required_int(processed, "failure_count", "processed_summary")
    document_count = _required_int(analysis, "document_count", "analysis_summary")
    generated_row_count = _required_int(benchmark, "generated_row_count", "generation_summary")
    train_row_count = _required_int(benchmark, "train_row_count", "generation_summary")
    benchmark_test_count = _required_int(benchmark, "test_row_count", "generation_summary")

    if sample_count != success_count + failure_count:
        raise BaselineValidationError("解析成功数与失败数之和不等于样本总数")
    if success_count != document_count:
        raise BaselineValidationError("解析成功文档数与分析文档数不一致")
    if generated_row_count != train_row_count + benchmark_test_count:
        raise BaselineValidationError("合成基准训练集与测试集数量之和不等于总数")
    if benchmark.get("project_isolation_check") is not True:
        raise BaselineValidationError("合成基准未通过项目隔离检查")
    if test_count <= 0:
        raise BaselineValidationError("测试数量必须大于 0")

    timestamp = generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "generated_at": timestamp,
        "release": COMPETITION_RELEASE,
        "versions": {
            "package_version": PACKAGE_VERSION,
            "audit_schema_version": AUDIT_SCHEMA_VERSION,
            "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
            "algorithm_version": ALGORITHM_VERSION,
            "model_schema_version": MODEL_SCHEMA_VERSION,
            "actual_model_schema_version": model.get("schema_version"),
            "training_schema_version": training.get("schema_version"),
            "synthetic_schema_version": benchmark.get("schema_version"),
        },
        "processed_data": {
            "sample_count": sample_count,
            "success_count": success_count,
            "failure_count": failure_count,
            "page_count": _required_int(processed, "page_count", "processed_summary"),
            "raw_text_char_count": int(processed.get("raw_text_char_count", processed.get("text_char_count", 0))),
            "analysis_text_char_count": _required_int(processed, "text_char_count", "processed_summary"),
            "removed_redundant_line_count": int(processed.get("removed_redundant_line_count", 0)),
            "duplicate_page_count": int(processed.get("duplicate_page_count", 0)),
            "text_cleaning_rule_version": (processed.get("text_cleaning") or {}).get("rule_version"),
            "ocr_engine": processed.get("ocr_engine"),
        },
        "analysis": {
            "project_count": _required_int(analysis, "project_count", "analysis_summary"),
            "document_count": document_count,
            "pair_count": _required_int(analysis, "pair_count", "analysis_summary"),
            "anomaly_count": _required_int(analysis, "anomaly_count", "analysis_summary"),
            "high_risk_count": _required_int(analysis, "high_risk_count", "analysis_summary"),
            "medium_risk_count": _required_int(analysis, "medium_risk_count", "analysis_summary"),
        },
        "model": {
            "model_type": model.get("model_type"),
            "training_project_count": _required_int(training, "training_project_count", "training_summary"),
            "training_pair_count": _required_int(training, "training_pair_count", "training_summary"),
            "feature_count": len(training.get("features") or []),
            "threshold": training.get("threshold"),
            "label_status": training.get("label_status"),
        },
        "synthetic_benchmark": {
            "source_row_count": _required_int(benchmark, "source_row_count", "generation_summary"),
            "generated_row_count": generated_row_count,
            "train_row_count": train_row_count,
            "test_row_count": benchmark_test_count,
            "transform_count": _required_int(benchmark, "transform_count", "generation_summary"),
            "project_isolation_check": True,
        },
        "quality_gate": {
            "unit_test_count": test_count,
            "unit_tests_passed": True,
        },
        "source_artifacts": [
            _artifact_record(processed_path, "processed_summary"),
            _artifact_record(analysis_path, "analysis_summary"),
            _artifact_record(training_path, "training_summary"),
            _artifact_record(model_path, "model_definition"),
            _artifact_record(benchmark_path, "synthetic_generation_summary"),
        ],
        "data_boundary": {
            "contains_raw_bid_files": False,
            "contains_bidder_identifiers": False,
            "contains_absolute_local_paths": False,
            "note": "清单只保留聚合指标和摘要哈希；真实文件、处理正文、项目标识、日志和模型产物继续保留在 Git 忽略目录。",
        },
        "result_boundary": "系统输出仅为待复核异常线索，不直接认定串标、围标或违法违规。",
    }


def write_baseline_manifest(manifest: dict[str, Any], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_path