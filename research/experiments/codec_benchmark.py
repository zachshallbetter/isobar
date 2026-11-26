"""Local cross-codec encode pilot using the Isobar blocked design."""
import argparse
from dataclasses import asdict, replace
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

from conformance.check_artifacts import Libdeflate, zlib_identity
from conformance.scoring import score
from research.experiments import blocked_timing as bt
from worker.artifact import decode_gzip
from worker.measure import encode_gzip, measure_artifact, stop_group
from worker.zstandard import Zstandard, digest

REPO = Path(__file__).resolve().parents[2]
CONFIGS = {"reference": "gzip-6", "candidate": "zstd-3"}
EXTENSIONS = {"gzip-6": "gz", "zstd-3": "zst"}
CLOCK = time.perf_counter_ns


def dump(path, data):
    path.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n")


def jsonl(path, rows):
    path.write_text("".join(json.dumps(row, allow_nan=False) + "\n" for row in rows))


def checked(*argv):
    return subprocess.check_output(argv, cwd=REPO, text=True).strip()


def adapters():
    zstd = Zstandard(os.environ["ISOBAR_ZSTD_LIB"])
    reference = Libdeflate(os.environ["ISOBAR_LIBDEFLATE"])
    return (
        {"gzip-6": (encode_gzip, decode_gzip, reference.decode),
         "zstd-3": (zstd.encode, zstd.decode, zstd.decode_frames)},
        {"gzip-6": [zlib_identity(), reference.identity],
         "zstd-3": [zstd.identity]},
    )


def plan(worker):
    return [replace(step, config=CONFIGS[step.config])
            for step in bt.schedule(bt.DEFAULT_SEED, worker)]


def artifact_path(step, invocation):
    return Path("artifacts") / f"{step.observation_id}-{invocation}.{EXTENSIONS[step.config]}"


def child(root, worker):
    source = (root / "input.bin").read_bytes()
    codecs, _ = adapters()
    probes = []
    for _ in range(bt.CLOCK_PROBE_PAIRS):
        started = CLOCK()
        probes.append(CLOCK() - started)

    records, invocations, artifacts = [], [], []
    for step in plan(worker):
        trials = []
        count = 1 if step.phase == "warmup" else bt.INVOCATIONS
        for invocation in range(1, count + 1):
            encode, decode, reference_decode = codecs[step.config]
            trial, artifact = measure_artifact(source, encode=encode, decode=decode)
            reference = reference_decode(artifact, output_limit=len(source)).matches(artifact, source)
            trials.append(trial if reference else replace(trial, validation="mismatch"))
            path = artifact_path(step, invocation)
            invocations.append({
                "observation_id": step.observation_id, "invocation": invocation,
                "elapsed_ns": trial.elapsed_ns, "work_bytes": len(source), "artifact_bytes": len(artifact),
                "artifact": str(path), "candidate_exact": trial.validation == "verified", "reference_exact": reference,
            })
            artifacts.append((path, artifact))
        records.extend(bt.batch_records(step, trials, root.name))

    # Keep filesystem writes outside all measured blocks in this worker.
    for path, artifact in artifacts:
        (root / path).write_bytes(artifact)
    dump(root / f"worker-{worker:02}.json", {
        "worker": worker, "pid": os.getpid(), "clock_probe_ns": probes,
        "records": records, "invocations": invocations,
    })


def capture(root):
    source = root / "input.bin"
    source.write_bytes(bt.payload())
    (root / "artifacts").mkdir()
    _, identities = adapters()
    clock = time.get_clock_info("perf_counter")
    plans = {worker: plan(worker) for worker in range(1, bt.WORKERS + 1)}
    manifest = {
        "run_id": root.name, "commit": checked("git", "rev-parse", "HEAD"),
        "zstd_revision": os.environ.get("ISOBAR_ZSTD_REVISION"),
        "libdeflate_revision": os.environ.get("ISOBAR_LIBDEFLATE_REVISION"),
        "implementations": identities, "os": platform.system(), "os_version": platform.mac_ver()[0],
        "kernel": platform.release(), "arch": platform.machine(),
        "cpu": checked("sysctl", "-n", "machdep.cpu.brand_string") if sys.platform == "darwin" else platform.processor(),
        "python": platform.python_version(), "python_sha256": digest(Path(sys.executable)),
        "clock": clock.implementation, "nominal_clock_resolution_ns": clock.resolution * 1e9,
        "fixture_sha256": digest(source), "fixture_bytes": source.stat().st_size,
        "seed": bt.DEFAULT_SEED, "timeout_seconds": 30, "configurations": CONFIGS,
        "plan": [asdict(step) for steps in plans.values() for step in steps],
        "measurement_invocations_per_batch": bt.INVOCATIONS,
        "warmup_invocations_per_batch": 1,
        "score_profile": {"baseline": "gzip-6", "ratio_bps": 5000, "encode_throughput_bps": 5000},
    }
    dump(root / "manifest.json", manifest)
    records, invocations = [], []
    for worker in plans:
        proc = subprocess.Popen(
            [sys.executable, "-B", "-m", "research.experiments.codec_benchmark",
             str(root), "--worker", str(worker)], cwd=REPO, start_new_session=True,
        )
        try:
            status = proc.wait(timeout=manifest["timeout_seconds"])
        except BaseException:
            stop_group(proc)
            raise
        if status != 0:
            raise RuntimeError(f"worker {worker} exited {status}")
        saved = json.loads((root / f"worker-{worker:02}.json").read_text())
        if saved["pid"] != proc.pid or saved["worker"] != worker:
            raise ValueError("Worker metadata disagrees with launched process")
        records.extend(saved["records"])
        invocations.extend(saved["invocations"])
    jsonl(root / "observations.jsonl", records)
    jsonl(root / "invocations.jsonl", invocations)

    slots = {step.observation_id: step for steps in plans.values() for step in steps}
    if {row["observation_id"] for row in records if "config_id" in row} != slots.keys():
        raise ValueError("Worker evidence does not cover the fixed plan")

    # Reuse the pre-existing timing analysis with the same roles and slot checks.
    roles = {config: role for role, config in CONFIGS.items()}
    analysis_records = [{**row, "config_id": roles[row["config_id"]]} if "config_id" in row else row
                        for row in records]
    timing = bt.summarize([step for worker in plans for step in bt.schedule(bt.DEFAULT_SEED, worker)],
                          analysis_records, source.stat().st_size)
    dump(root / "timing.json", timing)
    if timing["status"] != "complete":
        raise ValueError("The complete planned measurements did not qualify")

    artifacts = []
    for call in invocations:
        step = slots[call["observation_id"]]
        if step.phase != "measurement":
            continue
        path = root / call["artifact"]
        if path.stat().st_size != call["artifact_bytes"]:
            raise ValueError(f"Artifact size disagrees with the invocation record: {path.name}")
        artifacts.append((step.observation_id, source, path))
    python = score(root / "observations.jsonl", CONFIGS["reference"], 5000, artifacts)
    dump(root / "python-score.json", python)
    ruby_command = ["ruby", str(REPO / "conformance/score_evidence.rb"), "observations.jsonl",
                    "--baseline", CONFIGS["reference"], "--ratio-weight-bps", "5000"]
    for observation, original, artifact in artifacts:
        ruby_command.extend(("--artifact", observation, str(original.relative_to(root)),
                             str(artifact.relative_to(root))))
    ruby = json.loads(subprocess.check_output(ruby_command, cwd=root, text=True, timeout=30))
    dump(root / "ruby-score.json", ruby)

    if python["status"] != "complete" or ruby["status"] != "complete":
        raise ValueError("Both ratio-inclusive replays must complete")
    for config in CONFIGS.values():
        left, right = (result["scores"][config]["composite"]["value"] for result in (python, ruby))
        if not math.isclose(left, right, rel_tol=1e-12):
            raise ValueError(f"Independent composite disagrees for {config}")
        ruby["scores"][config]["composite"]["value"] = left
    if python != ruby:
        raise ValueError("Independent score replay disagrees")
    print(root)



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new directory for the evidence bundle")
    parser.add_argument("--worker", type=int, choices=range(1, bt.WORKERS + 1), help=argparse.SUPPRESS)
    args = parser.parse_args()
    root = args.output.resolve()
    if args.worker is None:
        if not 1 <= len(root.name) <= 128:
            parser.error("output directory name must contain 1–128 characters")
        for variable in ("ISOBAR_ZSTD_LIB", "ISOBAR_LIBDEFLATE"):
            if not os.environ.get(variable):
                parser.error(f"set {variable} to a trusted native library")
            os.environ[variable] = str(Path(os.environ[variable]).resolve(strict=True))
        root.mkdir()
        capture(root)
    else:
        child(root, args.worker)


if __name__ == "__main__":
    main()
