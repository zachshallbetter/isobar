"""Aggregate-import conformance and pinned-report integration tests."""
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from conformance.import_reports import (
    LZBENCH_SOURCE, LZ_SPEED, LZ_TIME, TURBOBENCH_REPORT_SHA256, TURBOBENCH_SNAPSHOT, import_report,
)
from research.experiments.blocked_timing import summarize


ROOT = Path(__file__).resolve().parent.parent
PYTHON = [sys.executable, "-B"]
SPEED_CSV = (LZ_SPEED + "\r\nalpha 1,1.20,0.00,900,800,88.89,folder/input,part.txt\r\n").encode()
TIME_CSV = (LZ_TIME + "\nalpha 1,123,0,900,800,88.89,input.txt\n").encode()
TURBO_TABLE = r"""# TurboBench Synthetic
## Platform: control

#### TurboBench: sample time - fixture CPU (bold = pareto)  MB=1.000.000
|C Size|ratio%|C MB/s|D MB/s|Rank|Name|File|
|--------:|-----:|--------:|--------:|----------------|----------------|----------------|
|825| 91.67% |$\color{green}{\textbf{50.00}}$|60.00|1👑|$\color{green}{\textbf{example 1}}$|input.txt|
""".encode("utf-8")


def lz(raw, **kwargs):
    return import_report(raw, format_name="lzbench-csv-v1", **kwargs)


def turbo(raw, **kwargs):
    return import_report(raw, format_name="turbobench-markdown-v1", **kwargs)


def upstream_cli(path, format_name, *arguments):
    result = subprocess.run([*PYTHON, "-m", "conformance.import_reports", str(path),
                             "--format", format_name, *arguments],
                            cwd=ROOT, capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise AssertionError((result.stdout, result.stderr))
    return json.loads(result.stdout)


class ImportTests(unittest.TestCase):
    def test_lzbench_preserves_units_estimator_and_unknown_validation(self):
        digest = sha256(SPEED_CSV).hexdigest()
        report = lz(SPEED_CSV, producer_revision=LZBENCH_SOURCE, producer_version="2.4.2",
                    expected_sha256=digest, declared_estimator="median",
                    source_url="https://example.com/source")
        self.assertEqual(report["status"], "available")
        source = report["provenance"]
        self.assertEqual(source["report_sha256"], digest)
        self.assertEqual(source["producer_version"], "2.4.2")
        self.assertEqual(source["source_url"], "https://example.com/source")
        self.assertIsNone(source["report_revision"])
        (table,) = report["tables"]
        (row,) = table["rows"]
        self.assertEqual(row["file"], "folder/input,part.txt")
        self.assertEqual(row["encode_speed"], {"display": "1.20", "value": "1.20", "unit": "MB/s (decimal)"})
        self.assertIsNone(row["decode_speed"]["value"])
        self.assertEqual(row["decode_speed"]["reason"], "zero_may_indicate_failure_or_rounding")
        self.assertEqual(table["timing"]["declared_estimator"], "median")
        self.assertIn("mixed", table["timing"]["reviewed_sampling"])
        self.assertIsNone(table["validation"]["reported_outcomes"])
        self.assertIsNone(report["comparison"]["timed_boundary"])

    def test_declared_microseconds_and_ambiguous_legacy_speed(self):
        (table,) = lz(TIME_CSV)["tables"]
        (row,) = table["rows"]
        self.assertEqual(row["encode_time"]["value"], "123")
        self.assertEqual(row["encode_time"]["unit"], "us")
        self.assertIsNone(row["decode_time"]["value"])
        self.assertEqual(table["timing"]["precision"], "truncated microseconds")
        (table,) = lz(SPEED_CSV, producer_revision="unknown-build")["tables"]
        (row,) = table["rows"]
        self.assertEqual(row["encode_speed"], {"display": "1.20", "value": None, "unit": None,
                                               "reason": "ambiguous_unit"})
        self.assertIsNone(row["original_bytes"]["value"])
        self.assertIsNone(table["timing"]["reviewed_sampling"])

    def test_turbobench_preserves_annotations_order_and_unknown_rank_scope(self):
        report = turbo(TURBO_TABLE)
        (table,) = report["tables"]
        (row,) = table["rows"]
        self.assertEqual(row["reported_rank"], "1")
        self.assertEqual(row["rank_marker"], "👑")
        self.assertEqual(row["marked_columns"], [2, 5])
        self.assertEqual(row["encode_speed"]["value"], "50.00")
        self.assertEqual(table["displayed_candidates"], [{"name": "example 1", "file": "input.txt"}])
        self.assertIsNone(table["ranking"]["ranked_population"])
        self.assertIsNone(table["ranking"]["reported_active_metrics"])

        ambiguous = turbo(TURBO_TABLE.replace(b"MB=1.000.000", b"MB"))
        speed = ambiguous["tables"][0]["rows"][0]["encode_speed"]
        self.assertEqual(speed["reason"], "ambiguous_unit")
        self.assertIsNone(speed["value"])

    def test_unknown_layout_and_failed_pin_expose_no_rows(self):
        for result, reason in (
            (import_report(SPEED_CSV, format_name="lzbench-csv-v2"), "unsupported_format"),
            (lz(SPEED_CSV.replace(b"Decompression speed", b"Unknown column")), "unsupported_layout"),
            (turbo(TURBO_TABLE.replace(b"|Rank|", b"|Score|")), "unsupported_layout"),
            (turbo(TURBO_TABLE + b"| truncated tail"), "malformed_report"),
            (lz(SPEED_CSV, expected_sha256="0" * 64), "digest_mismatch"),
            (lz(b"\xff"), "malformed_report"),
        ):
            with self.subTest(reason=reason):
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["reason"], reason)
                self.assertNotIn("tables", result)
                self.assertFalse(result["eligible_for_qualification"])

    def test_imports_fail_raw_evidence_scoring_and_uncertainty_gates(self):
        archived = (ROOT / "research/experiments/blocked-macos.jsonl").read_bytes()
        commands = (
            [*PYTHON, "conformance/verify_evidence.py"],
            ["ruby", "conformance/verify_evidence.rb"],
            [*PYTHON, "-m", "conformance.scoring"],
            ["ruby", "conformance/score_evidence.rb"],
        )
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "report.jsonl"
            for report in (lz(SPEED_CSV), turbo(TURBO_TABLE)):
                self.assertTrue(report["aggregate_only"])
                self.assertFalse(report["eligible_for_qualification"])
                self.assertFalse(report["eligible_for_uncertainty"])
                (table,) = report["tables"]
                (row,) = table["rows"]
                for value, prefix in ((report, b""), (row, archived)):
                    with self.assertRaisesRegex(ValueError, "requires IBS-0003"):
                        summarize([], [value], 1)
                    path.write_bytes(prefix + json.dumps(value).encode() + b"\n")
                    for index, command in enumerate(commands):
                        args = ["--baseline", "reference", "--ratio-weight-bps", "0"] if index >= 2 else []
                        result = subprocess.run([*command, str(path), *args], cwd=ROOT,
                                                capture_output=True, text=True, timeout=10)
                        self.assertEqual(result.returncode, 1, (command, result.stderr))
                        self.assertEqual(result.stdout, "")


@unittest.skipUnless(os.environ.get("ISOBAR_LZBENCH_REPORT_DIR") and os.environ.get("ISOBAR_TURBOBENCH_REPORT"),
                     "requires the pinned upstream reports described in conformance/README.md")
class PinnedUpstreamTests(unittest.TestCase):
    def test_real_lzbench_speed_and_time_csv(self):
        folder = Path(os.environ["ISOBAR_LZBENCH_REPORT_DIR"])
        self.assertEqual((folder / "version.txt").read_text().splitlines()[0],
                         "lzbench 2.4.2 (commit 4afc06fa5fa9)")
        binary_digest = sha256((folder / "lzbench").read_bytes()).hexdigest()
        for mode, unit in (("speed", "MB/s (decimal)"), ("time", "us")):
            path = folder / f"{mode}.csv"
            report = upstream_cli(
                path, "lzbench-csv-v1", "--producer-revision", LZBENCH_SOURCE,
                "--producer-version", "2.4.2", "--producer-sha256", binary_digest,
                "--declared-estimator", "median", "--expected-sha256", sha256(path.read_bytes()).hexdigest(),
            )
            (table,) = report["tables"]
            self.assertEqual(report["provenance"]["producer_sha256"], binary_digest)
            self.assertEqual([row["name"] for row in table["rows"]], ["zstd 1.5.7 -1", "zstd 1.5.7 -3"])
            self.assertEqual([row["original_bytes"]["value"] for row in table["rows"]], ["237568"] * 2)
            self.assertTrue(all(row[f"encode_{mode}"]["unit"] == unit for row in table["rows"]))

    def test_real_turbobench_published_report_keeps_tables_separate(self):
        remote = f"https://github.com/powturbo/TurboBench/blob/{TURBOBENCH_SNAPSHOT}/benchmark.md"
        report = upstream_cli(
            os.environ["ISOBAR_TURBOBENCH_REPORT"], "turbobench-markdown-v1", "--source-url", remote,
            "--report-revision", TURBOBENCH_SNAPSHOT, "--expected-sha256", TURBOBENCH_REPORT_SHA256,
        )
        self.assertEqual(report["provenance"]["report_sha256"], TURBOBENCH_REPORT_SHA256)
        self.assertIsNone(report["provenance"]["producer_revision"])
        tables = report["tables"]
        self.assertEqual(len(tables), 8)
        self.assertEqual(sum(len(table["rows"]) for table in tables), 273)
        self.assertEqual([table["platform"] for table in tables].count("macos-aarch64"), 2)
        self.assertEqual(tables[0]["rows"][0]["name"], "brotli 6")
        self.assertEqual(tables[0]["rows"][0]["reported_rank"], "25")
        self.assertEqual(tables[0]["ranking"]["reviewed_method"], "Copeland with Borda tie-break")
        self.assertIsNone(tables[0]["ranking"]["ranked_population"])


if __name__ == "__main__":
    unittest.main()
