from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from zhaocai_zhishen.demo_mode import load_demo_snapshot


class DemoModeTests(unittest.TestCase):
    def test_missing_demo_data_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot = load_demo_snapshot(root, root / "analysis")
            self.assertFalse(snapshot["ready"])
            self.assertEqual(snapshot["metrics"]["archive_count"], 0)

    def test_snapshot_contains_metrics_and_local_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "training_internal").mkdir()
            (root / "processed" / "cache").mkdir(parents=True)
            (root / "models").mkdir()
            analysis_dir = root / "analysis"
            analysis_dir.mkdir()
            (root / "training_internal" / "summary.json").write_text(
                json.dumps({"archive_count": 2, "file_count": 4}, ensure_ascii=False), encoding="utf-8"
            )
            (root / "processed" / "summary.json").write_text(
                json.dumps({"success_count": 3, "page_count": 18}, ensure_ascii=False), encoding="utf-8"
            )
            (root / "models" / "bid_anomaly_model.json").write_text(
                json.dumps({"model_type": "test"}), encoding="utf-8"
            )
            (root / "models" / "training_summary.json").write_text(
                json.dumps({"pair_count": 1}), encoding="utf-8"
            )
            (analysis_dir / "analysis_summary.json").write_text(
                json.dumps({"document_count": 3, "project_count": 1, "pair_count": 1, "anomaly_count": 0}),
                encoding="utf-8",
            )
            (analysis_dir / "document_entities.jsonl").write_text("", encoding="utf-8")
            for name in ("pairwise_similarity.csv", "anomaly_results.csv"):
                (analysis_dir / name).write_text("", encoding="utf-8")
            (root / "processed" / "cache" / "a.on.paddle-gpu.all.json").write_text("{}", encoding="utf-8")

            snapshot = load_demo_snapshot(root, analysis_dir)

            self.assertTrue(snapshot["ready"])
            self.assertEqual(snapshot["metrics"]["gpu_ocr_document_count"], 1)
            self.assertEqual(snapshot["metrics"]["page_count"], 18)
            self.assertEqual(snapshot["analysis"]["summary"]["project_count"], 1)


if __name__ == "__main__":
    unittest.main()
