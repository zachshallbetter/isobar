"""Exercise the worker, exact wire parsing and two independent evidence readers."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
PYTHON = [sys.executable, "-B"]
READERS = (
    [*PYTHON, "conformance/verify_evidence.py"],
    ["ruby", "conformance/verify_evidence.rb"],
)


def invoke(command):
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=10)


def jsonl(records):
    return "".join(json.dumps(record) + "\n" for record in records)


@unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
class EvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = invoke([*PYTHON, "worker/measure.py", "--evidence", "live-🔬"])
        if result.returncode != 0:
            raise AssertionError((result.returncode, result.stdout, result.stderr))
        cls.live = result.stdout
        cls.records = list(map(json.loads, cls.live.splitlines()))

    def assert_verdicts(self, wire, expected):
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "test.jsonl"
            evidence.write_bytes(wire.encode("utf-8") if isinstance(wire, str) else wire)
            for reader in READERS:
                with self.subTest(reader=reader[0]):
                    result = invoke([*reader, str(evidence)])
                    if expected is None:
                        self.assertEqual(result.returncode, 1, result.stderr)
                        self.assertTrue(result.stderr.startswith("Invalid evidence:"), result.stderr)
                        self.assertEqual(result.stdout, "")
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        verdicts = [json.loads(line) for line in result.stdout.splitlines()]
                        self.assertEqual(verdicts, expected)

    def verdict(self, qualified, run_id="live-🔬"):
        return {"run_id": run_id, "observation_id": "encode-1", "qualified": qualified}

    def test_live_output_has_exact_counters_and_reciprocal_links(self):
        observation, validation = self.records
        self.assertEqual(observation["validation_ref"], validation["validation_id"])
        self.assertEqual(observation["observation_id"], validation["observation_id"])
        self.assertEqual(observation["run_id"], validation["run_id"])
        self.assertEqual(observation["work_bytes"], len(b"Isobar single-invocation fixture\n"))
        self.assertIs(type(observation["elapsed_ns"]), int)
        self.assertGreater(observation["elapsed_ns"], 0)
        self.assertEqual(observation["invocation_count"], validation["checked_invocations"])
        self.assertEqual(validation["status"], "verified")
        self.assert_verdicts(self.live, [self.verdict(True)])
        escaped_slash = self.live.replace(r"live-\ud83d\udd2c", r"path\/file")
        self.assert_verdicts(escaped_slash, [self.verdict(True, "path/file")])

    def test_warmup_remains_valid_but_unqualified(self):
        command = [*PYTHON, "worker/measure.py", "--evidence", "warmup", "--warmup"]
        result = invoke(command)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout.splitlines()[0])["phase"], "warmup")
        self.assert_verdicts(result.stdout, [self.verdict(False, "warmup")])

    def fault(self, child_code):
        # Keep the normal CLI and supervisor while injecting a disposable child.
        script = """
import sys
from worker import measure

original = measure.run_worker
child_code = sys.argv.pop(1)
measure.run_worker = lambda _argv, data, **limits: original(
    [sys.executable, "-c", child_code], data, **limits)
raise SystemExit(measure.main())
"""
        return invoke([*PYTHON, "-c", script, child_code, "--evidence", "fault", "--timeout", "0.5"])

    def test_failed_and_timed_out_workers_emit_failure_only(self):
        work = self.records[0]["work_bytes"]
        verified = json.dumps([5, work, 51, "verified"])
        decode_error = json.dumps([5, work, 51, "decode_error"])
        clock_error = json.dumps([-1, work, 51, "verified"])
        faults = [
            ("raise SystemExit(9)", "error"),
            (f"import time; print({verified!r}, flush=True); time.sleep(60)", "timeout"),
            (f"print({decode_error!r}); raise SystemExit(3)", "error"),
            (f"print({clock_error!r}); raise SystemExit(3)", "error"),
        ]
        for child_code, status in faults:
            with self.subTest(child_code=child_code):
                result = self.fault(child_code)
                self.assertEqual(result.returncode, 1, result.stderr)
                (observation,) = list(map(json.loads, result.stdout.splitlines()))
                self.assertEqual(observation["status"], status)
                self.assertTrue(observation["reason"])
                for metric in ("elapsed_ns", "invocation_count", "work_bytes", "validation_ref"):
                    self.assertNotIn(metric, observation)
                self.assert_verdicts(result.stdout, [self.verdict(False, "fault")])

    def test_mismatch_and_resolution_limit_preserve_measurements(self):
        for elapsed, validation_status, status in ((5, "mismatch", "ok"), (0, "verified", "resolution_limited")):
            with self.subTest(status=status):
                private = json.dumps([elapsed, self.records[0]["work_bytes"], 51, validation_status])
                result = self.fault(f"print({private!r}); raise SystemExit(3)")
                self.assertEqual(result.returncode, 1, result.stderr)
                observation, validation = map(json.loads, result.stdout.splitlines())
                self.assertEqual(observation["status"], status)
                self.assertEqual(observation["elapsed_ns"], elapsed)
                self.assertEqual(validation["status"], validation_status)
                self.assert_verdicts(result.stdout, [self.verdict(False, "fault")])

    def test_exact_wire_counter_spellings(self):
        observation, validation = deepcopy(self.records)
        observation["work_bytes"] = "__counter__"
        wire = jsonl([observation, validation])
        valid = ["9007199254740991", "90071992547409910e-1", "1.2E+3",
                 "0.000e99999999999999999999999", "-0e-2"]
        invalid = ["9007199254740992", "9007199254740991.1", "90071992547409910.01e-1",
                   "1e-40000", "1e-99999999999999999999999", "1e400", "-1", "true", "NaN"]
        for token in valid + invalid:
            with self.subTest(token=token):
                self.assert_verdicts(wire.replace('"__counter__"', token),
                                     [self.verdict(True)] if token in valid else None)

    def test_malformed_wire_and_unknown_versions(self):
        first, second = self.live.splitlines(keepends=True)
        for wire in (
            "",
            self.live + "\n",
            '{"status":"error",' + self.live[1:],
            r'{"\u0073tatus":"error",' + self.live[1:],
            "/* comment */ " + self.live,
            self.live.replace('"operation":', '/* interior */ "operation":'),
            self.live.replace("\n", "\r"),
            first.replace("0.1-experimental", "unknown") + second,
            first + second.replace("0.1-experimental", "unknown"),
            b"\xff" + self.live.encode("utf-8"),
            self.live.replace(r"live-\ud83d\udd2c", r"\ud800"),
            self.live.replace(r"live-\ud83d\udd2c", r"\udc00"),
            self.live.replace(r"live-\ud83d\udd2c", r"\ud800\u0041"),
        ):
            with self.subTest(wire=wire):
                self.assert_verdicts(wire, None)

    def test_conflicting_identities_links_and_cadence_are_invalid(self):
        observation, validation = self.records
        for records in (
            [observation, observation, validation],
            [observation, validation, validation],
            [observation],
            [validation],
            [observation, dict(validation, run_id="other")],
            [observation, dict(validation, observation_id="other")],
            [observation, validation, dict(validation, validation_id="orphan")],
            [observation, dict(validation, checked_invocations=2)],
            [observation, dict(validation, cadence="last_invocation", checked_invocations=2)],
        ):
            with self.subTest(records=records):
                self.assert_verdicts(jsonl(records), None)

    def test_links_allow_record_reordering_and_ids_scoped_to_run(self):
        observation, validation = self.records
        missing = dict(observation, run_id="unchecked")
        del missing["validation_ref"]
        batched = dict(observation, run_id="batch", invocation_count=3)
        last = dict(validation, run_id="batch", cadence="last_invocation")
        self.assert_verdicts(
            jsonl([last, validation, observation, batched, missing]),
            [self.verdict(True), self.verdict(False, "batch"), self.verdict(False, "unchecked")],
        )


if __name__ == "__main__":
    unittest.main()
