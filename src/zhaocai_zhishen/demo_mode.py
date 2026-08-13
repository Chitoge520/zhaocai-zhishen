from __future__ import annotations

import json
from pathlib import Path

from .analysis_results import load_unsupervised_results
from .competition_demo import build_fully_desensitized_demo_snapshot


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _count_gpu_documents(processed_dir: Path) -> int:
    cache_dir = processed_dir / "cache"
    if not cache_dir.exists():
        return 0
    return sum(1 for path in cache_dir.glob("*.json") if "paddle-gpu" in path.name)


def load_demo_snapshot(data_root: Path, analysis_dir: Path, *, fully_desensitized: bool = False) -> dict:
    """Load the prepared local results used by the competition demo.

    The demo is deliberately read-only: it never invokes OCR, model training, or
    the LLM. This keeps the showcase deterministic and usable without network.
    """

    if fully_desensitized:
        return build_fully_desensitized_demo_snapshot()

    data_root = data_root.resolve()
    training = _read_json(data_root / "training_internal" / "summary.json")
    processed = _read_json(data_root / "processed" / "summary.json")
    analysis = load_unsupervised_results(
        analysis_dir,
        model_path=data_root / "models" / "bid_anomaly_model.json",
    )
    model = _read_json(data_root / "models" / "bid_anomaly_model.json")
    model_summary = _read_json(data_root / "models" / "training_summary.json")
    analysis_summary = analysis.get("summary", {})
    ready = bool(training and processed and analysis.get("ready") and model and model_summary)

    metrics = {
        "archive_count": int(training.get("archive_count", 0) or 0),
        "file_count": int(training.get("file_count", 0) or 0),
        "bid_document_count": int(processed.get("success_count", 0) or analysis_summary.get("document_count", 0) or 0),
        "page_count": int(processed.get("page_count", 0) or 0),
        "gpu_ocr_document_count": _count_gpu_documents(data_root / "processed"),
        "comparable_project_count": int(analysis_summary.get("comparable_project_count", 0) or 0),
        "pair_count": int(analysis_summary.get("pair_count", 0) or 0),
        "anomaly_count": int(analysis_summary.get("anomaly_count", 0) or len(analysis.get("anomalies", []))),
    }
    return {
        "ready": ready,
        "mode": "offline-demo",
        "schema_version": "competition-offline-demo/v1",
        "source": "local_precomputed_artifacts",
        "fully_desensitized": False,
        "fictional": False,
        "title": "历史基线离线演示案例",
        "message": (
            "演示数据已从本地预计算结果加载，不会触发 OCR、训练或大模型调用。"
            if ready
            else "演示数据尚未准备完整，请先完成历史数据处理和模型训练。"
        ),
        "metrics": metrics,
        "training": training,
        "processed": processed,
        "analysis": analysis,
        "model": {"ready": bool(model), "model": model, "summary": model_summary},
    }
