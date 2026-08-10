from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from zhaocai_zhishen.analysis_results import load_unsupervised_results


class AnalysisResultsTests(unittest.TestCase):
    def test_missing_results_are_reported_without_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = load_unsupervised_results(Path(temp) / "missing")
            self.assertFalse(result["ready"])
            self.assertEqual(result["anomalies"], [])

    def test_completed_results_are_loaded_and_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "analysis_summary.json").write_text(
                json.dumps({"document_count": 2, "anomaly_count": 1}, ensure_ascii=False), encoding="utf-8"
            )
            (root / "document_entities.jsonl").write_text(
                json.dumps({"document_id": "a", "bidder": "甲公司"}, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            with (root / "pairwise_similarity.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=[
                    "document_id_a", "document_id_b", "same_bidder", "similarity", "anomaly_score",
                    "shared_phones", "shared_emails", "shared_credit_codes", "shared_contacts", "shared_addresses",
                ])
                writer.writeheader()
                writer.writerow({
                    "document_id_a": "a", "document_id_b": "b", "same_bidder": "False",
                    "similarity": "0.75", "anomaly_score": "72.5", "shared_phones": '["13800138000"]',
                    "shared_emails": "[]", "shared_credit_codes": "[]", "shared_contacts": "[]",
                    "shared_addresses": "[]",
                })
            with (root / "anomaly_results.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=[
                    "document_id_a", "document_id_b", "similarity", "anomaly_score", "evidence",
                    "evidence_pages_a", "evidence_pages_b",
                ])
                writer.writeheader()
                writer.writerow({
                    "document_id_a": "a", "document_id_b": "b", "similarity": "0.75",
                    "anomaly_score": "72.5", "evidence": '["共同电话：13800138000"]',
                    "evidence_pages_a": "[3]", "evidence_pages_b": "[7]",
                })
            result = load_unsupervised_results(root)
            self.assertTrue(result["ready"])
            self.assertEqual(result["summary"]["anomaly_count"], 1)
            self.assertEqual(result["pairs"][0]["similarity"], 0.75)
            self.assertFalse(result["pairs"][0]["same_bidder"])
            self.assertEqual(result["pairs"][0]["shared_phones"], ["13800138000"])
            self.assertEqual(result["anomalies"][0]["evidence_pages_b"], [7])

    def test_model_trigger_is_merged_into_final_findings(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "analysis_summary.json").write_text(
                json.dumps({"document_count": 2, "project_count": 1, "pair_count": 1, "anomaly_count": 0}),
                encoding="utf-8",
            )
            (root / "document_entities.jsonl").write_text("", encoding="utf-8")
            with (root / "pairwise_similarity.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=[
                    "project_id", "document_id_a", "document_id_b", "bidder_a", "bidder_b", "same_bidder",
                    "similarity", "anomaly_score", "shared_phones", "shared_emails", "shared_credit_codes",
                    "shared_contacts", "shared_addresses", "repeated_segment_count", "repeated_segment_chars",
                    "repeated_segments",
                ])
                writer.writeheader()
                writer.writerow({
                    "project_id": "p1", "document_id_a": "a", "document_id_b": "b", "bidder_a": "甲公司",
                    "bidder_b": "乙公司", "same_bidder": "False", "similarity": "0.82", "anomaly_score": "57",
                    "shared_phones": "[]", "shared_emails": "[]", "shared_credit_codes": "[]",
                    "shared_contacts": "[]", "shared_addresses": "[]", "repeated_segment_count": "4",
                    "repeated_segment_chars": "192", "repeated_segments": '["罕见技术响应内容"]',
                })
            (root / "anomaly_results.csv").write_text("", encoding="utf-8-sig")
            result = load_unsupervised_results(
                root,
                model_pairs=[{
                    "project_id": "p1", "document_id_a": "a", "document_id_b": "b",
                    "bidder_a": "甲公司", "bidder_b": "乙公司", "similarity": "0.82",
                    "anomaly_score": "57", "model_score": "81.5", "model_threshold": "69.4",
                    "model_triggered": "true", "shared_phones": "[]", "shared_emails": "[]",
                    "shared_credit_codes": "[]", "shared_contacts": "[]", "shared_addresses": "[]",
                    "repeated_segment_count": "4", "repeated_segment_chars": "192",
                    "repeated_segments": '["罕见技术响应内容"]',
                }],
            )
            self.assertEqual(result["summary"]["rule_anomaly_count"], 0)
            self.assertEqual(result["summary"]["model_triggered_count"], 1)
            self.assertEqual(result["summary"]["anomaly_count"], 1)
            self.assertEqual(result["anomalies"][0]["model_score"], 81.5)
            self.assertIn("仅作为辅助证据", result["anomalies"][0]["evidence"][-1])


if __name__ == "__main__":
    unittest.main()
