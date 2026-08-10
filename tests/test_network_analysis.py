from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from docx import Document

from zhaocai_zhishen.audit_ingestion import ingest_audit_data
from zhaocai_zhishen.audit_schema import ip_network_prefix, normalize_ip_address
from zhaocai_zhishen.network_analysis import analyze_network_records, build_network_analysis


def source_ref(name: str, row: int) -> dict:
    return {
        "source_path": name,
        "source_format": "csv",
        "row_number": row,
        "source_sha256": f"{row:064x}",
    }


def record(record_id: str, bidder_id: str, **values) -> dict:
    row = {
        "record_id": record_id,
        "record_type": values.pop("record_type", "network_event"),
        "project_id": values.pop("project_id", "project-001"),
        "bidder_id": bidder_id,
        "bidder_name": values.pop("bidder_name", bidder_id),
        "source_refs": [source_ref("submission_events.csv", int(record_id.rsplit("-", 1)[-1]))],
    }
    row.update(values)
    return row


class NetworkAnalysisTests(unittest.TestCase):
    def test_same_ip_different_bidders_triggers_traceable_signal(self) -> None:
        rows = [
            record("row-1", "bidder-a", ip_address="203.0.113.8", event_time="2026-08-01T09:00:00+08:00"),
            record("row-2", "bidder-b", ip_address="203.0.113.8", event_time="2026-08-01T09:01:00+08:00"),
        ]
        feature = analyze_network_records(rows)["features"][0]
        self.assertEqual(feature["signal_status"]["shared_full_ip"], "triggered")
        self.assertIn("shared_full_ip", {item["signal_type"] for item in feature["signals"]})
        self.assertNotEqual(feature["risk_level"], "high")
        self.assertEqual(feature["signals"][0]["evidence_refs"][0]["source_path"], "submission_events.csv")
        self.assertNotIn("203.0.113.8", json.dumps(feature, ensure_ascii=False))

    def test_public_exit_ip_is_excluded_and_cannot_create_high_risk(self) -> None:
        rows = [
            record("row-1", "bidder-a", ip_address="198.51.100.10", is_public_exit=True),
            record("row-2", "bidder-b", ip_address="198.51.100.10", is_public_exit=True),
        ]
        feature = analyze_network_records(rows)["features"][0]
        self.assertEqual(feature["signal_status"]["shared_full_ip"], "excluded")
        self.assertEqual(feature["risk_score"], 0)
        self.assertEqual(feature["risk_level"], "none")

    def test_same_subnet_outside_time_window_does_not_trigger(self) -> None:
        rows = [
            record("row-1", "bidder-a", ip_address="203.0.113.10", event_time="2026-08-01T09:00:00+08:00"),
            record("row-2", "bidder-b", ip_address="203.0.113.20", event_time="2026-08-01T12:00:00+08:00"),
        ]
        feature = analyze_network_records(rows, network_window_seconds=1800)["features"][0]
        self.assertEqual(feature["signal_status"]["shared_subnet_time"], "no_signal")
        self.assertNotIn("shared_subnet_time", {item["signal_type"] for item in feature["signals"]})

    def test_same_subnet_signal_does_not_expose_network_prefix(self) -> None:
        rows = [
            record("row-1", "bidder-a", ip_address="203.0.113.10", event_time="2026-08-01T09:00:00+08:00"),
            record("row-2", "bidder-b", ip_address="203.0.113.20", event_time="2026-08-01T09:10:00+08:00"),
        ]
        feature = analyze_network_records(rows, network_window_seconds=1800)["features"][0]
        payload = json.dumps(feature, ensure_ascii=False)
        self.assertEqual(feature["signal_status"]["shared_subnet_time"], "triggered")
        self.assertNotIn("203.0.113.0/24", payload)
        self.assertNotIn("203.0.113.10", payload)
        self.assertNotIn("203.0.113.20", payload)

    def test_same_device_different_accounts_is_independent_signal(self) -> None:
        rows = [
            record("row-1", "bidder-a", device_id="device-x", account_id="account-a"),
            record("row-2", "bidder-b", device_id="device-x", account_id="account-b"),
        ]
        feature = analyze_network_records(rows)["features"][0]
        self.assertEqual(feature["signal_status"]["shared_device"], "triggered")
        self.assertEqual(feature["signal_status"]["shared_account"], "no_signal")

    def test_missing_metadata_is_not_treated_as_no_risk_signal(self) -> None:
        rows = [record("row-1", "bidder-a"), record("row-2", "bidder-b")]
        feature = analyze_network_records(rows)["features"][0]
        self.assertEqual(feature["signal_status"]["shared_file_hash"], "not_provided")
        self.assertEqual(feature["signal_status"]["shared_author"], "not_provided")
        self.assertIn("未提供", feature["interpretation"])

    def test_ipv6_normalization_and_prefix(self) -> None:
        normalized, ok = normalize_ip_address("2001:0db8:0000:0000:0000:0000:0000:0001")
        self.assertTrue(ok)
        self.assertEqual(normalized, "2001:db8::1")
        self.assertEqual(ip_network_prefix(normalized), "2001:db8::/64")
        rows = [
            record("row-1", "bidder-a", ip_address="2001:db8::1"),
            record("row-2", "bidder-b", ip_address="2001:db8::1"),
        ]
        feature = analyze_network_records(
            rows,
            excluded_ips=["2001:0db8:0000:0000:0000:0000:0000:0001"],
        )["features"][0]
        self.assertEqual(feature["signal_status"]["shared_full_ip"], "excluded")

    def test_combined_metadata_signals_form_high_priority_clue(self) -> None:
        shared_hash = "a" * 64
        rows = [
            record("row-1", "bidder-a", record_type="document", file_sha256=shared_hash,
                   author="same-author", file_creator="same-creator", pdf_producer="same-producer",
                   created_at="2026-08-01T09:00:00+08:00", modified_at="2026-08-01T10:00:00+08:00"),
            record("row-2", "bidder-b", record_type="document", file_sha256=shared_hash,
                   author="same-author", file_creator="same-creator", pdf_producer="same-producer",
                   created_at="2026-08-01T09:01:00+08:00", modified_at="2026-08-01T10:01:00+08:00"),
        ]
        feature = analyze_network_records(rows)["features"][0]
        self.assertEqual(feature["risk_level"], "high")
        self.assertGreaterEqual(len(feature["signals"]), 6)
        self.assertTrue(all(signal["evidence_refs"] for signal in feature["signals"]))

    def test_build_network_analysis_writes_standard_outputs(self) -> None:
        rows = [
            record("row-1", "bidder-a", device_id="device-x"),
            record("row-2", "bidder-b", device_id="device-x"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ingestion = root / "ingestion"
            ingestion.mkdir()
            (ingestion / "audit_records.jsonl").write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
            )
            result = build_network_analysis(ingestion, root / "network")
            self.assertEqual(result["feature_count"], 1)
            self.assertEqual(result["edge_count"], 1)
            self.assertTrue((root / "network" / "network_features.jsonl").exists())
            self.assertTrue((root / "network" / "network_graph.json").exists())
            self.assertEqual(result["summary"]["evidence_trace_rate"], 1.0)

    def test_ingestion_extracts_docx_metadata_and_extended_network_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            document_path = root / "projects" / "demo.docx"
            document_path.parent.mkdir()
            document = Document()
            document.add_paragraph("脱敏示例内容")
            document.core_properties.author = "脱敏作者甲"
            document.core_properties.last_modified_by = "脱敏创建者甲"
            document.save(document_path)
            with (root / "samples.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["sample_id", "project_name", "bidder_candidate", "bid_file"])
                writer.writeheader()
                writer.writerow({
                    "sample_id": "sample-001", "project_name": "脱敏项目甲",
                    "bidder_candidate": "脱敏企业甲", "bid_file": "projects/demo.docx",
                })
            with (root / "network_events.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=[
                    "record_type", "record_id", "project_name", "bidder_name", "ip_address",
                    "account_id", "network_role", "is_public_exit", "uploaded_at",
                ])
                writer.writeheader()
                writer.writerow({
                    "record_type": "network_event", "record_id": "network-001",
                    "project_name": "脱敏项目甲", "bidder_name": "脱敏企业甲",
                    "ip_address": "2001:db8::1", "account_id": "account-demo",
                    "network_role": "bidder", "is_public_exit": "否",
                    "uploaded_at": "2026-08-01T09:00:00+08:00",
                })
            ingest_audit_data(root, root / "out")
            records = [json.loads(line) for line in (root / "out" / "audit_records.jsonl").read_text(encoding="utf-8").splitlines()]
            doc_record = next(row for row in records if row["record_type"] == "document")
            network_record = next(row for row in records if row["record_type"] == "network_event")
            self.assertEqual(doc_record["author"], "脱敏作者甲")
            self.assertEqual(doc_record["file_creator"], "脱敏创建者甲")
            self.assertEqual(len(doc_record["file_sha256"]), 64)
            self.assertTrue(any(ref["source_format"] == "docx" for ref in doc_record["source_refs"]))
            self.assertEqual(network_record["account_id"], "account-demo")
            self.assertFalse(network_record["is_public_exit"])
            self.assertEqual(network_record["uploaded_at"], "2026-08-01T09:00:00+08:00")


if __name__ == "__main__":
    unittest.main()
