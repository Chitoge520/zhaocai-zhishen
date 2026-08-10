from __future__ import annotations

import argparse
import json
from pathlib import Path

from .analysis import build_analysis
from .config import load_settings
from .document_pipeline import prepare_dataset
from .unsupervised_analysis import build_unsupervised_analysis


def build_parser() -> argparse.ArgumentParser:
    settings = load_settings()
    default_dataset = settings.project_root / "data" / "training_internal" / "standard_dataset"
    parser = argparse.ArgumentParser(prog="zhaocai-zhishen", description="招采智审本地分析工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="检查数据目录和运行环境")
    check.add_argument("--input", type=Path, default=default_dataset)

    prepare = subparsers.add_parser("prepare", help="抽取投标文件并生成无监督样本集")
    prepare.add_argument("--input", type=Path, default=default_dataset)
    prepare.add_argument("--output", type=Path, default=settings.project_root / "data" / "processed")
    prepare.add_argument("--ocr", choices=["auto", "on", "off"], default="auto")
    prepare.add_argument(
        "--ocr-engine",
        choices=["auto", "paddle-gpu", "rapidocr"],
        default="auto",
        help="OCR 引擎；auto 优先使用本机 GPU，失败时回退 RapidOCR",
    )
    prepare.add_argument("--max-documents", type=int)
    prepare.add_argument("--max-pages-per-document", type=int)
    prepare.add_argument("--force", action="store_true", help="忽略已有文档缓存")
    prepare.add_argument("--workers", type=int, default=1, help="并行处理进程数")

    subparsers.add_parser("serve", help="启动本地审计驾驶舱")

    ingest = subparsers.add_parser("ingest", help="导入 CSV/JSONL 多源审计数据并生成覆盖率摘要")
    ingest.add_argument("--input", type=Path, default=settings.project_root / "data" / "training_internal")
    ingest.add_argument("--output", type=Path, default=settings.project_root / "data" / "audit_ingestion")
    ingest.add_argument("--strict", action="store_true", help="遇到 schema 错误时以失败退出")

    links = subparsers.add_parser("analyze-links", help="基于 M1 标准记录生成 IP、设备和文件元数据关联线索")
    links.add_argument("--input", type=Path, default=settings.project_root / "data" / "audit_ingestion")
    links.add_argument("--output", type=Path, default=settings.project_root / "data" / "network_analysis")
    links.add_argument("--exclude-ip", action="append", default=[], help="可重复指定平台、代理机构或采购单位公共出口 IP")
    links.add_argument("--network-window-minutes", type=int, default=30)
    links.add_argument("--metadata-window-seconds", type=int, default=300)

    quotes = subparsers.add_parser("analyze-quotes", help="执行 M3 项目内报价聚类与规律性差异分析")
    quotes.add_argument("--input", type=Path, default=settings.project_root / "data" / "audit_ingestion")
    quotes.add_argument("--output", type=Path, default=settings.project_root / "data" / "quote_analysis")

    analyze = subparsers.add_parser("analyze", help="生成项目内无监督异常线索与页码证据")
    analyze.add_argument("--input", type=Path, default=settings.project_root / "data" / "processed")
    analyze.add_argument("--output", type=Path, default=settings.project_root / "data" / "analysis")
    train = subparsers.add_parser("train", help="按项目划分训练无监督异常基线模型")
    train.add_argument("--input", type=Path, default=settings.project_root / "data" / "analysis")
    train.add_argument("--output", type=Path, default=settings.project_root / "data" / "models")
    train.add_argument("--folds", type=int, default=5)
    train.add_argument(
        "--benchmark",
        type=Path,
        help="可选的合成基准目录；只写入受控基准评估，不参与真实无监督模型拟合",
    )
    benchmark = subparsers.add_parser("make-benchmark", help="从两两分析结果生成项目隔离的合成训练/测试基准集")
    benchmark.add_argument("--analysis", type=Path, default=settings.project_root / "data" / "analysis" / "pairwise_similarity.csv")
    benchmark.add_argument("--output", type=Path, default=settings.project_root / "data" / "synthetic_benchmark")
    benchmark.add_argument("--test-fraction", type=float, default=0.25)
    benchmark.add_argument("--seed", type=int, default=20260808)
    baseline = subparsers.add_parser("baseline", help="生成不含敏感主体信息的竞赛基线清单")
    baseline.add_argument("--processed", type=Path, default=settings.project_root / "data" / "processed")
    baseline.add_argument("--analysis", type=Path, default=settings.project_root / "data" / "analysis")
    baseline.add_argument("--models", type=Path, default=settings.project_root / "data" / "models")
    baseline.add_argument("--benchmark", type=Path, default=settings.project_root / "data" / "synthetic_benchmark")
    baseline.add_argument(
        "--output",
        type=Path,
        default=settings.project_root / "docs" / "baselines" / "competition-m0-baseline.json",
    )
    baseline.add_argument("--tests", type=int, default=46, help="本轮已通过的单元测试数量")
    infer = subparsers.add_parser("infer", help="使用已训练模型对新项目分析结果进行异常评分")
    infer.add_argument("--input", type=Path, required=True)
    infer.add_argument("--model", type=Path, default=settings.project_root / "data" / "models" / "bid_anomaly_model.json")
    infer.add_argument("--output", type=Path, default=settings.project_root / "data" / "inference")
    enhance_ocr = subparsers.add_parser("enhance-ocr", help="使用大模型对疑似 OCR 错误页面进行脱敏校正")
    enhance_ocr.add_argument("--input", type=Path, default=settings.project_root / "data" / "processed")
    enhance_ocr.add_argument("--output", type=Path, default=settings.project_root / "data" / "ocr_llm")
    return parser


def command_check(input_dir: Path) -> int:
    payload = {
        "input_dir": str(input_dir.resolve()),
        "exists": input_dir.exists(),
        "samples_manifest": (input_dir / "samples.csv").exists(),
        "file_manifest": (input_dir / "file_manifest.csv").exists(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["exists"] else 2


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "check":
        raise SystemExit(command_check(args.input))
    if args.command == "prepare":
        result = prepare_dataset(
            args.input,
            args.output,
            ocr_mode=args.ocr,
            ocr_engine_name=args.ocr_engine,
            max_documents=args.max_documents,
            max_pages_per_document=args.max_pages_per_document,
            reuse_cache=not args.force,
            workers=args.workers,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "serve":
        from .server import main as serve

        serve()
        return
    if args.command == "ingest":
        from .audit_ingestion import ingest_audit_data

        result = ingest_audit_data(args.input, args.output, strict=args.strict)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "analyze-links":
        from .network_analysis import build_network_analysis

        result = build_network_analysis(
            args.input,
            args.output,
            excluded_ips=args.exclude_ip,
            network_window_seconds=max(1, args.network_window_minutes) * 60,
            metadata_window_seconds=max(1, args.metadata_window_seconds),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "analyze-quotes":
        from .quote_analysis import build_quote_analysis

        result = build_quote_analysis(args.input, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "baseline":
        from .baseline import build_baseline_manifest, write_baseline_manifest

        manifest = build_baseline_manifest(
            args.processed,
            args.analysis,
            args.models,
            args.benchmark,
            test_count=args.tests,
        )
        output_path = write_baseline_manifest(manifest, args.output)
        print(json.dumps({"output": str(output_path), "manifest": manifest}, ensure_ascii=False, indent=2))
        return
    if args.command == "infer":
        from .model_inference import run_model_inference

        result = run_model_inference(args.input, args.model, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "train":
        from .model_training import train_model

        result = train_model(args.input, args.output, folds=args.folds, benchmark_dir=args.benchmark)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "enhance-ocr":
        from .llm_ocr import run_ocr_enhancement

        result = run_ocr_enhancement(args.input, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "make-benchmark":
        from .synthetic_benchmark import generate_benchmark

        result = generate_benchmark(args.analysis, args.output, test_fraction=args.test_fraction, seed=args.seed)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "analyze":
        input_dir = args.input.resolve()
        if (input_dir / "documents.jsonl").exists():
            result = build_unsupervised_analysis(input_dir, args.output)
        else:
            result = build_analysis(input_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    parser.error(f"未知命令: {args.command}")
