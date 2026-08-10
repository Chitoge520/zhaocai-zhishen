from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from zhaocai_zhishen.baseline import BaselineValidationError, build_baseline_manifest
from zhaocai_zhishen.versioning import BASELINE_SCHEMA_VERSION


class BaselineManifestTests(unittest.TestCase):
    def _write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _build_fixture(self, root: Path) -> tuple[Path, Path, Path, Path]:
        processed = root / "processed"
        analysis = root / "analysis"
        models = root / "models"
        benchmark = root / "benchmark"
        self._write_json(
            processed / "summary.json",
            {
                "input_dir": "C:/private/真实项目",
                "sample_count": 3,
                "success_count": 3,
                "failure_count": 0,
                "page_count": 12,
                "text_char_count": 3456,
                "ocr_engine": "paddle-gpu",
            },
        )
        self._write_json(
            analysis / "analysis_summary.json",
            {
                "project_count": 1,
                "document_count": 3,
                "pair_count": 3,
                "anomaly_count": 1,
                "high_risk_count": 0,
                "medium_risk_count": 1,
            },
        )
        self._write_json(
            models / "training_summary.json",
            {
                "schema_version": "bid-anomaly-training/v2",
                "training_project_count": 1,
                "training_pair_count": 3,
                "features": ["similarity"],
                "threshold": 40.0,
                "label_status": "unlabeled_unsupervised",
            },
        )
        self._write_json(
            models / "bid_anomaly_model.json",
            {
                "schema_version": "bid-anomaly-model/v1",
                "model_type": "robust_unsupervised_pairwise",
                "project_ids": ["敏感项目编号"],
            },
        )
        self._write_json(
            benchmark / "generation_summary.json",
            {
                "schema_version": "bid-synthetic-benchmark/v2",
                "source_row_count": 3,
                "generated_row_count": 14,
                "train_row_count": 10,
                "test_row_count": 4,
                "transform_count": 14,
                "project_isolation_check": True,
                "projects": {"敏感项目编号": {}},
            },
        )
        return processed, analysis, models, benchmark

    def test_manifest_is_aggregate_only_and_consistent(self) -> None:
        with TemporaryDirectory() as temporary:
            paths = self._build_fixture(Path(temporary))
            manifest = build_baseline_manifest(
                *paths,
                test_count=40,
                generated_at="2026-08-10T00:00:00+00:00",
            )
            serialized = json.dumps(manifest, ensure_ascii=False)
            self.assertEqual(manifest["schema_version"], BASELINE_SCHEMA_VERSION)
            self.assertEqual(manifest["processed_data"]["sample_count"], 3)
            self.assertEqual(manifest["synthetic_benchmark"]["transform_count"], 14)
            self.assertNotIn("C:/private", serialized)
            self.assertNotIn("敏感项目编号", serialized)
            self.assertFalse(manifest["data_boundary"]["contains_raw_bid_files"])

    def test_manifest_rejects_inconsistent_document_counts(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._build_fixture(root)
            analysis_path = paths[1] / "analysis_summary.json"
            payload = json.loads(analysis_path.read_text(encoding="utf-8"))
            payload["document_count"] = 2
            self._write_json(analysis_path, payload)
            with self.assertRaises(BaselineValidationError):
                build_baseline_manifest(*paths, test_count=40)


if __name__ == "__main__":
    unittest.main()