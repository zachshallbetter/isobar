"""Run a single trusted, in-memory gzip measurement in a local worker process."""
import argparse
from dataclasses import astuple, dataclass
import gzip
import io
import json
import math
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import tempfile
import time
import zlib

if __package__:
    from .artifact import MAX_DECODED_BYTES, decode_gzip
else:
    from artifact import MAX_DECODED_BYTES, decode_gzip


MAX_INPUT_BYTES = MAX_DECODED_BYTES
MAX_REPORT_BYTES = 4096
DEFAULT_INPUT = b"Isobar single-invocation fixture\n"
UNQUALIFIED_EXIT = 3
SCHEMA_VERSION = "0.1-experimental"
MAX_COUNTER = (1 << 53) - 1


@dataclass(frozen=True)
class Trial:
    """Internal timing and validation outcome for one encode."""

    elapsed_ns: int
    input_bytes: int
    artifact_bytes: int
    validation: str

    @property
    def status(self):
        if self.elapsed_ns < 0:
            return "error"
        if self.elapsed_ns == 0:
            return "resolution_limited"
        return "ok"

    @property
    def qualified(self):
        return self.status == "ok" and self.validation == "verified"

    def report(self):
        return (f"status={self.status} validation={self.validation} "
                f"qualified={str(self.qualified).lower()} sequence=1 invocation_count=1 "
                f"elapsed_ns={self.elapsed_ns} input_bytes={self.input_bytes} "
                f"artifact_bytes={self.artifact_bytes}")


@dataclass(frozen=True)
class WorkerResult:
    """Supervisor outcome used by the local CLI."""

    pid: int | None
    status: str
    reason: str | None = None
    trial: Trial | None = None

    @property
    def qualified(self):
        return self.trial is not None and self.trial.qualified

    def report(self):
        prefix = f"worker_pid={self.pid} " if self.pid is not None else ""
        if self.trial is not None:
            return prefix + self.trial.report()
        reason = f" {self.reason}" if self.reason is not None else ""
        return f"{prefix}status={self.status} qualified=false{reason}"


def evidence_records(result, run_id, phase):
    observation = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "observation_id": "encode-1",
        "worker_id": "worker-1",
        "block_id": "block-1",
        "sequence": 1,
        "input_id": "input-1",
        "config_id": "gzip-6",
        "operation": "encode",
        "phase": phase,
        "status": result.status,
    }
    trial = result.trial
    if trial is None:
        observation["reason"] = result.reason or "worker_timeout"
    elif trial.status == "error":
        observation["reason"] = "clock_regressed"
    elif trial.elapsed_ns > MAX_COUNTER:
        observation.update(status="error", reason="elapsed_out_of_range")
    elif trial.validation == "decode_error":
        observation.update(status="error", reason="decode_failed")
    else:
        observation.update(elapsed_ns=trial.elapsed_ns, invocation_count=1,
                           work_bytes=trial.input_bytes, validation_ref="validation-1")
        if trial.status == "resolution_limited":
            observation["reason"] = "clock_interval_zero"
        yield observation
        yield {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "validation_id": observation["validation_ref"],
            "observation_id": observation["observation_id"],
            "cadence": "every_invocation",
            "checked_invocations": 1,
            "status": trial.validation,
            "method": "exact_bytes",
        }
        return
    yield observation


def encode_gzip(data, *, stream_factory=gzip.GzipFile):
    sink = io.BytesIO()
    with stream_factory(fileobj=sink, mode="wb", compresslevel=6, mtime=0) as stream:
        stream.write(data)
    return sink.getvalue()


def measure(data, *, clock=time.perf_counter_ns, encode=encode_gzip, decode=decode_gzip):
    started = clock()
    artifact = encode(data)
    elapsed = clock() - started

    # The built-in encoder controls artifact size and content. Validation runs
    # after the final clock reading and checks the entire decoded output.
    try:
        reconstructed = decode(artifact)
    except (EOFError, OSError, zlib.error):
        validation = "decode_error"
    else:
        validation = "verified" if reconstructed == data else "mismatch"
    return Trial(elapsed, len(data), len(artifact), validation)


def stop_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_worker(argv, data, *, timeout, scratch_root=None):
    """Run the built-in command, or a fixture command in tests."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    if len(data) > MAX_INPUT_BYTES:
        raise ValueError("input exceeds 1 MiB")

    with tempfile.TemporaryDirectory(prefix="isobar-worker-", dir=scratch_root) as scratch:
        input_path = Path(scratch) / "input.bin"
        input_path.write_bytes(data)
        with input_path.open("rb") as source, tempfile.TemporaryFile(dir=scratch) as report:
            try:
                process = subprocess.Popen(
                    argv, cwd=scratch, stdin=source, stdout=report, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as error:
                return WorkerResult(None, "error", f"reason=worker_launch_failed errno={error.errno}")
            try:
                exit_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                stop_group(process)
                return WorkerResult(process.pid, "timeout")
            except KeyboardInterrupt:
                stop_group(process)
                raise

            if exit_code not in (0, UNQUALIFIED_EXIT):
                return WorkerResult(process.pid, "error", f"exit_code={exit_code}")
            report.seek(0)
            raw = report.read(MAX_REPORT_BYTES + 1)
            try:
                if len(raw) > MAX_REPORT_BYTES:
                    raise ValueError("oversized worker report")
                values = json.loads(raw)
                if not isinstance(values, list):
                    raise ValueError("worker report must be a list")
                trial = Trial(*values)
                if (type(trial.elapsed_ns) is not int or type(trial.input_bytes) is not int
                        or trial.input_bytes != len(data) or type(trial.artifact_bytes) is not int
                        or trial.artifact_bytes < 1
                        or trial.validation not in ("verified", "mismatch", "decode_error")
                        or trial.qualified != (exit_code == 0)):
                    raise ValueError("inconsistent worker report")
            except (TypeError, ValueError):
                return WorkerResult(process.pid, "error", "reason=invalid_worker_report")
            return WorkerResult(process.pid, trial.status, trial=trial)


def child_main():
    data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(data) > MAX_INPUT_BYTES:
        raise ValueError("input exceeds 1 MiB")
    trial = measure(data)
    print(json.dumps(astuple(trial)))
    return 0 if trial.qualified else UNQUALIFIED_EXIT


def main():
    if sys.argv[1:] == ["--child"]:
        return child_main()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", type=Path, help="regular file, up to 1 MiB")
    parser.add_argument("--timeout", type=float, default=5.0, help="worker wall budget in seconds")
    parser.add_argument("--evidence", metavar="RUN_ID", help="emit experimental IBS-0003 JSONL")
    parser.add_argument("--warmup", action="store_true", help="mark evidence as a warm-up")
    args = parser.parse_args()
    if os.name != "posix":
        parser.error("the worker requires POSIX process groups")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("timeout must be finite and positive")

    if args.evidence is not None:
        if not 1 <= len(args.evidence) <= 128:
            parser.error("run ID must contain 1–128 characters")
        try:
            args.evidence.encode("utf-8")
        except UnicodeEncodeError:
            parser.error("run ID must be valid Unicode")
    elif args.warmup:
        parser.error("--warmup requires --evidence")

    data = DEFAULT_INPUT
    if args.input is not None:
        if not args.input.is_file():
            parser.error("input must be a regular file")
        with args.input.open("rb") as source:
            data = source.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            parser.error("input exceeds 1 MiB")

    if args.evidence is None:
        clock = time.get_clock_info("perf_counter")
        print(f"platform={platform.system()} kernel={platform.release()} arch={platform.machine()} "
              f"python={platform.python_implementation()}-{platform.python_version()} "
              f"zlib={zlib.ZLIB_RUNTIME_VERSION}")
        print(f"operation=gzip-encode level=6 threads=1 context=fresh clock={clock.implementation} "
              f"monotonic={str(clock.monotonic).lower()} "
              f"reported_resolution_ns={clock.resolution * 1_000_000_000:g}")
    phase = "warmup" if args.warmup else "measurement"
    command = [sys.executable, "-B", str(Path(__file__).resolve()), "--child"]
    interrupted = False
    try:
        result = run_worker(command, data, timeout=args.timeout)
    except KeyboardInterrupt:
        if args.evidence is None:
            print("status=cancelled qualified=false")
            return 130
        interrupted = True
        result = WorkerResult(None, "error", "interrupted")

    if args.evidence is None:
        print(result.report())
        return 0 if result.qualified else 1

    records = tuple(evidence_records(result, args.evidence, phase))
    for record in records:
        print(json.dumps(record))
    if interrupted:
        return 130
    return 0 if result.qualified and records[0]["status"] == "ok" and phase == "measurement" else 1


if __name__ == "__main__":
    sys.exit(main())
