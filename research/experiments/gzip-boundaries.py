"""Measure nested elapsed boundaries of one real in-memory gzip invocation."""
import argparse
import gzip
from hashlib import sha256, shake_256
import io
import json
import platform
from random import Random
from statistics import median
import subprocess
import sys
from time import get_clock_info, perf_counter_ns
import zlib


def workloads():
    return {
        "tiny": b"Isobar paired boundary fixture\n",
        "repeated": b"Isobar!!" * 32768,
        "shake256": shake_256(b"isobar gzip boundaries v1").digest(262144),
    }


def measure(data):
    sink = io.BytesIO()
    begin = perf_counter_ns()
    encoder = gzip.GzipFile(fileobj=sink, mode="wb", compresslevel=6, mtime=0)
    opened = perf_counter_ns()
    encoder.write(data)
    written = perf_counter_ns()
    encoder.close()
    closed = perf_counter_ns()
    artifact = sink.getvalue()
    if gzip.decompress(artifact) != data:
        raise AssertionError("Exact gzip round trip failed")
    return {
        "input_bytes": len(data), "artifact_bytes": len(artifact),
        "init_ns": opened - begin, "write_ns": written - opened, "close_ns": closed - written,
        "whole_ns": closed - begin,
    }


def worker(index, rounds):
    inputs = workloads()
    rng = Random(20261126 + index)
    # One unrecorded initialization pass per workload and process.
    for data in inputs.values():
        measure(data)
    observations = []
    for block in range(rounds):
        order = sorted(inputs)
        rng.shuffle(order)
        for position, name in enumerate(order):
            observations.append({"worker": index, "block": block, "position": position,
                                 "workload": name, **measure(inputs[name])})
    return observations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--rounds", type=int, default=4)
    parser.add_argument("--worker-index", type=int)
    args = parser.parse_args()
    if args.workers < 1 or args.rounds < 1:
        parser.error("workers and rounds must be positive")
    if args.worker_index is not None:
        print(json.dumps(worker(args.worker_index, args.rounds)))
        return
    rows = []
    for index in range(args.workers):
        result = subprocess.run(
            [sys.executable, __file__, "--worker-index", str(index), "--rounds", str(args.rounds)],
            check=True, capture_output=True, text=True, timeout=60,
        )
        rows.extend(json.loads(result.stdout))
    clock = get_clock_info("perf_counter")
    metadata = {
        "os": platform.system(), "kernel": platform.release(), "arch": platform.machine(),
        "python": platform.python_version(), "zlib_runtime": zlib.ZLIB_RUNTIME_VERSION,
        "clock": clock.implementation, "reported_clock_resolution_seconds": clock.resolution,
        "workers": args.workers, "rounds": args.rounds, "seed_base": 20261126,
        "input_sha256": {name: sha256(data).hexdigest() for name, data in workloads().items()},
    }
    print(json.dumps({"run": metadata}, sort_keys=True))
    for row in rows:
        print(json.dumps(row, sort_keys=True))
    for name in workloads():
        samples = [row for row in rows if row["workload"] == name]
        paired_share = median((row["whole_ns"] - row["write_ns"]) / row["whole_ns"]
                              for row in samples)
        print(f"{name}: {len(samples)} observations, median omitted share {paired_share:.1%}",
              file=sys.stderr)


if __name__ == "__main__":
    main()
