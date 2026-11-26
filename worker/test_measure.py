"""Executable tests for the trusted local worker."""
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import Mock, patch

from worker import measure


SCRIPT = Path(measure.__file__).resolve()
COMMAND = [sys.executable, "-B", str(SCRIPT), "--child"]


class MeasurementTests(unittest.TestCase):
    def test_records_operation_order_and_times_through_finalization(self):
        events = []
        tick = 0

        def clock():
            events.append("clock")
            return tick

        class TrackedGzip(gzip.GzipFile):
            def __init__(self, **kwargs):
                nonlocal tick
                super().__init__(**kwargs)
                events.append("init")
                tick += 2

            def write(self, data):
                nonlocal tick
                size = super().write(data)
                events.append("write")
                tick += 3
                return size

            def close(self):
                nonlocal tick
                if not self.closed:
                    super().close()
                    events.append("close")
                    tick += 5

        def decode(artifact):
            nonlocal tick
            events.append("decode and validate")
            tick += 20
            return gzip.decompress(artifact)

        def encode(data):
            artifact = measure.encode_gzip(data, stream_factory=TrackedGzip)
            events.append("artifact ready")
            return artifact

        trial = measure.measure(b"finalize me", clock=clock, encode=encode, decode=decode)
        self.assertEqual(events, ["clock", "init", "write", "close", "artifact ready",
                                  "clock", "decode and validate"])
        self.assertEqual(trial.elapsed_ns, 10)
        self.assertTrue(trial.qualified)

    def test_zero_elapsed_keeps_raw_sample_and_fails_qualification(self):
        trial = measure.measure(b"input", clock=lambda: 42)
        self.assertEqual(trial.elapsed_ns, 0)
        self.assertEqual(trial.status, "resolution_limited")
        self.assertEqual(trial.validation, "verified")
        self.assertFalse(trial.qualified)

    def test_reversed_clock_is_an_error(self):
        trial = measure.measure(b"input", clock=iter((42, 41)).__next__)
        self.assertEqual(trial.status, "error")
        self.assertFalse(trial.qualified)

    def test_early_mismatch_remains_visible_after_success(self):
        clock = iter((0, 5, 5, 10)).__next__
        first = measure.measure(b"abc", clock=clock, encode=lambda _: gzip.compress(b"abd"))
        second = measure.measure(b"abc", clock=clock)
        self.assertEqual(first.status, "ok")
        self.assertEqual(first.validation, "mismatch")
        self.assertEqual(first.elapsed_ns, 5)
        self.assertFalse(first.qualified)
        self.assertTrue(second.qualified)

    def test_incomplete_artifact_is_unqualified_even_after_valid_member(self):
        def encode(data):
            return gzip.compress(data) + gzip.compress(b"second member")[:-1]

        trial = measure.measure(b"first member", clock=iter((0, 5)).__next__, encode=encode)
        self.assertEqual(trial.validation, "decode_error")
        self.assertFalse(trial.qualified)


@unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
class WorkerTests(unittest.TestCase):
    def test_worker_round_trips_empty_tiny_and_binary_inputs(self):
        samples = [b"", measure.DEFAULT_INPUT, bytes(range(256)) * 64]
        with tempfile.TemporaryDirectory() as scratch:
            for data in samples:
                with self.subTest(input_bytes=len(data)):
                    result = measure.run_worker(COMMAND, data, timeout=5,
                                                scratch_root=scratch)
                    self.assertTrue(result.qualified, result.report())
                    self.assertIn("status=ok validation=verified qualified=true", result.report())
                    self.assertIn(f"input_bytes={len(data)} ", result.report())
                    self.assertIn("sequence=1 invocation_count=1", result.report())
                    self.assertEqual(list(Path(scratch).iterdir()), [])

    def test_worker_retains_measurement_for_completed_mismatch(self):
        report = json.dumps([5, 0, 20, "mismatch"])
        statement = f"print({report!r}); raise SystemExit({measure.UNQUALIFIED_EXIT})"
        result = measure.run_worker([sys.executable, "-c", statement], b"", timeout=5)
        self.assertFalse(result.qualified)
        self.assertIn("status=ok validation=mismatch qualified=false", result.report())
        self.assertEqual(result.trial.elapsed_ns, 5)

    def test_worker_exit_discards_even_complete_looking_partial_report(self):
        report = json.dumps([5, 0, 20, "verified"])
        command = [sys.executable, "-c", f"print({report!r}); raise SystemExit(9)"]
        result = measure.run_worker(command, b"", timeout=5)
        self.assertFalse(result.qualified)
        self.assertIn("status=error qualified=false exit_code=9", result.report())
        self.assertNotIn("status=ok", result.report())

    def test_timeout_discards_report_and_terminates_worker_and_child(self):
        import fcntl

        with tempfile.TemporaryDirectory() as root:
            tmp = Path(root)
            scratch = tmp / "scratch"
            scratch.mkdir()
            lock_path = tmp / "descendant.lock"
            inside = textwrap.dedent("""
                import fcntl, os, sys, time
                with open(sys.argv[1], 'w') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    lock.write(str(os.getpid()))
                    lock.flush()
                    time.sleep(60)
                """)
            outside = textwrap.dedent("""
                import subprocess, sys, time
                subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]])
                print('[5, 0, 20, "verified"]', flush=True)
                time.sleep(60)
                """)
            command = [sys.executable, "-c", outside, inside, str(lock_path)]
            result = measure.run_worker(command, b"", timeout=2, scratch_root=scratch)
            self.assertFalse(result.qualified)
            self.assertIn("status=timeout qualified=false", result.report())
            self.assertNotIn("status=ok", result.report())
            self.assertEqual(list(scratch.iterdir()), [])
            pid = result.pid
            with self.assertRaises(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)
            self.assertGreater(int(lock_path.read_text()), 0)
            # A dead but unreaped descendant cannot keep a file lock held.
            deadline = time.monotonic() + 2
            with lock_path.open() as lock:
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.01)

    def test_keyboard_interrupt_cancels_and_reaps_owned_group(self):
        process = Mock(pid=1234)
        process.wait.side_effect = [KeyboardInterrupt, -9]
        with tempfile.TemporaryDirectory() as scratch:
            with patch.object(measure.subprocess, "Popen", return_value=process):
                with patch.object(measure.os, "killpg") as killpg:
                    with self.assertRaises(KeyboardInterrupt):
                        measure.run_worker(COMMAND, b"", timeout=5, scratch_root=scratch)
                    killpg.assert_called_once_with(process.pid, measure.signal.SIGKILL)
            self.assertEqual(process.wait.call_count, 2)
            self.assertEqual(list(Path(scratch).iterdir()), [])

    def test_cli_reads_regular_file_and_reports_clock(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "binary input"
            payload = b"\x00\x01\xff"
            source.write_bytes(payload)
            result = subprocess.run([sys.executable, "-B", str(SCRIPT), str(source)],
                                    capture_output=True, text=True, timeout=10, check=True)
            self.assertEqual(source.read_bytes(), payload)
            self.assertIn("monotonic=true", result.stdout)
            self.assertIn("reported_resolution_ns=", result.stdout)
            self.assertIn("input_bytes=3 ", result.stdout)

    def test_invalid_report_is_an_error(self):
        for statement in (
            "pass",
            "print('x' * 4097)",
            f"print({json.dumps([5, 0, 20, 'mismatch'])!r})",
            f"print({json.dumps([5, 2, 20, 'verified'])!r})",
        ):
            with self.subTest(statement=statement):
                command = [sys.executable, "-c", statement]
                result = measure.run_worker(command, b"", timeout=5)
                self.assertFalse(result.qualified)
                self.assertIn("reason=invalid_worker_report", result.report())

    def test_launch_failure_is_an_error(self):
        with tempfile.TemporaryDirectory() as scratch:
            command = [str(Path(scratch) / "missing-worker")]
            result = measure.run_worker(command, b"", timeout=5, scratch_root=scratch)
            self.assertFalse(result.qualified)
            self.assertIn("reason=worker_launch_failed", result.report())
            self.assertEqual(list(Path(scratch).iterdir()), [])

    def test_oversized_input_never_launches_worker(self):
        with patch.object(measure.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(ValueError, "exceeds 1 MiB"):
                measure.run_worker(COMMAND, bytes(measure.MAX_INPUT_BYTES + 1), timeout=5)
            launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
