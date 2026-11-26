"""Run a single trusted, in-memory gzip measurement in a local worker process."""
import argparse
from dataclasses import dataclass
import gzip
import io
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


MAX_INPUT_BYTES = 1024 * 1024
MAX_REPORT_BYTES = 4096
DEFAULT_INPUT = b"Isobar single-invocation fixture\n"
UNQUALIFIED_EXIT = 3


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


def encode_gzip(data, *, stream_factory=gzip.GzipFile):
    sink = io.BytesIO()
    with stream_factory(fileobj=sink, mode="wb", compresslevel=6, mtime=0) as stream:
        stream.write(data)
    return sink.getvalue()


def measure(data, *, clock=time.perf_counter_ns, encode=encode_gzip, decode=gzip.decompress):
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
                return False, f"status=error qualified=false reason=worker_launch_failed errno={error.errno}"
            try:
                exit_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                stop_group(process)
                return False, f"worker_pid={process.pid} status=timeout qualified=false"
            except KeyboardInterrupt:
                stop_group(process)
                raise

            prefix = f"worker_pid={process.pid}"
            if exit_code not in (0, UNQUALIFIED_EXIT):
                return False, f"{prefix} status=error qualified=false exit_code={exit_code}"
            report.seek(0)
            raw = report.read(MAX_REPORT_BYTES + 1)
            expected = (b"status=ok validation=verified qualified=true "
                        if exit_code == 0 else b"status=")
            qualified_field = b" qualified=true " if exit_code == 0 else b" qualified=false "
            if (len(raw) > MAX_REPORT_BYTES or not raw.isascii() or not raw.endswith(b"\n")
                    or not raw.startswith(expected) or raw.count(b"\n") != 1
                    or qualified_field not in raw):
                return False, f"{prefix} status=error qualified=false reason=invalid_worker_report"
            return exit_code == 0, f"{prefix} {raw.decode('ascii').rstrip()}"


def child_main():
    data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(data) > MAX_INPUT_BYTES:
        raise ValueError("input exceeds 1 MiB")
    trial = measure(data)
    print(trial.report())
    return 0 if trial.qualified else UNQUALIFIED_EXIT


def main():
    if sys.argv[1:] == ["--child"]:
        return child_main()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", type=Path, help="regular file, up to 1 MiB")
    parser.add_argument("--timeout", type=float, default=5.0, help="worker wall budget in seconds")
    args = parser.parse_args()
    if os.name != "posix":
        parser.error("the worker requires POSIX process groups")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("timeout must be finite and positive")

    data = DEFAULT_INPUT
    if args.input is not None:
        if not args.input.is_file():
            parser.error("input must be a regular file")
        with args.input.open("rb") as source:
            data = source.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            parser.error("input exceeds 1 MiB")

    clock = time.get_clock_info("perf_counter")
    print(f"platform={platform.system()} kernel={platform.release()} arch={platform.machine()} "
          f"python={platform.python_implementation()}-{platform.python_version()} "
          f"zlib={zlib.ZLIB_RUNTIME_VERSION}")
    print(f"operation=gzip-encode level=6 threads=1 context=fresh clock={clock.implementation} "
          f"monotonic={str(clock.monotonic).lower()} "
          f"reported_resolution_ns={clock.resolution * 1_000_000_000:g}")
    command = [sys.executable, "-B", str(Path(__file__).resolve()), "--child"]
    try:
        qualified, report = run_worker(command, data, timeout=args.timeout)
    except KeyboardInterrupt:
        print("status=cancelled qualified=false")
        return 130
    print(report)
    return 0 if qualified else 1


if __name__ == "__main__":
    sys.exit(main())
