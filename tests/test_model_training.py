from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from zhaocai_zhishen.model_inference import run_model_inference
from zhaocai_zhishen.model_training import fit_model, pair_features, score_pair, train_model


class ModelTrainingTests(unittest.TestCase):
    def sample_rows(self):
        return [
            {"project_id": "p1", "same_bidder": "False", "similarity": "0.20", "shared_phones": "[]", "shared_emails": "[]", "shared_credit_codes": "[]", "shared_contacts": "[]", "shared_addresses": "[]"},
            {"project_id": "p2", "same_bidder": "False", "similarity": "0.25", "shared_phones": "[]", "shared_emails": "[]", "shared_credit_codes": "[]", "shared_contacts": "[]", "shared_addresses": "[]"},
            {"project_id": "p3", "same_bidder": "False", "similarity": "0.30", "shared_phones": "[]", "shared_emails": "[]", "shared_credit_codes": "[]", "shared_contacts": "[]", "shared_addresses": "[]"},
            {"project_id": "p3", "same_bidder": "False", "similarity": "0.55", "shared_phones": '["02112345678"]', "shared_emails": "[]", "shared_credit_codes": "[]", "shared_contacts": "[]", "shared_addresses": '["测试地址"]'},
        ]

    def test_pair_features_count_shared_entities(self):
        values = pair_features(self.sample_rows()[-1])
        self.assertEqual(values["shared_phones_count"], 1)
        self.assertEqual(values["shared_addresses_count"], 1)
        self.assertEqual(values["shared_entity_type_count"], 2)

    def test_pair_features_include_repeated_segments(self):
        row = self.sample_rows()[-1] | {"repeated_segment_count": "2", "repeated_segment_chars": "640"}
        values = pair_features(row)
        self.assertEqual(values["repeated_segment_count"], 2)
        self.assertAlmostEqual(values["repeated_segment_chars_k"], 0.64)

    def test_pair_features_include_standardized_anomaly_signals(self):
        row = self.sample_rows()[-1] | {
            "shared_file_authors": '["author"]',
            "shared_network_fingerprints": '["ip", "device"]',
            "shared_bid_accounts": '["account"]',
            "mixed_bid_documents": '["certificate"]',
            "price_pattern_score": "0.96",
            "price_deviation_span": "0.03",
        }
        values = pair_features(row)
        self.assertEqual(values["shared_file_authors_count"], 1)
        self.assertEqual(values["shared_network_fingerprints_count"], 2)
        self.assertEqual(values["shared_bid_accounts_count"], 1)
        self.assertEqual(values["mixed_bid_documents_count"], 1)
        self.assertAlmostEqual(values["price_pattern_score"], 0.96)
        self.assertAlmostEqual(values["price_deviation_span"], 0.03)

    def test_repeated_segments_are_not_a_primary_model_signal(self):
        model = fit_model(self.sample_rows()[:3], ["p1", "p2", "p3"])
        self.assertEqual(model["weights"]["repeated_segment_count"], 0.0)
        self.assertEqual(model["weights"]["repeated_segment_chars_k"], 0.0)

    def test_outlier_scores_above_normal_pair(self):
        rows = self.sample_rows()
        model = fit_model(rows[:3], ["p1", "p2", "p3"])
        self.assertGreater(score_pair(rows[-1], model)["model_score"], score_pair(rows[0], model)["model_score"])

    def test_train_and_infer_write_artifacts(self):
        rows = self.sample_rows()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            analysis = root / "analysis"
            analysis.mkdir()
            fieldnames = list(rows[0].keys())
            with (analysis / "pairwise_similarity.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            summary = train_model(analysis, root / "models", folds=3)
            self.assertEqual(summary["project_count"], 3)
            inference = run_model_inference(analysis, root / "models" / "bid_anomaly_model.json", root / "inference")
            self.assertEqual(inference["pair_count"], 4)
            self.assertTrue((root / "inference" / "model_scored_pairs.csv").exists())


if __name__ == "__main__":
    unittest.main()
