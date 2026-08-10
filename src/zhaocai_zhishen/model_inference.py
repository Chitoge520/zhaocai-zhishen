from __future__ import annotations

import csv
import json
from pathlib import Path

from .model_training import load_model, score_pair


def run_model_inference(analysis_dir: Path, model_path: Path, output_dir: Path) -> dict:
    analysis_dir, model_path, output_dir = analysis_dir.resolve(), model_path.resolve(), output_dir.resolve()
    model = load_model(model_path)
    if not model:
        raise FileNotFoundError(f"未找到模型文件：{model_path}")
    with (analysis_dir / "pairwise_similarity.csv").open("r", encoding="utf-8-sig", newline="") as handle:
        pairs = list(csv.DictReader(handle))
    output_dir.mkdir(parents=True, exist_ok=True)
    scored = []
    for row in pairs:
        if str(row.get("same_bidder", "")).lower() == "true":
            continue
        result = score_pair(row, model)
        scored.append({**row, "model_score": result["model_score"], "model_threshold": result["model_threshold"], "model_triggered": result["model_score"] >= result["model_threshold"], "model_zscores": json.dumps(result["model_zscores"], ensure_ascii=False)})
    scored.sort(key=lambda row: float(row["model_score"]), reverse=True)
    fieldnames = list(scored[0].keys()) if scored else ["project_id", "document_id_a", "document_id_b", "model_score", "model_threshold", "model_triggered", "model_zscores"]
    with (output_dir / "model_scored_pairs.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(scored)
    summary = {"schema_version": "bid-anomaly-inference/v1", "analysis_dir": str(analysis_dir), "model_path": str(model_path), "pair_count": len(scored), "triggered_count": sum(bool(row["model_triggered"]) for row in scored), "threshold": model.get("threshold", 40.0), "warning": "模型结果仅为待复核异常线索，不等于违规结论。"}
    (output_dir / "inference_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary