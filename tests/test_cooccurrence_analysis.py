from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from zhaocai_zhishen.cooccurrence_analysis import (
    COOCCURRENCE_ANALYSIS_SCHEMA_VERSION,
    analyze_cooccurrence_records,
    build_cooccurrence_analysis,
    load_cooccurrence_analysis,
)


def source_ref(project: str, row: int) -> dict:
    return {"source_path": f"masked/{project}.csv", "source_format": "csv", "row_number": row, "source_sha256": f"{row:064x}"}


def record(project: str, bidder: str, row: int, **values: object) -> dict:
    result = {
        "record_id": f"{project}-{bidder}-{row}", "record_type": "bid", "project_id": project,
        "bidder_id": bidder, "bidder_name": bidder, "source_refs": [source_ref(project, row)],
    }
    result.update(values)
    return result


def pair(result: dict, left: str, right: str) -> dict:
    for item in result["features"]:
        if {item["bidder_a_id"], item["bidder_b_id"]} == {left, right}:
            return item
    raise AssertionError("pair not found")


class CooccurrenceAnalysisTests(unittest.TestCase):
    def test_single_cooccurrence_excludes_lift_and_pmi_from_risk(self) -> None:
        result = analyze_cooccurrence_records([
            record("p1", "bidder-a", 1), record("p1", "bidder-b", 2),
            record("p2", "bidder-c", 3),
        ])
        feature = pair(result, "bidder-a", "bidder-b")
        self.assertEqual(feature["cooccurrence_count"], 1)
        self.assertEqual(feature["signal_status"]["lift"], "excluded")
        self.assertEqual(feature["signal_status"]["pmi"], "excluded")
        self.assertEqual(feature["risk_score"], 0)
        self.assertTrue(feature["low_support_warning"])

    def test_fixed_pair_has_project_evidence_and_traceable_metrics(self) -> None:
        rows = []
        for index, project in enumerate(("p1", "p2", "p3"), 1):
            rows.extend([
                record(project, "bidder-a", index * 10 + 1, event_time=f"2026-0{index}-01T09:00:00+08:00", agency_id="agency-x"),
                record(project, "bidder-b", index * 10 + 2, event_time=f"2026-0{index}-01T09:01:00+08:00", agency_id="agency-x"),
            ])
        feature = pair(analyze_cooccurrence_records(rows), "bidder-a", "bidder-b")
        self.assertEqual(feature["cooccurrence_count"], 3)
        self.assertEqual(feature["signal_status"]["stable_cooccurrence"], "triggered")
        self.assertEqual(feature["signal_status"]["same_agency"], "triggered")
        self.assertGreater(feature["risk_score"], 0)
        self.assertEqual(len(feature["project_evidence"]), 3)
        self.assertEqual(len(feature["source_record_ids"]), 6)
        self.assertNotIn("agency-x", json.dumps(feature, ensure_ascii=False))

    def test_stable_trio_is_aggregated_as_review_group(self) -> None:
        rows = []
        for index, project in enumerate(("p1", "p2"), 1):
            for offset, bidder in enumerate(("bidder-a", "bidder-b", "bidder-c"), 1):
                rows.append(record(project, bidder, index * 10 + offset, event_time=f"2026-0{index}-01"))
        result = analyze_cooccurrence_records(rows)
        group = next(item for item in result["groups"] if item["member_count"] == 3)
        self.assertEqual(group["group_type"], "stable_multi_bidder_group")
        self.assertEqual(group["stable_common_project_count"], 2)

    def test_alternating_winner_uses_project_time_not_input_order(self) -> None:
        rows = [
            record("p3", "bidder-a", 1, event_time="2026-03-01", is_winner=True), record("p3", "bidder-b", 2, event_time="2026-03-01", is_winner=False),
            record("p1", "bidder-a", 3, event_time="2026-01-01", is_winner=True), record("p1", "bidder-b", 4, event_time="2026-01-01", is_winner=False),
            record("p2", "bidder-a", 5, event_time="2026-02-01", is_winner=False), record("p2", "bidder-b", 6, event_time="2026-02-01", is_winner=True),
        ]
        feature = pair(analyze_cooccurrence_records(rows), "bidder-a", "bidder-b")
        self.assertEqual(feature["alternating_winner_count"], 2)
        self.assertEqual(feature["signal_status"]["alternating_winner"], "triggered")
        self.assertEqual(feature["project_ids"], ["p1", "p2", "p3"])

    def test_cross_project_shared_device_creates_traceable_signal(self) -> None:
        rows = [
            record("p1", "bidder-a", 1, device_id="masked-device"),
            record("p2", "bidder-b", 2, device_id="masked-device"),
        ]
        feature = pair(analyze_cooccurrence_records(rows), "bidder-a", "bidder-b")
        self.assertEqual(feature["cooccurrence_count"], 0)
        self.assertEqual(feature["signal_status"]["shared_device"], "triggered")
        self.assertEqual(feature["review_status"], "pending")
        self.assertNotIn("masked-device", json.dumps(feature, ensure_ascii=False))

    def test_build_outputs_and_empty_load_degrade_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ingestion, output = root / "ingestion", root / "cooccurrence"
            ingestion.mkdir()
            rows = [record("p1", "bidder-a", 1), record("p1", "bidder-b", 2), record("p2", "bidder-a", 3), record("p2", "bidder-b", 4)]
            (ingestion / "audit_records.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            built = build_cooccurrence_analysis(ingestion, output)
            self.assertEqual(built["feature_count"], 1)
            self.assertTrue((output / "bidder_pair_features.jsonl").exists())
            self.assertTrue((output / "group_features.jsonl").exists())
            self.assertTrue((output / "global_graph.json").exists())
            loaded = load_cooccurrence_analysis(output)
            self.assertEqual(loaded["summary"]["schema_version"], COOCCURRENCE_ANALYSIS_SCHEMA_VERSION)
            self.assertGreaterEqual(loaded["graph"]["node_count"], 2)
            empty = load_cooccurrence_analysis(root / "empty")
            self.assertEqual(empty["summary"]["record_count"], 0)
            self.assertEqual(empty["features"], [])


if __name__ == "__main__":
    unittest.main()
