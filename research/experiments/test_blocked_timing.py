"""Executable controls for blocked timing, batch accounting and interval coverage."""
import json
import math
from pathlib import Path
import subprocess
import sys
import unittest

from conformance.verify_evidence import verify_records
from research.experiments import blocked_timing as timing
from worker.measure import Trial


def controlled_records(plan, work=5):
    records = []
    for step in plan:
        baseline = 10 if step.sequence % 2 else 15
        elapsed = baseline if step.config == "reference" else 2 * baseline
        if step.phase == "warmup":
            elapsed *= 100
        count = 1 if step.phase == "warmup" else timing.INVOCATIONS
        records.extend(timing.batch_records(step, [Trial(elapsed, work, 1, "verified")] * count, "test"))
    return records


class BlockedTimingTests(unittest.TestCase):
    def setUp(self):
        self.plan = [step for worker in range(1, timing.WORKERS + 1)
                     for step in timing.schedule(timing.DEFAULT_SEED, worker)]

    def test_seed_and_counterbalancing(self):
        self.assertEqual(self.plan[:20], list(timing.schedule(timing.DEFAULT_SEED, 1)))
        other = [step for worker in range(1, timing.WORKERS + 1)
                 for step in timing.schedule(timing.DEFAULT_SEED + 1, worker)]
        self.assertNotEqual(self.plan, other)
        for worker in range(1, timing.WORKERS + 1):
            steps = [step for step in self.plan if step.worker == worker]
            self.assertEqual([step.sequence for step in steps], list(range(1, 21)))
            for start in range(0, 20, 4):
                first = [step.config for step in steps[start:start + 2]]
                reverse = [step.config for step in steps[start + 2:start + 4]]
                self.assertEqual(first, reverse[::-1])
                self.assertEqual(set(first), {"reference", "candidate"})

    def test_fixed_order_bias_and_balanced_control(self):
        self.assertAlmostEqual(math.exp(timing.log_ratio(100, 300)), 3.0)
        reverse = timing.log_ratio(150, 200)
        self.assertAlmostEqual(math.exp(timing.counterbalanced_pairs(
            [timing.log_ratio(100, 300), reverse]
        )[0]), 2.0)
        summary = timing.summarize(self.plan, controlled_records(self.plan), 5)
        self.assertAlmostEqual(summary["candidate_over_reference"], 2.0)

    def test_serial_work_counts_each_batch_once(self):
        records = controlled_records(self.plan)
        summary = timing.summarize(self.plan, records, 5)
        totals = summary["serial_throughput"]
        self.assertEqual(summary["blocks"], 96)
        self.assertEqual(summary["workers"], 12)
        self.assertEqual(len(summary["worker_log_ratios"]), 12)
        self.assertEqual(totals["reference"], {
            "work_bytes": 1440, "elapsed_ns": 3600, "bytes_per_second": 400_000_000,
        })
        self.assertEqual(totals["candidate"]["elapsed_ns"], 7200)
        with self.assertRaisesRegex(ValueError, "Repeated record identity"):
            timing.summarize(self.plan, [*records, records[-2]], 5)
        duplicate_invocation = dict(records[-2], observation_id="overlapping-per-call")
        with self.assertRaisesRegex(ValueError, "unscheduled batch"):
            timing.summarize(self.plan, [*records, duplicate_invocation], 5)

    def test_failure_or_missing_arm_suppresses_interval_and_rates(self):
        records = controlled_records(self.plan)
        step = self.plan[-1]
        for failure in ("missing", "mismatch", "zero"):
            with self.subTest(failure=failure):
                incomplete = records[:-2]
                if failure == "mismatch":
                    incomplete.extend(timing.batch_records(step, [
                        Trial(10, 5, 1, "verified"), Trial(11, 5, 1, "mismatch"), Trial(10, 5, 1, "verified"),
                    ], "test"))
                    self.assertEqual(incomplete[-1]["status"], "mismatch")
                elif failure == "zero":
                    incomplete.extend(timing.batch_records(step, [
                        Trial(10, 5, 1, "verified"), Trial(0, 5, 1, "verified"), Trial(10, 5, 1, "verified"),
                    ], "test"))
                    self.assertEqual(incomplete[-1]["status"], "error")
                    self.assertNotIn("elapsed_ns", incomplete[-1])
                summary = timing.summarize(self.plan, incomplete, 5)
                self.assertEqual(summary, {"status": "incomplete", "unavailable_observations": [step.observation_id]})

    def test_rejects_wrong_work_count_or_sequence(self):
        records = controlled_records(self.plan)
        for field, wrong in (("work_bytes", 5), ("invocation_count", 1), ("sequence", 99)):
            with self.subTest(field=field):
                altered = [*records[:-2], dict(records[-2], **{field: wrong}), records[-1]]
                with self.assertRaises(ValueError):
                    timing.summarize(self.plan, altered, 5)

    def test_nominal_interval_and_pseudoreplication_control(self):
        scenarios = timing.simulate(timing.DEFAULT_SEED, 2000)["scenarios"]
        for name in ("homogeneous", "heterogeneous"):
            self.assertGreater(scenarios[name]["worker_coverage"], 0.92)
            self.assertLess(scenarios[name]["worker_coverage"], 0.98)
        self.assertGreater(scenarios["homogeneous"]["unclustered_coverage"], 0.92)
        self.assertLess(scenarios["heterogeneous"]["unclustered_coverage"], 0.75)
        self.assertLess(scenarios["skewed"]["worker_coverage"], scenarios["heterogeneous"]["worker_coverage"])
        self.assertGreater(scenarios["skewed"]["worker_coverage"], scenarios["skewed"]["unclustered_coverage"])

    def test_recorded_platform_evidence_recomputes_throughput(self):
        for name, reference_time, candidate_time in (
            ("blocked-macos.jsonl", 37264547, 57077695),
            ("blocked-linux-qemu.jsonl", 55593156, 73610798),
        ):
            with self.subTest(name=name):
                path = Path(__file__).with_name(name)
                records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                verdicts = verify_records(records)
                self.assertEqual(sum(verdict["qualified"] for verdict in verdicts), 192)
                summary = timing.summarize(self.plan, records, len(timing.payload()))
                totals = summary["serial_throughput"]
                self.assertEqual(totals["reference"]["elapsed_ns"], reference_time)
                self.assertEqual(totals["candidate"]["elapsed_ns"], candidate_time)

    @unittest.skipUnless(sys.platform.startswith(("linux", "darwin")), "POSIX pilot")
    def test_independent_processes_emit_verifiable_existing_schema(self):
        run = subprocess.run([
            sys.executable, "-B", "-m", "research.experiments.blocked_timing", "--run-id", "live-test",
        ], check=True, capture_output=True, timeout=60)
        report = json.loads(run.stdout)
        self.assertEqual(report["summary"]["status"], "complete")
        self.assertEqual(len({worker["pid"] for worker in report["workers"]}), timing.WORKERS)
        self.assertTrue(all(worker["clock_cost_ns"]["samples"] == 128 for worker in report["workers"]))
        verdicts = verify_records(report["records"])
        self.assertEqual(len(verdicts), timing.WORKERS * (timing.WARMUP_BLOCKS + timing.MEASURED_BLOCKS) * 2)
        self.assertEqual(sum(verdict["qualified"] for verdict in verdicts), timing.WORKERS * timing.MEASURED_BLOCKS * 2)
        self.assertEqual(report["design"]["input_bytes"], 65536)
        self.assertEqual(report["summary"]["serial_throughput"]["reference"]["work_bytes"], 18874368)


if __name__ == "__main__":
    unittest.main()
