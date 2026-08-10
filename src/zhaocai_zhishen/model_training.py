from __future__ import annotations

import csv
import json
import math
import statistics
from pathlib import Path

FEATURES = (
    "similarity",
    "shared_phones_count",
    "shared_emails_count",
    "shared_credit_codes_count",
    "shared_contacts_count",
    "shared_addresses_count",
    "shared_file_authors_count",
    "shared_network_fingerprints_count",
    "shared_bid_accounts_count",
    "mixed_bid_documents_count",
    "price_pattern_score",
    "price_deviation_span",
    "shared_entity_type_count",
    "repeated_segment_count",
    "repeated_segment_chars_k",
)
WEIGHTS = {
    "similarity": 1.5,
    "shared_phones_count": 2.0,
    "shared_emails_count": 2.0,
    "shared_credit_codes_count": 2.0,
    "shared_contacts_count": 1.0,
    "shared_addresses_count": 1.0,
    "shared_file_authors_count": 3.0,
    "shared_network_fingerprints_count": 3.0,
    "shared_bid_accounts_count": 3.0,
    "mixed_bid_documents_count": 3.0,
    "price_pattern_score": 3.0,
    "price_deviation_span": 1.0,
    "shared_entity_type_count": 1.0,
    # 合同模板和法定格式会造成大量正常重复，重复片段只保留在证据中，
    # 不参与冻结模型的主要异常判断。
    "repeated_segment_count": 0.0,
    "repeated_segment_chars_k": 0.0,
}


def _json_list(value: object) -> list:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def pair_features(row: dict) -> dict[str, float]:
    values = {
        "similarity": float(row.get("similarity") or 0),
        "shared_phones_count": float(len(_json_list(row.get("shared_phones")))),
        "shared_emails_count": float(len(_json_list(row.get("shared_emails")))),
        "shared_credit_codes_count": float(len(_json_list(row.get("shared_credit_codes")))),
        "shared_contacts_count": float(len(_json_list(row.get("shared_contacts")))),
        "shared_addresses_count": float(len(_json_list(row.get("shared_addresses")))),
        "shared_file_authors_count": float(len(_json_list(row.get("shared_file_authors")))),
        "shared_network_fingerprints_count": float(len(_json_list(row.get("shared_network_fingerprints")))),
        "shared_bid_accounts_count": float(len(_json_list(row.get("shared_bid_accounts")))),
        "mixed_bid_documents_count": float(len(_json_list(row.get("mixed_bid_documents")))),
        "price_pattern_score": float(row.get("price_pattern_score") or 0),
        "price_deviation_span": float(row.get("price_deviation_span") or 0),
        "repeated_segment_count": float(row.get("repeated_segment_count") or 0),
        "repeated_segment_chars_k": float(row.get("repeated_segment_chars") or 0) / 1000.0,
    }
    values["shared_entity_type_count"] = float(sum(values[name] > 0 for name in FEATURES[1:6]))
    return values


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def fit_model(rows: list[dict], projects: list[str]) -> dict:
    project_set = set(projects)
    selected = [row for row in rows if row.get("project_id", "") in project_set and str(row.get("same_bidder", "")).lower() != "true"]
    stats: dict[str, dict[str, float]] = {}
    for name in FEATURES:
        values = [pair_features(row)[name] for row in selected]
        median = statistics.median(values) if values else 0.0
        deviations = [abs(value - median) for value in values]
        mad = statistics.median(deviations) if deviations else 0.0
        scale = 1.4826 * mad
        if scale < 0.01:
            scale = max(statistics.pstdev(values) if len(values) > 1 else 0.0, 1.0)
        stats[name] = {"median": round(median, 8), "scale": round(scale, 8)}
    model = {
        "schema_version": "bid-anomaly-model/v1",
        "model_type": "robust_unsupervised_pairwise",
        "training_project_count": len(project_set),
        "training_pair_count": len(selected),
        "features": list(FEATURES),
        "weights": WEIGHTS,
        "stats": stats,
        "threshold": 0.0,
        "warning": "无监督异常分数不是违规概率，必须结合原文证据和人工复核。",
    }
    scores = [score_pair(row, model)["model_score"] for row in selected]
    model["threshold"] = round(max(40.0, _percentile(scores, 0.95)), 4) if scores else 40.0
    return model


def score_pair(row: dict, model: dict) -> dict:
    values = pair_features(row)
    raw = 0.0
    zscores: dict[str, float] = {}
    for name in model.get("features", FEATURES):
        stat = model.get("stats", {}).get(name, {"median": 0.0, "scale": 1.0})
        scale = max(float(stat.get("scale", 1.0)), 0.01)
        z = max(0.0, (values[name] - float(stat.get("median", 0.0))) / scale)
        zscores[name] = round(z, 4)
        raw += z * float(model.get("weights", WEIGHTS).get(name, 1.0))
    score = 100.0 * (1.0 - math.exp(-raw / 5.0))
    return {"model_score": round(score, 2), "model_threshold": float(model.get("threshold", 40.0)), "model_zscores": zscores}


def _load_pairs(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def evaluate_synthetic_benchmark(benchmark_dir: Path, model: dict) -> dict:
    """Evaluate only controlled synthetic labels; never present them as real-case accuracy."""
    from .synthetic_benchmark import load_benchmark_rows

    rows = load_benchmark_rows(benchmark_dir)
    if not rows:
        raise ValueError(f"合成基准测试集为空：{benchmark_dir}")
    metadata = {}
    summary_path = benchmark_dir / "generation_summary.json"
    if summary_path.exists():
        try:
            metadata = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            metadata = {}
    scored_rows = []
    for row in rows:
        label = 1 if str(row.get("synthetic_is_positive", "0")) == "1" else 0
        score = score_pair(row, model)["model_score"]
        predicted = 1 if score >= float(model.get("threshold", 40.0)) else 0
        scored_rows.append((row, label, predicted, score))

    true_positive = sum(label == predicted == 1 for _, label, predicted, _ in scored_rows)
    false_positive = sum(label == 0 and predicted == 1 for _, label, predicted, _ in scored_rows)
    false_negative = sum(label == 1 and predicted == 0 for _, label, predicted, _ in scored_rows)
    true_negative = sum(label == predicted == 0 for _, label, predicted, _ in scored_rows)
    positive_count = true_positive + false_negative
    negative_count = true_negative + false_positive
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    recall = true_positive / positive_count if positive_count else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    specificity = true_negative / negative_count if negative_count else 0.0
    by_transform: dict[str, dict[str, float | int | str]] = {}
    for transform in sorted({row.get("transform_type", "") for row, *_ in scored_rows}):
        subset = [item for item in scored_rows if item[0].get("transform_type") == transform]
        positives = sum(label == 1 for _, label, _, _ in subset)
        detected = sum(label == 1 and predicted == 1 for _, label, predicted, _ in subset)
        controls = sum(label == 0 for _, label, _, _ in subset)
        control_flags = sum(label == 0 and predicted == 1 for _, label, predicted, _ in subset)
        by_transform[transform] = {
            "count": len(subset),
            "positive_count": positives,
            "detected_count": detected,
            "detection_rate": round(detected / positives, 4) if positives else 0.0,
            "control_count": controls,
            "control_false_positive_count": control_flags,
            "control_false_positive_rate": round(control_flags / controls, 4) if controls else 0.0,
            "score_min": round(min(score for _, _, _, score in subset), 2),
            "score_max": round(max(score for _, _, _, score in subset), 2),
        }
    return {
        "evaluation_type": "synthetic_controlled_benchmark",
        "status": "completed",
        "row_count": len(scored_rows),
        "source_row_count": int(metadata.get("source_row_count", 0) or 0),
        "train_row_count": int(metadata.get("train_row_count", 0) or 0),
        "train_project_count": len(metadata.get("train_projects", []) or []),
        "test_project_count": len(metadata.get("test_projects", []) or []),
        "threshold": float(model.get("threshold", 40.0)),
        "confusion_matrix": {
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "true_negative": true_negative,
        },
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "specificity": round(specificity, 4),
        "false_positive_rate": round(1.0 - specificity, 4),
        "by_transform": by_transform,
        "warning": "该指标只衡量受控合成变换是否被模型排序到阈值以上，不代表真实串标、围标识别准确率。",
    }


def train_model(analysis_dir: Path, model_dir: Path, folds: int = 5, benchmark_dir: Path | None = None) -> dict:
    analysis_dir, model_dir = analysis_dir.resolve(), model_dir.resolve()
    model_dir.mkdir(parents=True, exist_ok=True)
    rows = _load_pairs(analysis_dir / "pairwise_similarity.csv")
    projects = sorted({row.get("project_id", "") for row in rows if row.get("project_id")})
    if len(projects) < 3:
        raise ValueError("至少需要 3 个项目才能进行项目级交叉验证")
    folds = max(2, min(folds, len(projects)))
    fold_rows = []
    for fold in range(folds):
        validation_projects = [project for index, project in enumerate(projects) if index % folds == fold]
        training_projects = [project for project in projects if project not in validation_projects]
        model = fit_model(rows, training_projects)
        validation = [row for row in rows if row.get("project_id") in set(validation_projects) and str(row.get("same_bidder", "")).lower() != "true"]
        scored = [score_pair(row, model)["model_score"] for row in validation]
        fold_rows.append({"fold": fold + 1, "training_projects": len(training_projects), "validation_projects": len(validation_projects), "validation_pairs": len(validation), "threshold": model["threshold"], "validation_above_threshold": sum(score >= model["threshold"] for score in scored), "validation_max_score": round(max(scored), 2) if scored else 0.0})
    final_model = fit_model(rows, projects)
    final_model["project_ids"] = projects
    final_model["cross_validation"] = fold_rows
    final_model["training_source"] = str(analysis_dir)
    final_rows = [
        row for row in rows
        if str(row.get("same_bidder", "")).lower() != "true"
    ]
    final_scores = [score_pair(row, final_model)["model_score"] for row in final_rows]
    feature_coverage = {
        name: sum(pair_features(row)[name] > 0 for row in final_rows)
        for name in FEATURES
    }
    final_model["evaluation"] = {
        "status": "unlabeled_no_accuracy_metrics",
        "note": "当前没有项目级复核标签，只能报告异常排序分布和特征覆盖率，不能计算准确率、召回率或违规概率。",
        "score_distribution": {
            "min": round(min(final_scores), 2) if final_scores else 0.0,
            "median": round(statistics.median(final_scores), 2) if final_scores else 0.0,
            "p95": round(_percentile(final_scores, 0.95), 2) if final_scores else 0.0,
            "max": round(max(final_scores), 2) if final_scores else 0.0,
        },
        "feature_coverage": feature_coverage,
    }
    synthetic_evaluation = evaluate_synthetic_benchmark(benchmark_dir, final_model) if benchmark_dir else None
    if synthetic_evaluation:
        final_model["synthetic_benchmark_evaluation"] = synthetic_evaluation
    model_path = model_dir / "bid_anomaly_model.json"
    model_path.write_text(json.dumps(final_model, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "schema_version": "bid-anomaly-training/v2",
        "model_path": str(model_path),
        "project_count": len(projects),
        "pair_count": len(final_rows),
        "features": list(FEATURES),
        "folds": fold_rows,
        "fold_count": len(fold_rows),
        "training_project_count": len(projects),
        "training_pair_count": len(final_rows),
        "threshold": final_model["threshold"],
        "label_status": "unlabeled_unsupervised",
        "evaluation": final_model["evaluation"],
        "synthetic_benchmark": synthetic_evaluation,
        "reliability_note": "当前结果只能作为异常线索排序，不能推导违规概率或确认串标围标。",
    }
    (model_dir / "training_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    evaluation_summary = dict(final_model["evaluation"])
    if synthetic_evaluation:
        evaluation_summary["synthetic_benchmark"] = synthetic_evaluation
    (model_dir / "evaluation_summary.json").write_text(
        json.dumps(evaluation_summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


def load_model(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
