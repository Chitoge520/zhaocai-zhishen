from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from zhaocai_zhishen.synthetic_benchmark import generate_benchmark, load_benchmark_rows


class SyntheticBenchmarkTests(unittest.TestCase):
    def _write_source(self, path: Path) -> bytes:
        rows = []
        for index in range(4):
            rows.append(
                {
                    "project_id": f"project-{index}",
                    "document_id_a": f"project-{index}-a",
                    "document_id_b": f"project-{index}-b",
                    "bidder_a": "甲公司",
                    "bidder_b": "乙公司",
                    "same_bidder": "False",
                    "similarity": str(0.2 + index * 0.05),
                    "shared_phones": "[]",
                    "shared_emails": "[]",
                    "shared_credit_codes": "[]",
                    "shared_contacts": "[]",
                    "shared_addresses": "[]",
                    "repeated_segment_count": "0",
                    "repeated_segment_chars": "0",
                    "repeated_segments": "[]",
                    "anomaly_score": "0",
                }
            )
        path.write_text("", encoding="utf-8")
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        return path.read_bytes()

    def test_generation_is_project_isolated_and_auditable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "pairwise_similarity.csv"
            source_bytes = self._write_source(source)
            output = root / "benchmark"
            summary = generate_benchmark(source, output, test_fraction=0.25, seed=11)

            self.assertEqual(summary["source_row_count"], 4)
            self.assertEqual(summary["generated_row_count"], 56)
            self.assertTrue(summary["project_isolation_check"])
            self.assertTrue(set(summary["train_projects"]).isdisjoint(summary["test_projects"]))
            self.assertEqual(len(load_benchmark_rows(output)), 14)
            self.assertEqual(source.read_bytes(), source_bytes)

            test_rows = load_benchmark_rows(output)
            self.assertEqual({row["dataset_type"] for row in test_rows}, {"synthetic_benchmark"})
            self.assertEqual({row["generation_version"] for row in test_rows}, {"bid-synthetic-benchmark/v2"})
            self.assertEqual({row["transform_type"] for row in test_rows}, {
                "clean_pair",
                "shared_phone",
                "shared_address",
                "shared_email",
                "shared_credit_code",
                "copied_custom_error",
                "high_text_similarity",
                "multi_signal",
                "same_file_author",
                "same_network_fingerprint",
                "same_bid_account",
                "mixed_bid_documents",
                "price_pattern",
                "coordinated_price_pattern",
            })
            self.assertTrue(all(row["source_row_id"] for row in test_rows))

    def test_generation_is_reproducible_for_same_seed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "pairwise_similarity.csv"
            self._write_source(source)
            first = root / "first"
            second = root / "second"
            generate_benchmark(source, first, test_fraction=0.5, seed=42)
            generate_benchmark(source, second, test_fraction=0.5, seed=42)
            self.assertEqual((first / "manifest.csv").read_bytes(), (second / "manifest.csv").read_bytes())
            self.assertEqual(
                (first / "generation_summary.json").read_bytes(),
                (second / "generation_summary.json").read_bytes(),
            )

    def test_clean_control_is_not_claimed_as_real_label(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "pairwise_similarity.csv"
            self._write_source(source)
            generate_benchmark(source, root / "benchmark", seed=7)
            rows = load_benchmark_rows(root / "benchmark")
            clean = [row for row in rows if row["transform_type"] == "clean_pair"]
            self.assertTrue(clean)
            self.assertTrue(all(row["synthetic_is_positive"] == "0" for row in clean))
            self.assertTrue(all(row["dataset_type"] == "synthetic_benchmark" for row in clean))
            self.assertNotIn("collusion", {row["synthetic_label"] for row in rows})


if __name__ == "__main__":
    unittest.main()
