from __future__ import annotations

import cgi
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from .analysis import build_analysis
from .analysis_results import load_unsupervised_results
from .audit_ingestion import load_coverage_summary
from .config import load_settings
from .demo_mode import load_demo_snapshot
from .evidence_graph import load_evidence_graph
from .evidence_replay import load_evidence_detail
from .job_manager import cancel_job, create_job, get_job, list_jobs, load_job_results
from .llm_analysis import get_llm_config, validate_llm_base_url
from .network_analysis import load_network_analysis
from .quote_analysis import load_quote_analysis
from .reporting import build_docx_report, build_report_payload, render_html_report

settings = load_settings()
STATIC_DIR = Path(__file__).resolve().parent / "static"
DATA_ROOT = settings.data_dir
JOBS_ROOT = DATA_ROOT / "jobs"
MODEL_PATH = DATA_ROOT / "models" / "bid_anomaly_model.json"
MAX_UPLOAD_BYTES = 4 * 1024 * 1024 * 1024
ALLOWED_LLM_MODELS = {"deepseek-chat", "deepseek-reasoner", "deepseek-v4-pro", "deepseek-v4-flash"}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, content_type: str, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload, status: int = 200):
        self._send(
            status,
            "application/json; charset=utf-8",
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )

    def _send_file(self, body: bytes, content_type: str, filename: str, *, inline: bool = False):
        disposition = "inline" if inline else "attachment"
        safe_name = quote(filename)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f"{disposition}; filename*=UTF-8''{safe_name}")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _read_llm_result(llm_path: Path) -> dict:
        if not llm_path.exists():
            return {"status": "skipped", "findings": [], "validated_finding_count": 0}
        try:
            value = json.loads(llm_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {"status": "unreadable", "findings": [], "validated_finding_count": 0}
        return value if isinstance(value, dict) else {"status": "unreadable", "findings": [], "validated_finding_count": 0}

    def _send_report(self, analysis_dir: Path, processed_dir: Path, training_dir: Path, models_dir: Path, *,
                     format_name: str, title: str, llm_path: Path):
        payload = build_report_payload(
            analysis_dir,
            processed_dir,
            training_dir,
            models_dir,
            title=title,
            llm=self._read_llm_result(llm_path),
        )
        if format_name == "html":
            self._send_file(render_html_report(payload), "text/html; charset=utf-8", "招采智审异常线索复核报告.html", inline=True)
        else:
            self._send_file(
                build_docx_report(payload),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "招采智审异常线索复核报告.docx",
            )

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/analysis":
            self._send_json(build_analysis(settings.data_dir))
            return
        if path == "/api/unsupervised":
            self._send_json(load_unsupervised_results(settings.analysis_dir, model_path=MODEL_PATH))
            return
        if path == "/api/demo":
            self._send_json(load_demo_snapshot(DATA_ROOT, settings.analysis_dir))
            return
        if path == "/api/network-analysis":
            self._send_json(load_network_analysis(DATA_ROOT / "network_analysis"))
            return
        if path == "/api/quote-analysis":
            self._send_json(load_quote_analysis(DATA_ROOT / "quote_analysis"))
            return
        if path == "/api/evidence-graph":
            self._send_json(load_evidence_graph(settings.analysis_dir, model_path=MODEL_PATH))
            return
        if path == "/api/report.html":
            self._send_report(
                settings.analysis_dir,
                DATA_ROOT / "processed",
                DATA_ROOT / "training_internal",
                DATA_ROOT / "models",
                format_name="html",
                title="招采智审历史项目异常线索复核报告",
                llm_path=DATA_ROOT / "llm" / "llm_analysis.json",
            )
            return
        if path == "/api/report.docx":
            self._send_report(
                settings.analysis_dir,
                DATA_ROOT / "processed",
                DATA_ROOT / "training_internal",
                DATA_ROOT / "models",
                format_name="docx",
                title="招采智审历史项目异常线索复核报告",
                llm_path=DATA_ROOT / "llm" / "llm_analysis.json",
            )
            return
        if path.startswith("/api/findings/"):
            finding_id = unquote(path.rsplit("/", 1)[-1])
            detail = load_evidence_detail(settings.analysis_dir, DATA_ROOT / "processed", finding_id)
            self._send_json(detail if detail else {"error": "异常线索不存在"}, 200 if detail else 404)
            return
        if path.startswith("/api/projects/jobs/") and "/findings/" in path:
            parts = path.strip("/").split("/")
            if len(parts) >= 6:
                job_id, finding_id = parts[3], unquote(parts[5])
                job = get_job(JOBS_ROOT, job_id)
                detail = None
                if job and job.get("state") == "completed":
                    detail = load_evidence_detail(JOBS_ROOT / job_id / "analysis", JOBS_ROOT / job_id / "processed", finding_id)
                self._send_json(detail if detail else {"error": "任务或异常线索不存在"}, 200 if detail else 404)
                return
        if path.startswith("/api/projects/jobs/") and path.endswith("/report.html"):
            parts = path.strip("/").split("/")
            if len(parts) == 5:
                job_id = parts[3]
                job = get_job(JOBS_ROOT, job_id)
                if job and job.get("state") == "completed":
                    job_root = JOBS_ROOT / job_id
                    self._send_report(
                        job_root / "analysis",
                        job_root / "processed",
                        job_root / "dataset" / "standard_dataset",
                        DATA_ROOT / "models",
                        format_name="html",
                        title=f"招采智审项目 {job_id} 异常线索复核报告",
                        llm_path=job_root / "llm" / "llm_analysis.json",
                    )
                    return
                self._send_json({"error": "任务尚未完成或不存在"}, 404)
                return
        if path.startswith("/api/projects/jobs/") and path.endswith("/report.docx"):
            parts = path.strip("/").split("/")
            if len(parts) == 5:
                job_id = parts[3]
                job = get_job(JOBS_ROOT, job_id)
                if job and job.get("state") == "completed":
                    job_root = JOBS_ROOT / job_id
                    self._send_report(
                        job_root / "analysis",
                        job_root / "processed",
                        job_root / "dataset" / "standard_dataset",
                        DATA_ROOT / "models",
                        format_name="docx",
                        title=f"招采智审项目 {job_id} 异常线索复核报告",
                        llm_path=job_root / "llm" / "llm_analysis.json",
                    )
                    return
                self._send_json({"error": "任务尚未完成或不存在"}, 404)
                return
        if path == "/api/projects/jobs":
            self._send_json({"jobs": list_jobs(JOBS_ROOT)})
            return
        if path.startswith("/api/projects/jobs/") and path.endswith("/coverage"):
            parts = path.strip("/").split("/")
            job_id = parts[-2] if len(parts) >= 4 else ""
            job = get_job(JOBS_ROOT, job_id) if job_id else None
            coverage = load_coverage_summary(JOBS_ROOT / job_id / "audit_ingestion" / "coverage_summary.json") if job else None
            self._send_json({"job_id": job_id, "coverage": coverage} if coverage is not None else {"error": "任务不存在"}, 200 if coverage is not None else 404)
            return
        if path.startswith("/api/projects/jobs/") and path.endswith("/results"):
            parts = path.strip("/").split("/")
            results = load_job_results(JOBS_ROOT, parts[-2]) if len(parts) >= 5 else None
            self._send_json(results if results else {"error": "任务尚未完成或不存在"}, 200 if results else 404)
            return
        if path.startswith("/api/projects/jobs/"):
            job = get_job(JOBS_ROOT, path.rsplit("/", 1)[-1])
            self._send_json(job if job else {"error": "任务不存在"}, 200 if job else 404)
            return
        if path == "/api/coverage":
            self._send_json(load_coverage_summary(DATA_ROOT / "audit_ingestion" / "coverage_summary.json"))
            return
        if path == "/api/ingest/status":
            training = DATA_ROOT / "training_internal"
            summary_path = training / "summary.json"
            processed_path = DATA_ROOT / "processed" / "summary.json"
            analysis_path = settings.analysis_dir / "analysis_summary.json"
            audit_coverage = load_coverage_summary(DATA_ROOT / "audit_ingestion" / "coverage_summary.json")
            network = load_network_analysis(DATA_ROOT / "network_analysis")
            summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
            processed = json.loads(processed_path.read_text(encoding="utf-8")) if processed_path.exists() else {}
            analysis = json.loads(analysis_path.read_text(encoding="utf-8")) if analysis_path.exists() else {}
            self._send_json({"training": summary, "processed": processed, "analysis": analysis, "audit": audit_coverage, "network": network.get("summary", {}), "ready": bool(summary and analysis)})
            return
        if path == "/api/model/status":
            model_dir = DATA_ROOT / "models"
            model_path = model_dir / "bid_anomaly_model.json"
            summary_path = model_dir / "training_summary.json"
            model = json.loads(model_path.read_text(encoding="utf-8")) if model_path.exists() else {}
            summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
            self._send_json({"ready": bool(model), "model": model, "summary": summary})
            return
        if path == "/api/llm/config":
            config = get_llm_config()
            config.update({
                "key_hint": "已配置（不会返回密钥）" if config["configured"] else "未配置",
                "privacy_mode": "redacted-evidence-only",
                "max_candidates": 8,
                "max_excerpt_chars": 24000,
            })
            self._send_json(config)
            return
        file_path = STATIC_DIR / ("index.html" if path == "/" else path.lstrip("/"))
        if not file_path.exists() or not file_path.is_file() or STATIC_DIR not in file_path.resolve().parents:
            self._send(404, "text/plain; charset=utf-8", b"Not found")
            return
        content_type = "text/html; charset=utf-8" if file_path.suffix == ".html" else "text/plain; charset=utf-8"
        self._send(200, content_type, file_path.read_bytes())

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/projects/upload":
            self._handle_project_upload()
            return
        if path.startswith("/api/projects/jobs/") and path.endswith("/cancel"):
            parts = path.strip("/").split("/")
            if len(parts) == 5:
                status = cancel_job(JOBS_ROOT, parts[3])
                self._send_json(status if status else {"error": "任务不存在"}, 200 if status else 404)
                return
        if path == "/api/llm/config":
            self._handle_llm_config()
            return
        self._send_json({"error": "Not found"}, 404)

    def _handle_project_upload(self):
        if not MODEL_PATH.exists():
            self._send_json({"error": "尚未生成可用模型，请先训练模型"}, 409)
            return
        try:
            content_type = self.headers.get("Content-Type", "")
            content_length = int(self.headers.get("Content-Length", "0"))
            if not content_type.startswith("multipart/form-data"):
                raise ValueError("请求必须使用 multipart/form-data")
            if content_length <= 0 or content_length > MAX_UPLOAD_BYTES:
                raise ValueError("上传文件为空或超过 4 GB 限制")
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": content_type,
                    "CONTENT_LENGTH": str(content_length),
                },
            )
            item = form["file"] if "file" in form else None
            if item is None or not getattr(item, "file", None):
                raise ValueError("没有收到项目压缩包")
            status = create_job(JOBS_ROOT, item.filename or "project.zip", item.file, MODEL_PATH)
            self._send_json(status, 202)
        except Exception as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 400)

    def _handle_llm_config(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 64 * 1024:
                raise ValueError("配置请求过大")
            payload = json.loads(self.rfile.read(length) or b"{}")
            model = str(payload.get("model") or os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")).strip()
            if model not in ALLOWED_LLM_MODELS:
                raise ValueError("不支持的大模型名称")
            base_url = validate_llm_base_url(
                str(payload.get("base_url") or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
            )
            os.environ["DEEPSEEK_MODEL"] = model
            os.environ["DEEPSEEK_BASE_URL"] = base_url
            if "enabled" in payload:
                os.environ["DEEPSEEK_ENABLED"] = "1" if payload["enabled"] else "0"
            if payload.get("clear_api_key"):
                os.environ.pop("DEEPSEEK_API_KEY", None)
            elif payload.get("api_key"):
                os.environ["DEEPSEEK_API_KEY"] = str(payload["api_key"]).strip()
            self._send_json({"ok": True, "message": "配置已应用到当前服务进程，密钥不会写入项目文件。"})
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)

    def log_message(self, fmt, *args):
        print(fmt % args)


def main():
    server = ThreadingHTTPServer((settings.host, settings.port), Handler)
    print(f"招采智审服务已启动：http://{settings.host}:{settings.port}")
    print(f"数据目录：{settings.data_dir}")
    server.serve_forever()


if __name__ == "__main__":
    main()
