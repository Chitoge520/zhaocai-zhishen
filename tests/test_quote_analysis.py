from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from zhaocai_zhishen.quote_analysis import (
    QUOTE_ANALYSIS_SCHEMA_VERSION,
    analyze_quote_records,
    build_quote_analysis,
    load_quote_analysis,
)


def record(record_id: str, bidder_id: str, amount: str | None = None, *, project_id: str = "project-1", **extra: object) -> dict[str, object]:
    row: dict[str, object] = {
        "record_id": record_id,
        "record_type": "quote",
        "project_id": project_id,
        "project_name": project_id,
        "bidder_id": bidder_id,
        "bidder_name": bidder_id,
        "amount": amount,
        "currency": "CNY",
        "source_refs": [{
            "source_path": "synthetic/quotes.csv",
            "source_sha256": "a" * 64,
            "row_number": int(record_id.rsplit("-", 1)[-1]) if record_id.rsplit("-", 1)[-1].isdigit() else 1,
        }],
    }
    row.update(extra)
    return row


def feature(result: dict[str, object], bidder_a: str, bidder_b: str) -> dict[str, object]:
    expected = {bidder_a, bidder_b}
    for item in result["features"]:  # type: ignore[index]
        if {item["bidder_a_id"], item["bidder_b_id"]} == expected:  # type: ignore[index]
            return item
    raise AssertionError(f"missing feature for {expected}")


def signal_types(item: dict[str, object]) -> set[str]:
    return {signal["signal_type"] for signal in item["signals"]}  # type: ignore[index]


class QuoteAnalysisTests(unittest.TestCase):
    def test_fixed_difference_and_staircase_are_traceable(self) -> None:
        result = analyze_quote_records([
            record("quote-1", "bidder-a", "100000"),
            record("quote-2", "bidder-b", "110000"),
            record("quote-3", "bidder-c", "120000"),
        ])
        item = feature(result, "bidder-a", "bidder-b")
        self.assertTrue({"fixed_difference", "staircase_quote"}.issubset(signal_types(item)))
        self.assertEqual(item["signal_status"]["fixed_difference"], "triggered")  # type: ignore[index]
        self.assertEqual(item["review_status"], "pending_review")
        for signal in item["signals"]:  # type: ignore[index]
            self.assertTrue(signal["source_record_ids"])
            self.assertTrue(signal["evidence_refs"])

    def test_fixed_ratio_does_not_require_fixed_difference(self) -> None:
        result = analyze_quote_records([
            record("quote-1", "bidder-a", "100000"),
            record("quote-2", "bidder-b", "110000"),
            record("quote-3", "bidder-c", "121000"),
        ])
        item = feature(result, "bidder-a", "bidder-b")
        self.assertIn("fixed_ratio", signal_types(item))
        self.assertNotIn("fixed_difference", signal_types(item))

    def test_discrete_quotes_do_not_trigger_regular_pattern_signals(self) -> None:
        result = analyze_quote_records([
            record("quote-1", "bidder-a", "100000"),
            record("quote-2", "bidder-b", "145000"),
            record("quote-3", "bidder-c", "220000"),
        ])
        item = feature(result, "bidder-a", "bidder-b")
        self.assertFalse({"fixed_difference", "fixed_ratio", "staircase_quote"} & signal_types(item))
        self.assertEqual(item["signal_status"]["fixed_difference"], "no_signal")  # type: ignore[index]

    def test_missing_control_price_is_not_provided_but_other_comparisons_run(self) -> None:
        result = analyze_quote_records([
            record("quote-1", "bidder-a", "100000"),
            record("quote-2", "bidder-b", "100100"),
        ])
        item = feature(result, "bidder-a", "bidder-b")
        self.assertEqual(item["signal_status"]["control_price_ratio"], "not_provided")  # type: ignore[index]
        self.assertIn("pairwise_price_distance", signal_types(item))

    def test_amount_units_are_normalized_and_unknown_units_are_excluded(self) -> None:
        normalized = analyze_quote_records([
            record("quote-1", "bidder-a", "100", amount_unit="10k"),
            record("quote-2", "bidder-b", "1000000", amount_unit="yuan"),
        ])
        item = feature(normalized, "bidder-a", "bidder-b")
        self.assertEqual(item["bidder_a_total_amount"], "1000000.00")
        self.assertEqual(item["bidder_b_total_amount"], "1000000.00")
        self.assertIn("pairwise_price_distance", signal_types(item))

        excluded = analyze_quote_records([
            record("quote-1", "bidder-a", "100", amount_unit="unknown-unit"),
            record("quote-2", "bidder-b", "1000000", amount_unit="yuan"),
        ])
        item = feature(excluded, "bidder-a", "bidder-b")
        self.assertEqual(item["signal_status"]["pairwise_price_distance"], "excluded")  # type: ignore[index]
        self.assertEqual(item["signal_status"]["control_price_ratio"], "excluded")  # type: ignore[index]

    def test_robust_outlier_tail_and_accompaniment_signals(self) -> None:
        outlier = analyze_quote_records([
            record("quote-1", "bidder-a", "100000"),
            record("quote-2", "bidder-b", "101000"),
            record("quote-3", "bidder-c", "102000"),
            record("quote-4", "bidder-d", "1000000"),
        ])
        self.assertIn("median_deviation", signal_types(feature(outlier, "bidder-a", "bidder-d")))

        tails = analyze_quote_records([
            record("quote-1", "bidder-a", "100123"),
            record("quote-2", "bidder-b", "200123"),
            record("quote-3", "bidder-c", "300123"),
        ])
        self.assertIn("repeated_price_tail", signal_types(feature(tails, "bidder-a", "bidder-b")))

        structure = analyze_quote_records([
            record("quote-1", "bidder-a", "80000"),
            record("quote-2", "bidder-b", "99500"),
            record("quote-3", "bidder-c", "100000"),
            record("quote-4", "bidder-d", "100500"),
        ])
        self.assertIn("accompaniment_structure", signal_types(feature(structure, "bidder-a", "bidder-b")))

    def test_item_price_correlation_and_rank_consistency_use_only_items(self) -> None:
        rows: list[dict[str, object]] = []
        for bidder, prices in (("bidder-a", (100, 200, 300)), ("bidder-b", (110, 220, 330))):
            for index, price in enumerate(prices, 1):
                rows.append(record(
                    f"{bidder}-{index}", bidder, None,
                    quote_scope="item", item_code=f"item-{index}", unit_price=str(price),
                ))
        result = analyze_quote_records(rows)
        item = feature(result, "bidder-a", "bidder-b")
        self.assertTrue({"item_price_correlation", "item_rank_consistency"}.issubset(signal_types(item)))
        self.assertEqual(item["signal_status"]["pairwise_price_distance"], "not_provided")  # type: ignore[index]
        self.assertEqual(item["risk_level"], "low")

    def test_build_outputs_and_empty_load_degrade_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ingestion = root / "ingestion"
            ingestion.mkdir()
            rows = [
                record("quote-1", "bidder-a", "100000", control_amount="105000"),
                record("quote-2", "bidder-b", "110000", control_amount="105000"),
                record("quote-3", "bidder-c", "120000", control_amount="105000"),
            ]
            (ingestion / "audit_records.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
            output = root / "quotes"
            built = build_quote_analysis(ingestion, output)
            self.assertEqual(built["feature_count"], 3)
            self.assertTrue((output / "price_features.jsonl").exists())
            self.assertTrue((output / "price_graph.json").exists())
            self.assertTrue((output / "price_analysis_summary.json").exists())
            loaded = load_quote_analysis(output)
            self.assertEqual(loaded["summary"]["schema_version"], QUOTE_ANALYSIS_SCHEMA_VERSION)  # type: ignore[index]
            self.assertEqual(loaded["summary"]["evidence_trace_rate"], 1.0)  # type: ignore[index]
            self.assertTrue(loaded["summary"]["all_triggered_traceable"])  # type: ignore[index]

            empty = load_quote_analysis(root / "empty")
            self.assertEqual(empty["summary"]["record_count"], 0)  # type: ignore[index]
            self.assertEqual(empty["features"], [])


if __name__ == "__main__":
    unittest.main()
