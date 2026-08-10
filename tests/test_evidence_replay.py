from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from zhaocai_zhishen.evidence_replay import load_evidence_detail
from zhaocai_zhishen.finding_ids import finding_id


class EvidenceReplayTests(unittest.TestCase):
    def test_detail_returns_two_pages_and_citations(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            analysis = root / "analysis"
            processed = root / "processed"
            analysis.mkdir()
            processed.mkdir()
            finding = {
                "project_id": "p1",
                "document_id_a": "a",
                "document_id_b": "b",
                "bidder_a": "甲公司",
                "bidder_b": "乙公司",
                "anomaly_score": "55",
                "similarity": "0.7",
                "evidence": '["共同电话：13800138000"]',
                "evidence_pages_a": "[2]",
                "evidence_pages_b": "[3]",
                "review_status": "待复核",
            }
            (analysis / "analysis_summary.json").write_text(json.dumps({"anomaly_count": 1}), encoding="utf-8")
            (analysis / "document_entities.jsonl").write_text("", encoding="utf-8")
            with (analysis / "anomaly_results.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(finding))
                writer.writeheader()
                writer.writerow(finding)
            (analysis / "pairwise_similarity.csv").write_text("", encoding="utf-8")
            (processed / "documents.jsonl").write_text(
                "\n".join([
                    json.dumps({"document_id": "a", "bidder": "甲公司", "file_path": "a.pdf"}),
                    json.dumps({"document_id": "b", "bidder": "乙公司", "file_path": "b.pdf"}),
                ]),
                encoding="utf-8",
            )
            (processed / "pages.jsonl").write_text(
                "\n".join([
                    json.dumps({"document_id": "a", "page_number": 2, "text": "甲公司共同电话 13800138000"}),
                    json.dumps({"document_id": "b", "page_number": 3, "text": "乙公司共同电话 13800138000"}),
                ]),
                encoding="utf-8",
            )

            detail = load_evidence_detail(analysis, processed, finding_id(finding))

            self.assertIsNotNone(detail)
            self.assertEqual(detail["document_a"]["page_number"], 2)
            self.assertIn("甲公司", detail["document_a"]["text"])
            self.assertEqual(detail["document_b"]["page_number"], 3)
            self.assertEqual(len(detail["citations"]), 2)


if __name__ == "__main__":
    unittest.main()
