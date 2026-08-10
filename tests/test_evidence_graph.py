from __future__ import annotations

import unittest
import csv
import json
import tempfile
from pathlib import Path

from zhaocai_zhishen.evidence_graph import build_evidence_graph, load_evidence_graph


class EvidenceGraphTests(unittest.TestCase):
    def test_graph_scopes_nodes_by_project_and_links_shared_entities(self) -> None:
        graph = build_evidence_graph(
            [
                {
                    "project_id": "p1",
                    "document_id": "d1",
                    "file_path": "p1/投标文件/a.pdf",
                    "bidder": "甲公司",
                    "phones": ["13800138000"],
                    "emails": [],
                    "credit_codes": [],
                    "contacts": [],
                    "addresses": [],
                },
                {
                    "project_id": "p1",
                    "document_id": "d2",
                    "file_path": "p1/投标文件/b.pdf",
                    "bidder": "乙公司",
                    "phones": ["13800138000"],
                    "emails": [],
                    "credit_codes": [],
                    "contacts": [],
                    "addresses": [],
                },
            ],
            [
                {
                    "project_id": "p1",
                    "document_id_a": "d1",
                    "document_id_b": "d2",
                    "bidder_a": "甲公司",
                    "bidder_b": "乙公司",
                    "same_bidder": False,
                    "similarity": 0.8,
                    "anomaly_score": 70,
                }
            ],
            [
                {
                    "project_id": "p1",
                    "document_id_a": "d1",
                    "document_id_b": "d2",
                    "evidence": ["共同电话：13800138000"],
                }
            ],
        )

        self.assertEqual(graph["project_count"], 1)
        self.assertEqual(len([node for node in graph["nodes"] if node["type"] == "bidder"]), 2)
        self.assertEqual(len([node for node in graph["nodes"] if node["type"] == "phone"]), 1)
        self.assertEqual(len([edge for edge in graph["edges"] if edge["type"] == "text_similarity"]), 1)
        self.assertEqual(len(graph["findings"]), 1)
        self.assertTrue(graph["findings"][0]["finding_id"].startswith("finding:"))

    def test_loaded_graph_uses_frozen_model_scores(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            analysis = root / "analysis"
            analysis.mkdir()
            (analysis / "analysis_summary.json").write_text(
                json.dumps({"pair_count": 1}, ensure_ascii=False), encoding="utf-8"
            )
            entities = [
                {"project_id": "p1", "document_id": "d1", "file_path": "a.pdf", "bidder": "甲公司", "phones": [], "emails": [], "credit_codes": [], "contacts": [], "addresses": []},
                {"project_id": "p1", "document_id": "d2", "file_path": "b.pdf", "bidder": "乙公司", "phones": [], "emails": [], "credit_codes": [], "contacts": [], "addresses": []},
            ]
            (analysis / "document_entities.jsonl").write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in entities), encoding="utf-8"
            )
            fields = ["project_id", "document_id_a", "document_id_b", "bidder_a", "bidder_b", "same_bidder", "similarity", "anomaly_score", "shared_phones", "shared_emails", "shared_credit_codes", "shared_contacts", "shared_addresses", "repeated_segment_count", "repeated_segment_chars", "repeated_segments"]
            with (analysis / "pairwise_similarity.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow({"project_id": "p1", "document_id_a": "d1", "document_id_b": "d2", "bidder_a": "甲公司", "bidder_b": "乙公司", "same_bidder": "False", "similarity": "0.2", "anomaly_score": "0", "shared_phones": "[]", "shared_emails": "[]", "shared_credit_codes": "[]", "shared_contacts": "[]", "shared_addresses": "[]", "repeated_segment_count": "0", "repeated_segment_chars": "0", "repeated_segments": "[]"})
            with (analysis / "anomaly_results.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields + ["evidence", "evidence_pages_a", "evidence_pages_b", "review_status"])
                writer.writeheader()
            (analysis / "pages.jsonl").write_text("", encoding="utf-8")
            model = root / "model.json"
            model.write_text(json.dumps({
                "model_type": "robust_unsupervised_pairwise",
                "threshold": 1.0,
                "feature_names": ["similarity"],
                "feature_medians": {"similarity": 0.2},
                "feature_scales": {"similarity": 0.01},
                "feature_weights": {"similarity": 1.0},
            }), encoding="utf-8")

            graph = load_evidence_graph(analysis, model_path=model)

        similarity_edges = [edge for edge in graph["edges"] if edge["type"] == "text_similarity"]
        self.assertEqual(len(graph["findings"]), 1)
        self.assertTrue(graph["findings"][0]["model_triggered"])
        self.assertTrue(similarity_edges[0]["finding_id"])


if __name__ == "__main__":
    unittest.main()
