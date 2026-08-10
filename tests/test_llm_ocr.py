from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from zhaocai_zhishen.llm_ocr import build_ocr_packet, validate_ocr_result


class LlmOcrTests(unittest.TestCase):
    def test_packet_redacts_sensitive_values_and_selects_suspicious_page(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "documents.jsonl").write_text(
                json.dumps({"document_id": "doc-1", "bidder_name": "甲公司"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            (root / "pages.jsonl").write_text(
                json.dumps({
                    "document_id": "doc-1",
                    "page_number": 1,
                    "text": "投标件\n电话：13800138000\n邮箱：audit@example.com",
                    "cleaned_text": "投标件\n电话：13800138000\n邮箱：audit@example.com",
                }, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            packet = build_ocr_packet(root)

            self.assertEqual(len(packet["pages"]), 1)
            self.assertNotIn("13800138000", packet["pages"][0]["text"])
            self.assertNotIn("audit@example.com", packet["pages"][0]["text"])
            self.assertTrue(packet["redaction_maps"])

    def test_result_restores_only_original_redacted_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "documents.jsonl").write_text(
                json.dumps({"document_id": "doc-1", "bidder_name": "甲公司"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            (root / "pages.jsonl").write_text(
                json.dumps({
                    "document_id": "doc-1",
                    "page_number": 1,
                    "text": "投标件\n电话：13800138000",
                    "cleaned_text": "投标件\n电话：13800138000",
                }, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            packet = build_ocr_packet(root)
            masked = packet["pages"][0]["text"]
            result = validate_ocr_result(packet, {
                "pages": [{
                    "document_id": "doc-1",
                    "page_number": 1,
                    "status": "corrected",
                    "corrected_text": masked.replace("投标件", "投标文件"),
                    "changes": ["修正 OCR 错字"],
                    "fields": {},
                }]
            })

            self.assertEqual(result["validated_count"], 1)
            self.assertIn("投标文件", result["corrections"][0]["corrected_text"])
            self.assertIn("13800138000", result["corrections"][0]["corrected_text"])

    def test_result_rejects_unknown_page(self) -> None:
        result = validate_ocr_result({"pages": [], "redaction_maps": {}}, {"pages": [{"document_id": "unknown", "page_number": 1, "corrected_text": "x"}]})
        self.assertEqual(result["validated_count"], 0)
        self.assertEqual(result["rejected_count"], 1)


if __name__ == "__main__":
    unittest.main()
