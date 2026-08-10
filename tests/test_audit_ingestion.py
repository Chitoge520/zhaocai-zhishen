from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from zhaocai_zhishen.audit_ingestion import ingest_audit_data
from zhaocai_zhishen.audit_schema import (
    _CREDIT_CODE_CHARS,
    _CREDIT_CODE_WEIGHTS,
    is_valid_credit_code,
    make_source_reference,
    normalize_company_name,
)


def valid_credit_code(prefix: str) -> str:
    assert len(prefix) == 17
    total = sum(_CREDIT_CODE_CHARS.index(char) * weight for char, weight in zip(prefix, _CREDIT_CODE_WEIGHTS))
    return prefix + _CREDIT_CODE_CHARS[(31 - total % 31) % 31]


class AuditIngestionTests(unittest.TestCase):
    def write_csv(self, root: Path, name: str, rows: list[dict]) -> Path:
        path = root / name
        fields = list(dict.fromkeys(key for row in rows for key in row)) if rows else ["record_type"]
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def test_company_aliases_and_credit_code_prefer_global_bidder_id(self) -> None:
        code = valid_credit_code("91350211M000100Y4")
        self.assertTrue(is_valid_credit_code(code))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_csv(root, "quotes.csv", [
                {
                    "record_type": "quote", "project_name": "项目甲", "bidder_name": "示例科技（北京）有限公司",
                    "bidder_aliases": "示例科技有限公司", "credit_code": code, "amount": "1,200.00",
                },
                {
                    "record_type": "quote", "project_name": "项目乙", "bidder_name": "示例科技有限公司",
                    "amount": "1300", "event_time": "2026-08-10 09:00:00",
                },
            ])
            result = ingest_audit_data(root, root / "out")
            records = [json.loads(line) for line in (root / "out" / "audit_records.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(result["record_count"], 2)
            self.assertEqual(records[0]["bidder_id"], records[1]["bidder_id"])
            self.assertTrue(records[0]["bidder_id"].startswith("bidder_"))
            self.assertEqual(result["coverage"]["categories"]["quotes"]["usable_record_count"], 2)

    def test_missing_ip_or_quote_does_not_block_text_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_csv(root, "audit_records.csv", [
                {"record_type": "quote", "project_name": "项目甲", "bidder_name": "甲公司", "amount": ""},
                {"record_type": "network_event", "project_name": "项目甲", "bidder_name": "甲公司", "event_time": "2026-08-10"},
            ])
            result = ingest_audit_data(root, root / "out")
            self.assertEqual(result["record_count"], 2)
            self.assertTrue(result["coverage"]["degradation"]["text_analysis_allowed_without_quotes"])
            self.assertTrue(result["coverage"]["degradation"]["text_analysis_allowed_without_ip"])
            codes = result["coverage"]["data_quality"]["issue_codes"]
            self.assertGreaterEqual(codes.get("missing_amount", 0), 1)
            self.assertGreaterEqual(codes.get("missing_network_identity", 0), 1)

    def test_duplicate_records_are_deduplicated_with_two_source_refs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            row = {"record_type": "quote", "project_name": "项目甲", "bidder_name": "甲公司", "amount": "1000"}
            self.write_csv(root, "quotes.csv", [row])
            self.write_csv(root, "quotes_copy.csv", [row])
            result = ingest_audit_data(root, root / "out")
            records = [json.loads(line) for line in (root / "out" / "audit_records.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(result["record_count"], 1)
            self.assertEqual(result["coverage"]["records"]["duplicate_record_count"], 1)
            self.assertEqual(len(records[0]["source_refs"]), 2)

    def test_credit_code_conflict_is_reported_without_crashing_default_mode(self) -> None:
        code_a = valid_credit_code("91350211M000100Y4")
        code_b = valid_credit_code("91350211M000100Y5")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_csv(root, "bidders.csv", [
                {"record_type": "bidder", "bidder_name": "冲突企业有限公司", "credit_code": code_a},
                {"record_type": "bidder", "bidder_name": "冲突企业有限公司", "credit_code": code_b},
            ])
            result = ingest_audit_data(root, root / "out")
            self.assertEqual(result["coverage"]["data_quality"]["credit_code_conflict_count"], 1)
            issues = [json.loads(line) for line in (root / "out" / "validation_issues.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(issue["code"] == "credit_code_conflict" for issue in issues))

    def test_invalid_amount_and_time_are_downgraded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_csv(root, "quotes.csv", [{
                "record_type": "quote", "project_name": "项目甲", "bidder_name": "甲公司",
                "amount": "-10", "event_time": "not-a-time",
            }])
            result = ingest_audit_data(root, root / "out")
            record = json.loads((root / "out" / "audit_records.jsonl").read_text(encoding="utf-8").splitlines()[0])
            self.assertIsNone(record["amount"])
            self.assertEqual(record["event_time"], "")
            codes = result["coverage"]["data_quality"]["issue_codes"]
            self.assertGreaterEqual(codes.get("invalid_amount", 0), 1)
            self.assertGreaterEqual(codes.get("invalid_time", 0), 1)

    def test_standard_samples_are_adapted_to_document_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw_row = {
                "sample_id": "sample-001", "project_name": "???", "bidder_candidate": "???",
                "bid_file": "projects/p/????/????.pdf", "source_inner_path": "????.pdf",
            }
            self.write_csv(root, "samples.csv", [raw_row])
            result = ingest_audit_data(root, root / "out")
            record = json.loads((root / "out" / "audit_records.jsonl").read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(result["record_count"], 1)
            self.assertEqual(record["record_type"], "document")
            self.assertEqual(record["record_id"], "sample-001")
            expected_ref = make_source_reference("samples.csv", "csv", 2, raw_row)
            self.assertEqual(record["source_refs"][0]["source_sha256"], expected_ref.source_sha256)
            self.assertEqual(result["coverage"]["categories"]["files"]["usable_record_count"], 1)

    def test_jsonl_and_empty_table_are_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "network_events.jsonl").write_text(
                json.dumps({"project_name": "项目甲", "bidder_name": "甲公司", "ip": "10.0.0.1"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            self.write_csv(root, "historical_relations.csv", [])
            result = ingest_audit_data(root, root / "out")
            self.assertEqual(result["record_count"], 1)
            self.assertEqual(result["coverage"]["source_files"]["empty"], 1)
            self.assertEqual(result["coverage"]["categories"]["ip"]["usable_record_count"], 1)

    def test_sensitive_validation_hints_do_not_log_raw_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_csv(root, "quotes.csv", [{
                "record_type": "quote", "project_name": "项目甲", "bidder_name": "敏感企业有限公司",
                "credit_code": "91350211M000100Y4X", "amount": "bad",
            }])
            ingest_audit_data(root, root / "out")
            issue_text = (root / "out" / "validation_issues.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("敏感企业有限公司", issue_text)
            self.assertNotIn("91350211M000100Y4X", issue_text)

    def test_normalize_company_name_is_punctuation_stable(self) -> None:
        self.assertEqual(normalize_company_name("示例（北京）有限公司"), normalize_company_name("示例北京有限公司"))


if __name__ == "__main__":
    unittest.main()
