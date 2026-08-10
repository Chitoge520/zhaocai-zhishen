from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zhaocai_zhishen.llm_analysis import (
    build_evidence_packet,
    redact_text,
    run_llm_analysis,
    validate_llm_result,
)


class LlmAnalysisTests(unittest.TestCase):
    def test_redaction_is_stable_and_masks_direct_identifiers(self):
        text = "联系人电话 13800138000，邮箱 audit@example.com，再次出现 13800138000。"
        redacted = redact_text(text)
        self.assertNotIn("13800138000", redacted)
        self.assertNotIn("audit@example.com", redacted)
        phone_tokens = [part for part in redacted.split() if "<电话:" in part]
        self.assertIn(redacted.split("，")[0].split()[-1], redacted.split("，")[-1])
        self.assertTrue(phone_tokens)

    def test_build_packet_only_contains_redacted_evidence_pages(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            processed, analysis, inference, output = [root / name for name in ("processed", "analysis", "inference", "llm")]
            processed.mkdir(); analysis.mkdir(); inference.mkdir()
            with (processed / "pages.jsonl").open("w", encoding="utf-8") as handle:
                handle.write(json.dumps({"document_id": "doc-a", "page_number": 3, "text": "联系电话 13800138000，方案存在相同错字。"}, ensure_ascii=False) + "\n")
                handle.write(json.dumps({"document_id": "doc-b", "page_number": 5, "text": "联系电话 13800138000，方案存在相同错字。"}, ensure_ascii=False) + "\n")
                handle.write(json.dumps({"document_id": "doc-a", "page_number": 99, "text": "不应发送的页面"}, ensure_ascii=False) + "\n")
            fieldnames = ["project_id", "document_id_a", "document_id_b", "bidder_a", "bidder_b", "anomaly_score", "similarity", "evidence", "evidence_pages_a", "evidence_pages_b"]
            with (analysis / "anomaly_results.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerow({"project_id": "p1", "document_id_a": "doc-a", "document_id_b": "doc-b", "bidder_a": "甲公司", "bidder_b": "乙公司", "anomaly_score": 60, "similarity": 0.5, "evidence": json.dumps(["共同电话：13800138000"], ensure_ascii=False), "evidence_pages_a": "[3]", "evidence_pages_b": "[5]"})
            packet = build_evidence_packet(processed, analysis, inference, output)
            serialized = json.dumps(packet, ensure_ascii=False)
            self.assertNotIn("13800138000", serialized)
            self.assertNotIn("不应发送的页面", serialized)
            self.assertEqual({row["page_number"] for row in packet["excerpts"]}, {3, 5})
            self.assertTrue((output / "llm_input.json").exists())

    def test_validation_rejects_unverifiable_citations(self):
        packet = {
            "candidates": [{"candidate_id": "C001", "project_id": "p1", "document_id_a": "a", "document_id_b": "b", "bidder_a": "甲", "bidder_b": "乙"}],
            "excerpts": [{"document_id": "a", "page_number": 2, "text": "原文中的共同错字"}],
        }
        raw = {"summary": "测试", "findings": [
            {"candidate_id": "C001", "title": "可验证", "confidence": "high", "citations": [{"document_id": "a", "page_number": 2, "quote": "共同错字"}]},
            {"candidate_id": "C001", "title": "幻觉引用", "confidence": "high", "citations": [{"document_id": "b", "page_number": 8, "quote": "不存在的原文"}]},
        ]}
        result = validate_llm_result(packet, raw)
        self.assertEqual(result["validated_finding_count"], 1)
        self.assertEqual(result["rejected_finding_count"], 1)
        self.assertEqual(result["findings"][0]["review_status"], "待复核")

    def test_run_analysis_uses_injected_caller_and_persists_validated_result(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"DEEPSEEK_ENABLED": "1", "DEEPSEEK_API_KEY": "test-key"}, clear=False):
            root = Path(temp)
            processed, analysis, inference, output = [root / name for name in ("processed", "analysis", "inference", "llm")]
            processed.mkdir(); analysis.mkdir(); inference.mkdir()
            (processed / "pages.jsonl").write_text(json.dumps({"document_id": "a", "page_number": 1, "text": "相同的罕见表述"}, ensure_ascii=False) + "\n", encoding="utf-8")
            with (analysis / "anomaly_results.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["project_id", "document_id_a", "document_id_b", "bidder_a", "bidder_b", "anomaly_score", "similarity", "evidence", "evidence_pages_a", "evidence_pages_b"])
                writer.writeheader(); writer.writerow({"project_id": "p", "document_id_a": "a", "document_id_b": "b", "bidder_a": "甲", "bidder_b": "乙", "anomaly_score": 55, "similarity": 0.4, "evidence": "[]", "evidence_pages_a": "[1]", "evidence_pages_b": "[]"})
            def fake_caller(packet):
                return {"summary": "存在待复核表述", "findings": [{"candidate_id": "C001", "title": "罕见表述相同", "explanation": "需要核对来源", "confidence": "medium", "recommended_review": "检查模板来源", "citations": [{"document_id": "a", "page_number": 1, "quote": "罕见表述"}]}]}
            result = run_llm_analysis(processed, analysis, inference, output, caller=fake_caller)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["validated_finding_count"], 1)
            self.assertTrue((output / "llm_analysis.json").exists())


if __name__ == "__main__":
    unittest.main()