from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from zhaocai_zhishen.unsupervised_analysis import (
    build_repeated_segment_index,
    build_unsupervised_analysis,
    extract_entities,
    is_valid_address,
    is_valid_credit_code,
    is_valid_phone,
    repeated_segment_evidence,
)


class UnsupervisedAnalysisTests(unittest.TestCase):
    def test_credit_code_checksum_excludes_identity_numbers(self) -> None:
        valid_code = "91310000MA1K123451"
        identity_number = "340823198106080057"
        self.assertTrue(is_valid_credit_code(valid_code))
        self.assertFalse(is_valid_credit_code(identity_number))
        entities = extract_entities({
            "document_id": "doc-credit",
            "text": f"统一社会信用代码：{valid_code}\n法定代表人身份证号：{identity_number}",
        })
        self.assertEqual(entities["credit_codes"], [valid_code])

    def test_phone_and_address_filters_reject_catalog_codes_and_labels(self) -> None:
        self.assertTrue(is_valid_phone("02120881123"))
        self.assertTrue(is_valid_phone("05938901666"))
        self.assertTrue(is_valid_phone("13812345678"))
        self.assertFalse(is_valid_phone("040901007001"))
        self.assertFalse(is_valid_phone("011707001001"))
        self.assertFalse(is_valid_phone("010516005001"))
        self.assertTrue(is_valid_address("上海市浦东新区浦东南路3470号"))
        self.assertFalse(is_valid_address("制造的部件名称"))
        self.assertFalse(is_valid_address("提交下述文件正本一份和副本肆份"))

    def test_address_extraction_stops_before_trailing_contact_fields(self) -> None:
        entities = extract_entities({
            "document_id": "doc-address-boundary",
            "text": "注册地址：上海市浦东新区浦东南路3470号8楼 传真：021-20881123 电话：13812345678 邮箱：audit@example.com",
        })
        self.assertEqual(entities["addresses"], ["上海市浦东新区浦东南路3470号8楼"])
        self.assertEqual(entities["phones"], ["02120881123", "13812345678"])
        self.assertEqual(entities["emails"], ["audit@example.com"])
    def test_entities_prefer_manifest_bidder_and_ignore_trailing_certification_contacts(self) -> None:
        trailing = "无关内容" * 14_000 + "\n检测机构电话：075512345678 邮箱：lab@example.com"
        entities = extract_entities({
            "document_id": "doc-scope",
            "bidder_name": "001_上海凯道电子科技有限公司 会议系统项目",
            "text": "投标人：上海凯道电科技有限公司\n联系人：张三 电话：13812345678\n" + trailing,
        })
        self.assertEqual(entities["bidder"], "上海凯道电子科技有限公司")
        self.assertEqual(entities["phones"], ["13812345678"])
        self.assertEqual(entities["emails"], [])

    def test_english_bidder_name_is_extracted_from_labeled_field(self) -> None:
        entities = extract_entities({
            "document_id": "doc-english",
            "bidder_name": "2026 project scanned file",
            "text": "投标人名称：Terminexus Co., Ltd.\n投标人代表签字：",
        })
        self.assertEqual(entities["bidder"], "Terminexus Co., Ltd.")
    def test_contact_labels_are_not_treated_as_person_names(self) -> None:
        entities = extract_entities({
            "document_id": "doc-contact",
            "text": "联系人及电话：13812345678\n联系人：张三 电话：13812345678",
        })
        self.assertEqual(entities["contacts"], ["张三"])

    def test_repeated_segments_exclude_project_wide_template(self) -> None:
        template = "投标文件编制说明：本文件按照招标文件要求编制，内容真实有效。" * 8
        copied = "共同错误：设备数量写成一百一十七台，交付周期为三十七日。" * 8
        documents = [{"text": template + copied}, {"text": template + copied}, {"text": template}]
        indexes, common = build_repeated_segment_index(documents)
        evidence = repeated_segment_evidence(
            documents[0]["text"], documents[1]["text"],
            common_segments=set(common), left_index=indexes[0], right_index=indexes[1],
        )
        self.assertGreaterEqual(evidence["count"], 1)
        self.assertGreaterEqual(evidence["chars"], 96)
        self.assertTrue(any("共同错误" in segment for segment in evidence["segments"]))
        self.assertNotIn("投标文件编制说明", "".join(evidence["segments"]))

    def test_project_bidder_aliases_are_canonicalized(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            processed = root / "processed"
            output = root / "analysis"
            processed.mkdir()
            documents = [
                {
                    "document_id": "part-a",
                    "project_id": "project-alias",
                    "bidder_name": "上海梵企 二楼展厅项目 扫描件 部分1",
                    "file_path": "part-a.pdf",
                    "text": "技术部分" * 100,
                },
                {
                    "document_id": "part-b",
                    "project_id": "project-alias",
                    "bidder_name": "上海梵企光电科技有限公司 商务部分",
                    "file_path": "part-b.pdf",
                    "text": "商务部分" * 100,
                },
            ]
            (processed / "documents.jsonl").write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in documents), encoding="utf-8"
            )
            (processed / "pages.jsonl").write_text("", encoding="utf-8")
            summary = build_unsupervised_analysis(processed, output)
            self.assertEqual(summary["pair_count"], 1)
            with (output / "pairwise_similarity.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                pair = next(csv.DictReader(handle))
            self.assertEqual(pair["same_bidder"], "True")
            self.assertEqual(summary["anomaly_count"], 0)
            entity_rows = [json.loads(line) for line in (output / "document_entities.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual({row["bidder"] for row in entity_rows}, {"上海梵企光电科技有限公司"})

    def test_documents_from_different_projects_are_never_compared(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            processed = root / "processed"
            output = root / "analysis"
            processed.mkdir()
            documents = [
                {
                    "document_id": "p1-a",
                    "project_id": "project-1",
                    "bidder_name": "甲方科技有限公司",
                    "file_path": "p1-a.pdf",
                    "text": "投标人：甲方科技有限公司\n联系人：张三 电话：13812345678\n技术方案" * 20,
                },
                {
                    "document_id": "p1-b",
                    "project_id": "project-1",
                    "bidder_name": "乙方科技有限公司",
                    "file_path": "p1-b.pdf",
                    "text": "投标人：乙方科技有限公司\n联系人：李四 电话：13812345678\n技术方案" * 20,
                },
                {
                    "document_id": "p2-a",
                    "project_id": "project-2",
                    "bidder_name": "甲方科技有限公司",
                    "file_path": "p2-a.pdf",
                    "text": "投标人：甲方科技有限公司\n联系人：王五 电话：13812345678\n技术方案" * 20,
                },
                {
                    "document_id": "p2-b",
                    "project_id": "project-2",
                    "bidder_name": "丙方设备有限公司",
                    "file_path": "p2-b.pdf",
                    "text": "投标人：丙方设备有限公司\n联系人：赵六 电话：13812345678\n技术方案" * 20,
                },
            ]
            (processed / "documents.jsonl").write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in documents),
                encoding="utf-8",
            )
            (processed / "pages.jsonl").write_text("", encoding="utf-8")

            summary = build_unsupervised_analysis(processed, output)

            self.assertEqual(summary["pair_count"], 2)
            with (output / "pairwise_similarity.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                pairs = list(csv.DictReader(handle))
            self.assertEqual({row["project_id"] for row in pairs}, {"project-1", "project-2"})
            self.assertTrue(
                all(
                    row["document_id_a"].startswith(row["project_id"].replace("project-", "p"))
                    and row["document_id_b"].startswith(row["project_id"].replace("project-", "p"))
                    for row in pairs
                )
            )

    def test_similarity_shared_phone_and_page_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            processed = root / "processed"
            output = root / "analysis"
            processed.mkdir()
            common = "本项目采用相同技术方案和设备配置，提供完整的安装调试及售后服务。" * 20
            documents = [
                {
                    "document_id": "doc-a",
                    "project_id": "project-1",
                    "bidder_name": "甲方科技有限公司",
                    "file_path": "a.pdf",
                    "text": f"投标人：甲方科技有限公司\n联系人：张三 电话：13812345678\n{common}",
                },
                {
                    "document_id": "doc-b",
                    "project_id": "project-1",
                    "bidder_name": "乙方科技有限公司",
                    "file_path": "b.pdf",
                    "text": f"投标人：乙方科技有限公司\n联系人：李四 电话：13812345678\n{common}",
                },
                {
                    "document_id": "doc-c",
                    "project_id": "project-1",
                    "bidder_name": "丙方设备有限公司",
                    "file_path": "c.pdf",
                    "text": "投标人：丙方设备有限公司\n联系人：王五 电话：13900001111\n完全不同的简短报价文件。",
                },
            ]
            pages = [
                {"document_id": row["document_id"], "page_number": 1, "text": row["text"]}
                for row in documents
            ]
            for path, rows in ((processed / "documents.jsonl", documents), (processed / "pages.jsonl", pages)):
                path.write_text(
                    "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                    encoding="utf-8",
                )

            summary = build_unsupervised_analysis(processed, output)
            self.assertEqual(summary["document_count"], 3)
            self.assertEqual(summary["pair_count"], 3)
            self.assertGreaterEqual(summary["anomaly_count"], 1)
            with (output / "anomaly_results.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                anomalies = list(csv.DictReader(handle))
            target = next(row for row in anomalies if {row["document_id_a"], row["document_id_b"]} == {"doc-a", "doc-b"})
            self.assertIn("13812345678", target["evidence"])
            self.assertGreater(int(target["repeated_segment_count"]), 0)
            self.assertEqual(json.loads(target["evidence_pages_a"]), [1])
            self.assertEqual(json.loads(target["evidence_pages_b"]), [1])
            self.assertEqual(target["review_status"], "待复核")


if __name__ == "__main__":
    unittest.main()
