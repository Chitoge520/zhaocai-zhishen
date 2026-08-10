from __future__ import annotations

import csv
import json
import re
import shutil
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from .audit_ingestion import ingest_audit_data, load_coverage_summary
from .datasets.organize_internal_archives import organize
from .document_pipeline import prepare_dataset
from .llm_analysis import run_llm_analysis
from .model_inference import run_model_inference
from .network_analysis import build_network_analysis, load_network_analysis
from .quote_analysis import build_quote_analysis, load_quote_analysis
from .unsupervised_analysis import build_unsupervised_analysis

_GPU_JOB_LOCK = threading.Lock()
_JOB_CREATION_LOCK = threading.Lock()
MAX_PENDING_JOBS = 4
MAX_JOB_SECONDS = 2 * 60 * 60


class JobCancelled(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _write_status(job_dir: Path, **updates) -> dict:
    path = job_dir / "status.json"
    current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    current.update(updates)
    current["updated_at"] = _now()
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)
    return current


def create_job(jobs_root: Path, filename: str, source: BinaryIO, model_path: Path) -> dict:
    jobs_root.mkdir(parents=True, exist_ok=True)
    if Path(filename).suffix.lower() != ".zip":
        raise ValueError("目前只支持 ZIP 项目压缩包")
    with _JOB_CREATION_LOCK:
        pending = [row for row in list_jobs(jobs_root) if row.get("state") in {"queued", "running"}]
        if len(pending) >= MAX_PENDING_JOBS:
            raise ValueError(f"当前已有 {len(pending)} 个任务排队或运行，请等待已有任务完成")
        job_id = uuid.uuid4().hex[:16]
    job_dir = jobs_root / job_id
    raw_dir = job_dir / "raw"
    raw_dir.mkdir(parents=True)
    archive_name = re.sub(r'[<>:"/\\|?*]+', "_", Path(filename).name).strip() or "project.zip"
    archive_path = raw_dir / archive_name
    with archive_path.open("wb") as target:
        shutil.copyfileobj(source, target, length=1024 * 1024)
    status = _write_status(
        job_dir,
        job_id=job_id,
        filename=Path(filename).name,
        state="queued",
        phase="等待处理",
        progress=0,
        created_at=_now(),
        archive_size=archive_path.stat().st_size,
    )
    thread = threading.Thread(
        target=_run_job,
        args=(job_dir, model_path),
        name=f"bid-job-{job_id}",
        daemon=True,
    )
    thread.start()
    return status


def _run_job(job_dir: Path, model_path: Path) -> None:
    deadline = time.monotonic() + MAX_JOB_SECONDS
    try:
        _ensure_job_active(job_dir, deadline)
        _write_status(job_dir, state="running", phase="整理和识别项目文件", progress=10)
        dataset_dir = job_dir / "dataset"
        organize(job_dir / "raw", dataset_dir, extract=True)
        _ensure_job_active(job_dir, deadline)
        _write_status(job_dir, phase="统一多源审计数据层", progress=18)
        audit_ingestion = ingest_audit_data(dataset_dir, job_dir / "audit_ingestion")
        _ensure_job_active(job_dir, deadline)
        _write_status(job_dir, phase="网络、设备和文件元数据关联", progress=23)
        network_analysis = build_network_analysis(job_dir / "audit_ingestion", job_dir / "network_analysis")
        _ensure_job_active(job_dir, deadline)
        _write_status(job_dir, phase="报价聚类与规律性差异分析", progress=27)
        quote_analysis = build_quote_analysis(job_dir / "audit_ingestion", job_dir / "quote_analysis")
        _ensure_job_active(job_dir, deadline)
        standard_dir = dataset_dir / "standard_dataset"
        if not (standard_dir / "samples.csv").exists():
            raise ValueError("压缩包中没有识别到可分析的投标文件")
        with _GPU_JOB_LOCK:
            _ensure_job_active(job_dir, deadline)
            _write_status(job_dir, phase="GPU OCR 和文档解析", progress=30)
            prepare_dataset(
                standard_dir,
                job_dir / "processed",
                ocr_mode="auto",
                ocr_engine_name="auto",
                reuse_cache=True,
                workers=1,
            )
        _ensure_job_active(job_dir, deadline)
        _write_status(job_dir, phase="项目内异常特征分析", progress=68)
        build_unsupervised_analysis(job_dir / "processed", job_dir / "analysis")
        _ensure_job_active(job_dir, deadline)
        _write_status(job_dir, phase="冻结模型推理", progress=84)
        inference = run_model_inference(job_dir / "analysis", model_path, job_dir / "inference")
        _ensure_job_active(job_dir, deadline)
        _write_status(job_dir, phase="大模型辅助复核", progress=92)
        try:
            llm = run_llm_analysis(
                job_dir / "processed",
                job_dir / "analysis",
                job_dir / "inference",
                job_dir / "llm",
            )
        except Exception as exc:
            llm = {
                "status": "failed",
                "reason": f"{type(exc).__name__}: {exc}",
                "findings": [],
                "validated_finding_count": 0,
            }
            (job_dir / "llm").mkdir(parents=True, exist_ok=True)
            (job_dir / "llm" / "llm_analysis.json").write_text(
                json.dumps(llm, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        _write_status(
            job_dir,
            state="completed",
            phase="分析完成",
            progress=100,
            result={"inference": inference, "llm": llm, "audit_ingestion": audit_ingestion, "network_analysis": network_analysis, "quote_analysis": quote_analysis},
        )
    except Exception as exc:
        (job_dir / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
        if isinstance(exc, JobCancelled):
            _write_status(job_dir, state="cancelled", phase="已取消", progress=100, error=str(exc))
            return
        _write_status(
            job_dir,
            state="failed",
            phase="处理失败",
            progress=100,
            error=f"{type(exc).__name__}: {exc}",
        )


def _ensure_job_active(job_dir: Path, deadline: float) -> None:
    if time.monotonic() > deadline:
        raise TimeoutError(f"任务处理超过 {MAX_JOB_SECONDS // 3600} 小时限制")
    status_path = job_dir / "status.json"
    if status_path.exists():
        status = json.loads(status_path.read_text(encoding="utf-8"))
        if status.get("cancel_requested"):
            raise JobCancelled("用户已请求取消任务")


def cancel_job(jobs_root: Path, job_id: str) -> dict | None:
    status = get_job(jobs_root, job_id)
    if not status or status.get("state") not in {"queued", "running"}:
        return status
    return _write_status(jobs_root / job_id, cancel_requested=True, phase="等待取消")


def get_job(jobs_root: Path, job_id: str) -> dict | None:
    if not job_id or any(character not in "0123456789abcdef" for character in job_id.lower()):
        return None
    path = jobs_root / job_id / "status.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def list_jobs(jobs_root: Path, limit: int = 20) -> list[dict]:
    if not jobs_root.exists():
        return []
    rows = []
    for directory in jobs_root.iterdir():
        if directory.is_dir():
            status = get_job(jobs_root, directory.name)
            if status:
                rows.append(status)
    rows.sort(key=lambda row: row.get("created_at", ""), reverse=True)
    return rows[:limit]


def load_job_results(jobs_root: Path, job_id: str) -> dict | None:
    status = get_job(jobs_root, job_id)
    if not status or status.get("state") != "completed":
        return None
    job_dir = jobs_root / job_id
    from .analysis_results import load_unsupervised_results

    scored_path = job_dir / "inference" / "model_scored_pairs.csv"
    scored = []
    if scored_path.exists():
        with scored_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                row["model_score"] = float(row.get("model_score") or 0)
                row["model_threshold"] = float(row.get("model_threshold") or 0)
                row["model_triggered"] = str(row.get("model_triggered", "")).lower() == "true"
                try:
                    row["model_zscores"] = json.loads(row.get("model_zscores") or "{}")
                except json.JSONDecodeError:
                    row["model_zscores"] = {}
                scored.append(row)
    analysis = load_unsupervised_results(job_dir / "analysis", model_pairs=scored)
    llm_path = job_dir / "llm" / "llm_analysis.json"
    coverage = load_coverage_summary(job_dir / "audit_ingestion" / "coverage_summary.json")
    network = load_network_analysis(job_dir / "network_analysis")
    quotes = load_quote_analysis(job_dir / "quote_analysis")
    llm = json.loads(llm_path.read_text(encoding="utf-8")) if llm_path.exists() else {
        "status": "skipped",
        "findings": [],
        "validated_finding_count": 0,
    }
    return {
        "job": status,
        "analysis": analysis,
        "model_pairs": scored,
        "model_triggered": [row for row in scored if row["model_triggered"]],
        "llm": llm,
        "audit_coverage": coverage,
        "network_analysis": network,
        "quote_analysis": quotes,
    }
