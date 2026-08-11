from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from zhaocai_zhishen.risk_fusion import build_risk_fusion, fuse_risk_sources, load_risk_fusion


def source_ref(row: int, path: str = "internal/private-project.csv") -> dict:
    return {"source_path": path, "source_sha256": f"{row:064x}", "row_number": row, "source_format": "csv"}


def feature(layer: str, feature_id: str, signal_type: str, score: int, *, project_id: str = "p-1", bidder_ids: tuple[str, str] = ("bidder-a", "bidder-b"), source_value: str = "masked-device") -> dict:
    return {
        "feature_id": feature_id,
        "algorithm_version": f"{layer}/1",
        "project_id": project_id,
        "bidder_a_id": bidder_ids[0],
        "bidder_b_id": bidder_ids[1],
        "risk_contributions": [{"signal_type": signal_type, "score": score}],
        "source_record_ids": [f"record-{feature_id}"],
        "evidence_refs": [source_ref(score)],
        "private_value": source_value,
    }


class RiskFusionTests(unittest.TestCase):
    def test_weak_signal_cannot_exceed_low_risk(self) -> None:
        result = fuse_risk_sources(
            quotes={"features": [feature("quote", "quote-weak", "repeated_price_tail", 8)]},
        )
        pair = result["bidder_pairs"][0]
        self.assertEqual(pair["risk_level"], "low")
        self.assertEqual(pair["evidence_chain"]["calculation"]["rule"], "weak_only_cap")

    def test_strong_network_plus_quote_support_can_reach_high(self) -> None:
        network = feature("network", "network-strong", "shared_file_hash", 30)
        quote = feature("quote", "quote-support", "fixed_difference", 16)
        cooccur = feature("cooccurrence", "cooccur-support", "consecutive_cooccurrence", 10, project_id="")
        cooccur["project_ids"] = ["p-1"]
        result = fuse_risk_sources(
            network={"features": [network]}, quotes={"features": [quote]}, cooccurrence={"features": [cooccur]},
        )
        pair = result["bidder_pairs"][0]
        self.assertEqual(pair["risk_level"], "high")
        self.assertGreaterEqual(pair["risk_score"], 50)
        self.assertEqual(pair["evidence_chain"]["calculation"]["rule"], "strong_plus_independent_dimension")

    def test_same_source_feature_and_signal_is_not_double_counted(self) -> None:
        repeated = feature("network", "shared-source", "shared_device", 24)
        result = fuse_risk_sources(network={"features": [repeated, dict(repeated)]})
        pair = result["bidder_pairs"][0]
        self.assertEqual(len(pair["risk_contributions"]), 1)
        self.assertEqual(pair["risk_score"], 24)

    def test_cooccurrence_only_produces_project_pair_and_group_results(self) -> None:
        pair_feature = feature("cooccurrence", "co-pair", "stable_cooccurrence", 22, project_id="")
        pair_feature["project_ids"] = ["p-1", "p-2"]
        group_feature = {
            "feature_id": "co-group", "algorithm_version": "cooccurrence/1", "bidder_ids": ["bidder-a", "bidder-b", "bidder-c"],
            "stable_project_ids": ["p-1", "p-2"], "risk_contributions": [{"signal_type": "stable_group", "score": 16}],
            "source_record_ids": ["record-group"], "evidence_refs": [source_ref(99)],
        }
        result = fuse_risk_sources(cooccurrence={"features": [pair_feature], "groups": [group_feature]})
        self.assertEqual(len(result["projects"]), 2)
        self.assertEqual(len(result["bidder_pairs"]), 1)
        self.assertEqual(len(result["groups"]), 1)
        self.assertTrue(all(item["review_status"] == "pending_review" for item in result["projects"]))

    def test_evidence_is_sanitized_and_traceable(self) -> None:
        secret = "10.0.0.8-secret-device"
        result = fuse_risk_sources(network={"features": [feature("network", "private", "shared_device", 24, source_value=secret)]})
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(secret, encoded)
        self.assertNotIn("internal/private-project.csv", encoded)
        event = result["evidence_events"][0]
        self.assertTrue(event["source_record_ids"])
        self.assertTrue(event["source_refs"][0]["source_ref_id"])
        self.assertNotIn("source_path", event["source_refs"][0])

    def test_missing_input_directories_degrade_safely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "risk"
            built = build_risk_fusion(root / "analysis", root / "network", root / "quotes", root / "cooccur", output)
            loaded = load_risk_fusion(output)
        self.assertEqual(built["project_count"], 0)
        self.assertEqual(loaded["summary"]["evidence_event_count"], 0)
        self.assertEqual(loaded["projects"], [])

    def test_document_and_model_signals_remain_separate_from_bidder_pair(self) -> None:
        analysis = {"anomalies": [{
            "finding_id": "doc-finding", "document_id_a": "doc-a", "document_id_b": "doc-b", "anomaly_score": 40,
            "model_triggered": True, "model_score": 44, "model_threshold": 40, "evidence_pages_a": [1], "evidence_pages_b": [2],
        }]}
        result = fuse_risk_sources(analysis=analysis)
        self.assertEqual(len(result["documents"]), 1)
        self.assertEqual(result["bidder_pairs"], [])
        self.assertEqual(result["documents"][0]["risk_level"], "low")


if __name__ == "__main__":
    unittest.main()
