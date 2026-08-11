from __future__ import annotations

import csv
import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from docx import Document

from zhaocai_zhishen.reporting import build_docx_report, build_report_payload, render_html_report


class ReportingTests(unittest.TestCase):
    def test_html_and_docx_include_boundary_and_finding(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for directory in ("analysis", "processed", "training", "models", "risk_fusion"):
                (root / directory).mkdir()
            (root / "analysis" / "analysis_summary.json").write_text(
                json.dumps({"project_count": 1, "document_count": 2, "pair_count": 1, "anomaly_count": 1}), encoding="utf-8"
            )
            (root / "analysis" / "document_entities.jsonl").write_text("", encoding="utf-8")
            (root / "analysis" / "pairwise_similarity.csv").write_text("", encoding="utf-8")
            with (root / "analysis" / "anomaly_results.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=[
                    "project_id", "document_id_a", "document_id_b", "bidder_a", "bidder_b", "anomaly_score",
                    "risk_level", "similarity", "evidence", "evidence_pages_a", "evidence_pages_b", "review_status",
                ])
                writer.writeheader()
                writer.writerow({
                    "project_id": "p1", "document_id_a": "a", "document_id_b": "b", "bidder_a": "甲公司",
                    "bidder_b": "乙公司", "anomaly_score": "60", "risk_level": "中", "similarity": "0.7",
                    "evidence": '["共同电话：13800138000"]', "evidence_pages_a": "[2]", "evidence_pages_b": "[3]",
                    "review_status": "待复核",
                })
            (root / "processed" / "summary.json").write_text(json.dumps({"success_count": 2, "page_count": 8}), encoding="utf-8")
            (root / "training" / "summary.json").write_text(json.dumps({"archive_count": 1}), encoding="utf-8")
            (root / "models" / "bid_anomaly_model.json").write_text(json.dumps({"model_type": "test"}), encoding="utf-8")
            (root / "models" / "training_summary.json").write_text(json.dumps({"pair_count": 1}), encoding="utf-8")
            (root / "risk_fusion" / "risk_fusion_summary.json").write_text(json.dumps({
                "high_risk_project_count": 1, "medium_risk_project_count": 2, "evidence_event_count": 7,
            }), encoding="utf-8")
            (root / "risk_fusion" / "project_risk_results.jsonl").write_text(json.dumps({
                "project_id": "p1", "risk_score": 56, "risk_level": "high",
                "evidence_chain": {"calculation": {"independent_layers": ["network", "quote"]}},
            }) + "\n", encoding="utf-8")

            payload = build_report_payload(root / "analysis", root / "processed", root / "training", root / "models")
            html = render_html_report(payload).decode("utf-8")
            doc = Document(BytesIO(build_docx_report(payload)))
            text = "\n".join(paragraph.text for paragraph in doc.paragraphs)

            self.assertIn("待复核", html)
            self.assertIn("甲公司", html)
            self.assertIn("不直接认定串标", html)
            self.assertIn("甲公司", text)
            self.assertIn("共同电话", text)
            self.assertIn("全样本风险融合与复核优先级", html)
            self.assertIn("高风险项目：1", html)
            self.assertIn("全样本风险融合与复核优先级", text)
            self.assertIn("网络关联触发组合", text)


if __name__ == "__main__":
    unittest.main()
