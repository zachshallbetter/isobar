"""Fixed-design blocked gzip timing pilot and interval coverage experiment."""
import argparse
from collections import defaultdict
from dataclasses import asdict, dataclass
from functools import partial
from hashlib import sha256, shake_256
import json
import math
import os
import platform
from random import Random
from pathlib import Path
from statistics import mean, median, stdev
import subprocess
import sys
import time
import zlib

from worker.measure import MAX_COUNTER, SCHEMA_VERSION, encode_gzip, measure


WORKERS = 12
WARMUP_BLOCKS = 2
MEASURED_BLOCKS = 8
INVOCATIONS = 3
WORKER_TIMEOUT_SECONDS = 10
CLOCK_PROBE_PAIRS = 128
DEFAULT_SEED = 20261126
LEVELS = {"reference": 1, "candidate": 6}
WORKER_T = 2.201  # 0.975 quantile, 11 degrees of freedom
UNCLUSTERED_T = 2.012  # 0.975 quantile, 47 degrees of freedom


@dataclass(frozen=True)
class Step:
    worker: int
    block: int
    sequence: int
    phase: str
    config: str

    @property
    def observation_id(self):
        return f"w{self.worker}-s{self.sequence}"


def schedule(seed, worker):
    rng = Random(seed + worker)
    block = 0
    sequence = 0
    for phase, blocks in (("warmup", WARMUP_BLOCKS), ("measurement", MEASURED_BLOCKS)):
        for _ in range(blocks // 2):
            order = ("reference", "candidate")
            if rng.getrandbits(1):
                order = order[::-1]
            for pair in (order, order[::-1]):
                block += 1
                for config in pair:
                    sequence += 1
                    yield Step(worker, block, sequence, phase, config)


def payload():
    fixed = (b"city=portland status=ok event=search\n" * 100)[:3072]
    entropy = shake_256(b"Isobar blocked gzip pilot fixture v1").digest(16 * 1024)
    return b"".join(fixed + entropy[start:start + 1024] for start in range(0, len(entropy), 1024))


def batch_records(step, trials, run_id):
    count = 1 if step.phase == "warmup" else INVOCATIONS
    if len(trials) != count:
        raise ValueError(f"Incorrect invocation count for {step.observation_id}")
    observation = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "observation_id": step.observation_id,
        "worker_id": f"worker-{step.worker}",
        "block_id": f"block-{step.block}",
        "sequence": step.sequence,
        "input_id": "blocked-gzip-v1",
        "config_id": step.config,
        "operation": "encode",
        "phase": step.phase,
        "status": "ok",
    }
    elapsed = sum(trial.elapsed_ns for trial in trials)
    if any(trial.validation == "decode_error" for trial in trials):
        observation.update(status="error", reason="batch_decode_failed")
    elif any(trial.elapsed_ns <= 0 for trial in trials):
        observation.update(status="error", reason="batch_nonpositive_interval")
    elif elapsed > MAX_COUNTER:
        observation.update(status="error", reason="batch_interval_out_of_range")
    else:
        observation.update(elapsed_ns=elapsed, invocation_count=len(trials),
                           work_bytes=sum(trial.input_bytes for trial in trials),
                           validation_ref=f"v-{step.observation_id}")
        validation = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "validation_id": observation["validation_ref"],
            "observation_id": step.observation_id,
            "cadence": "every_invocation",
            "checked_invocations": len(trials),
            "status": ("verified" if all(trial.qualified for trial in trials) else "mismatch"),
            "method": "exact_bytes",
        }
        return [observation, validation]
    return [observation]


def run_child(seed, worker, run_id):
    data = payload()
    clock_costs = []
    clock = time.perf_counter_ns
    for _ in range(CLOCK_PROBE_PAIRS):
        started = clock()
        clock_costs.append(clock() - started)

    records = []
    encoders = {config: partial(encode_gzip, level=level) for config, level in LEVELS.items()}
    for step in schedule(seed, worker):
        count = 1 if step.phase == "warmup" else INVOCATIONS
        trials = [measure(data, encode=encoders[step.config]) for _ in range(count)]
        records.extend(batch_records(step, trials, run_id))

    return {"records": records, "pid": os.getpid(), "clock_cost_ns": {
        "samples": len(clock_costs), "min": min(clock_costs), "median": median(clock_costs),
    }}


def interval(values, critical):
    estimate = mean(values)
    margin = critical * stdev(values) / math.sqrt(len(values))
    return estimate, estimate - margin, estimate + margin


def blocked_interval(worker_blocks):
    # Each worker contributes one equally weighted summary regardless of invocation count.
    return interval([mean(blocks) for blocks in worker_blocks], WORKER_T)


def counterbalanced_pairs(blocks):
    return [mean(blocks[index:index + 2]) for index in range(0, len(blocks), 2)]


def log_ratio(reference_ns, candidate_ns):
    return math.log(candidate_ns / reference_ns)


def summarize(plan, records, work_per_invocation):
    observations = {}
    validations = {}
    run_ids = set()
    for record in records:
        if not isinstance(record, dict) or not {"run_id", "schema_version"} <= record.keys():
            raise ValueError("Timing analysis requires IBS-0003 records")
        if record["schema_version"] != SCHEMA_VERSION:
            raise ValueError("Unsupported IBS-0003 record version")
        run_ids.add(record["run_id"])
        if "validation_id" in record:
            index, key = validations, record["validation_id"]
        else:
            index, key = observations, record["observation_id"]
        if key in index:
            raise ValueError(f"Repeated record identity: {key}")
        index[key] = record

    if len(run_ids) > 1:
        raise ValueError("Expected one run for the fixed design")
    expected = {step.observation_id for step in plan}
    if observations.keys() - expected:
        raise ValueError("Observed an unscheduled batch")
    paired = defaultdict(dict)
    unavailable = []
    totals = {config: {"work_bytes": 0, "elapsed_ns": 0} for config in LEVELS}

    for step in plan:
        observation = observations.get(step.observation_id)
        if observation is not None:
            actual = (observation["worker_id"], observation["block_id"], observation["phase"],
                      observation["sequence"], observation["config_id"])
            required = (f"worker-{step.worker}", f"block-{step.block}", step.phase,
                        step.sequence, step.config)
            if actual != required:
                raise ValueError(f"Observation differs from schedule: {step.observation_id}")
        if step.phase != "measurement":
            continue

        validation = validations.get(observation.get("validation_ref")) if observation else None
        if (not observation or observation["status"] != "ok" or not validation
                or validation["status"] != "verified"):
            unavailable.append(step.observation_id)
            continue
        if (observation["invocation_count"] != INVOCATIONS
                or observation["work_bytes"] != work_per_invocation * INVOCATIONS
                or observation["elapsed_ns"] <= 0
                or validation["observation_id"] != step.observation_id
                or validation["run_id"] != observation["run_id"]
                or validation["checked_invocations"] != INVOCATIONS
                or validation["cadence"] != "every_invocation"):
            raise ValueError(f"Unexpected batch measurement or validation: {step.observation_id}")

        elapsed = observation["elapsed_ns"]
        paired[step.worker, step.block][step.config] = elapsed
        totals[step.config]["elapsed_ns"] += elapsed
        totals[step.config]["work_bytes"] += observation["work_bytes"]

    if unavailable:
        return {"status": "incomplete", "unavailable_observations": unavailable}

    worker_blocks = [[] for _ in range(WORKERS)]
    for (worker, _), times in paired.items():
        worker_blocks[worker - 1].append(log_ratio(times["reference"], times["candidate"]))
    if any(len(blocks) != MEASURED_BLOCKS for blocks in worker_blocks):
        raise ValueError("Schedule does not meet fixed-design worker and block counts")

    estimate, lower, upper = blocked_interval(worker_blocks)
    for total in totals.values():
        total["bytes_per_second"] = total["work_bytes"] * 1_000_000_000 / total["elapsed_ns"]
    durations = defaultdict(list)
    for observation in observations.values():
        validation = validations.get(observation.get("validation_ref"))
        if observation["status"] == "ok" and validation and validation["status"] == "verified":
            durations[observation["config_id"], observation["phase"]].append(
                observation["elapsed_ns"] / observation["invocation_count"]
            )
    observational_means = {config: {} for config in LEVELS}
    for (config, phase), values in durations.items():
        observational_means[config][phase] = median(values)
    return {
        "status": "complete", "workers": len(worker_blocks), "blocks": len(paired),
        "candidate_over_reference": math.exp(estimate),
        "interval_95": [math.exp(lower), math.exp(upper)],
        "worker_log_ratios": [mean(blocks) for blocks in worker_blocks],
        "worker_log_ratio_sd": stdev(mean(blocks) for blocks in worker_blocks),
        "within_worker_log_ratio_sd": [stdev(blocks) for blocks in worker_blocks],
        "median_ns_per_invocation": observational_means,
        "serial_throughput": totals,
    }


def simulate(seed, repetitions):
    """Compare worker-cluster and incorrectly unclustered intervals under a known effect."""
    true_effect = math.log(1.08)
    rng = Random(seed)
    scenarios = {}
    for name, worker_sd in (("homogeneous", 0.0), ("heterogeneous", 0.16), ("skewed", 0.16)):
        covered = [0, 0]
        for _ in range(repetitions):
            workers = []
            for _ in range(WORKERS):
                if name == "skewed":
                    worker_effect = worker_sd * (rng.expovariate(1) - 1)
                else:
                    worker_effect = rng.gauss(0, worker_sd)
                blocks = [true_effect + worker_effect + rng.gauss(0, 0.06)
                          + (0.18 if index % 2 == 0 else -0.18)
                          for index in range(MEASURED_BLOCKS)]
                workers.append(blocks)
            independent = blocked_interval(workers)
            pooled = [pair for blocks in workers for pair in counterbalanced_pairs(blocks)]
            unclustered = interval(pooled, UNCLUSTERED_T)
            for index, (_, lo, hi) in enumerate((independent, unclustered)):
                covered[index] += lo <= true_effect <= hi
        scenarios[name] = {
            "worker_coverage": covered[0] / repetitions, "unclustered_coverage": covered[1] / repetitions,
        }
    return {"seed": seed, "repetitions": repetitions, "true_ratio": 1.08, "scenarios": scenarios}


def pilot(seed, run_id):
    metadata = time.get_clock_info("perf_counter")
    data = payload()
    plan = []
    workers = []
    records = []
    for worker in range(1, WORKERS + 1):
        plan.extend(schedule(seed, worker))
        argv = [sys.executable, "-B", "-m", "research.experiments.blocked_timing",
                "--worker", str(worker), "--seed", str(seed), "--run-id", run_id]
        try:
            result = subprocess.run(argv, capture_output=True, check=True, text=True,
                                    timeout=WORKER_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            workers.append({"worker": worker, "status": "timeout"})
            continue
        except subprocess.CalledProcessError as failure:
            workers.append({"worker": worker, "status": "error", "exit_code": failure.returncode})
            continue
        decoded = json.loads(result.stdout)
        records.extend(decoded.pop("records"))
        workers.append({"worker": worker, "status": "ok", **decoded})

    return {
        "environment": {
            "os": platform.system(), "kernel": platform.release(), "arch": platform.machine(),
            "python": platform.python_version(), "zlib_runtime": zlib.ZLIB_RUNTIME_VERSION,
            "python_sha256": sha256(Path(sys.executable).read_bytes()).hexdigest(),
            "clock": metadata.implementation, "monotonic": metadata.monotonic,
            "reported_resolution_ns": metadata.resolution * 1e9,
            "probes": {"peak_memory": "unsupported", "energy": "unsupported"},
        },
        "design": {
            "seed": seed, "input_bytes": len(data), "input_sha256": sha256(data).hexdigest(),
            "levels": LEVELS, "workers": WORKERS, "warmup_blocks": WARMUP_BLOCKS,
            "measurement_blocks": MEASURED_BLOCKS, "invocations": INVOCATIONS,
            "worker_timeout_seconds": WORKER_TIMEOUT_SECONDS,
        },
        "plan": [asdict(step) for step in plan], "workers": workers, "records": records,
        "summary": summarize(plan, records, len(data)),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--run-id", default="blocked-gzip-v1-local", help="opaque experiment run ID")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--worker", type=int, choices=range(1, WORKERS + 1), help=argparse.SUPPRESS)
    mode.add_argument("--simulate", type=int, metavar="REPETITIONS", help="run the seeded coverage experiment")
    args = parser.parse_args()
    if not 1 <= len(args.run_id) <= 128:
        parser.error("run ID must contain 1–128 characters")
    try:
        args.run_id.encode("utf-8")
    except UnicodeEncodeError:
        parser.error("run ID must be valid Unicode")

    if args.simulate is not None:
        if args.simulate <= 0:
            parser.error("simulation repetitions must be positive")
        result = simulate(args.seed, args.simulate)
    elif args.worker is not None:
        result = run_child(args.seed, args.worker, args.run_id)
    else:
        result = pilot(args.seed, args.run_id)
    print(json.dumps(result))
    return 1 if result.get("summary", {}).get("status") == "incomplete" else 0


if __name__ == "__main__":
    sys.exit(main())
