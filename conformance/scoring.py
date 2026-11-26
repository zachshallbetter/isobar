"""Experimental IBS-0004 replay from a qualified local encode run."""
import argparse
from collections import defaultdict
from fractions import Fraction
from hashlib import sha256
import json
import math
from pathlib import Path
import sys

from worker.artifact import MAX_ARTIFACT_BYTES, MAX_DECODED_BYTES, gzip_members
from .verify_evidence import strict_json, verify_records


VERSIONS = {
    "ratio": "total-byte-ratio-v1",
    "throughput": "serial-bytes-per-second-v1",
    "composite": "weighted-log-ratio-v1",
    "ordinal": "enabled-lexicographic-competition-v1",
}
WEIGHT_TOTAL = 10_000
METRICS = ("ratio", "throughput")


def positive(value):
    if type(value) in (int, Fraction):
        return value > 0
    return type(value) is float and math.isfinite(value) and value > 0


def log_value(value):
    if isinstance(value, Fraction):
        return math.log(value.numerator) - math.log(value.denominator)
    return math.log(value)


def composite(metrics):
    weights = [metric["weight"] for metric in metrics]
    if (any(not math.isfinite(weight) or weight < 0 for weight in weights)
            or not math.isclose(math.fsum(weights), 1, rel_tol=0, abs_tol=1e-12)):
        return "invalid_weights"
    terms = []
    for metric in metrics:
        weight = metric["weight"]
        if weight == 0:
            continue
        x, reference = metric.get("candidate"), metric.get("reference")
        if not positive(x) or not positive(reference):
            return "unavailable"
        log_ratio = log_value(x) - log_value(reference)
        direction = {"max": 1, "min": -1}[metric["direction"]]
        terms.append(direction * weight * log_ratio)
    total = math.fsum(terms)
    try:
        score = math.exp(math.log(100) + total) if total else 100.0
    except OverflowError:
        return "out_of_range"
    return score if score > 0 else "out_of_range"


def artifact_counts(source_path, artifact_path):
    with Path(source_path).open("rb") as stream:
        source = stream.read(MAX_DECODED_BYTES + 1)
    with Path(artifact_path).open("rb") as stream:
        encoded = stream.read(MAX_ARTIFACT_BYTES + 1)
    if len(source) > MAX_DECODED_BYTES or len(encoded) > MAX_ARTIFACT_BYTES:
        raise ValueError("Artifact or original exceeds the local format limits")
    if not gzip_members(encoded).matches(encoded, source):
        raise ValueError("Gzip artifact did not reconstruct the supplied input")
    return len(source), len(encoded), sha256(source).hexdigest()


def slot(row):
    return row["worker_id"], row["block_id"], row["input_id"]


def rank_enabled(points, enabled):
    keys = {config: tuple(-values[metric] for metric in enabled)
            for config, values in points.items()}
    ranking = {}
    previous = None
    for place, config in enumerate(sorted(keys, key=lambda config: (keys[config], config)), 1):
        key = keys[config]
        if key != previous:
            rank = place
        ranking[config] = rank
        previous = key
    return ranking


def exact(value):
    return f"{value.numerator}/{value.denominator}"


def read_measurements(path):
    with path.open(encoding="utf-8", newline="\n") as source:
        records = [strict_json(line, evidence=True) for line in source]
    verdicts = verify_records(records)
    if len({verdict["run_id"] for verdict in verdicts}) != 1:
        raise ValueError("Scoring requires one run")
    qualified = {verdict["observation_id"] for verdict in verdicts if verdict["qualified"]}
    measurements = {row["observation_id"]: row for row in records
                    if "config_id" in row and row["phase"] == "measurement"}
    if not measurements or any(row["operation"] != "encode" for row in measurements.values()):
        raise ValueError("Scoring requires encode measurement observations")
    return verdicts[0]["run_id"], measurements, qualified


def grouped_artifacts(arguments, measurements, qualified):
    supplied = defaultdict(list)
    for observation_id, source, encoded in arguments:
        if observation_id not in qualified:
            raise ValueError("Artifact requires a qualified measurement observation")
        row = measurements[observation_id]
        entries = supplied[observation_id]
        if len(entries) >= row["invocation_count"]:
            raise ValueError("Too many artifact pairs for observation")
        entries.append(artifact_counts(source, encoded))
    for observation_id, entries in supplied.items():
        row = measurements[observation_id]
        measured = sum(entry[0] for entry in entries)
        complete = len(entries) == row["invocation_count"]
        if measured > row["work_bytes"] or (complete and measured != row["work_bytes"]):
            raise ValueError("Artifact source byte total disagrees with observation")
    return supplied


def score(path, baseline, ratio_bps, artifact_arguments):
    if not 0 <= ratio_bps <= WEIGHT_TOTAL:
        raise ValueError("Ratio weight must be between 0 and 10000 basis points")
    run_id, measurements, qualified = read_measurements(path)
    groups = defaultdict(dict)
    for obs in measurements.values():
        cohort = groups[obs["config_id"]]
        if slot(obs) in cohort:
            raise ValueError("Multiple observations for one config and slot")
        cohort[slot(obs)] = obs
    if baseline not in groups:
        raise ValueError("Baseline must name a measured configuration")

    supplied = grouped_artifacts(artifact_arguments, measurements, qualified)
    configurations = sorted(groups)
    scores = {config: {"observations": sorted(row["observation_id"] for row in groups[config].values())}
              for config in configurations}
    weights = dict(zip(METRICS, (ratio_bps, WEIGHT_TOTAL - ratio_bps), strict=True))
    enabled = [metric for metric, weight in weights.items() if weight]
    result = {"run_id": run_id, "baseline": baseline, "versions": VERSIONS,
              "weights_bps": weights, "candidate_set": configurations, "scores": scores,
              "publication": "withheld_reviewed_uncertainty_required"}
    if qualified != measurements.keys():
        return dict(result, status="unavailable", reason="unqualified_evidence")

    # Qualification alone does not prove that the configurations did the same work.
    def workload(cohort):
        return {key: (obs["work_bytes"], obs["invocation_count"]) for key, obs in cohort.items()}

    baseline_slots = workload(groups[baseline])
    if any(workload(cohort) != baseline_slots for cohort in groups.values()):
        return dict(result, status="unavailable", reason="incomparable_work")

    # When artifact sources are supplied, confirm content agreement within matched slots.
    source_digests = {}
    values = {}
    for config in configurations:
        rows = groups[config].values()
        work = sum(row["work_bytes"] for row in rows)
        elapsed = sum(row["elapsed_ns"] for row in rows)
        compressed = 0
        missing = False
        for row in rows:
            entries = supplied.get(row["observation_id"], [])
            if len(entries) != row["invocation_count"]:
                missing = True
                continue
            digests = sorted(entry[2] for entry in entries)
            if source_digests.setdefault(slot(row), digests) != digests:
                raise ValueError("Supplied source contents disagree within the paired slot")
            compressed += sum(entry[1] for entry in entries)

        ratio = Fraction(work, compressed) if not missing and work and compressed else None
        throughput = Fraction(work * 1_000_000_000, elapsed)
        values[config] = {"ratio": ratio, "throughput": throughput}
        scores[config].update(
            work_bytes=str(work), elapsed_ns=str(elapsed), artifact_bytes=None if missing else str(compressed),
            metrics={
                "ratio": {"value": exact(ratio)} if ratio is not None else {
                    "reason": "missing_artifact_bytes" if missing else "undefined_ratio",
                },
                "throughput": {"value": exact(throughput)},
            },
        )

    for config, metrics in values.items():
        terms = [{"direction": "max", "weight": weights[metric] / WEIGHT_TOTAL,
                  "candidate": metrics[metric], "reference": values[baseline][metric]} for metric in enabled]
        score_value = composite(terms)
        reason = score_value
        if reason == "unavailable":
            reason = ("missing_enabled_metric" if any(term["candidate"] is None or term["reference"] is None
                                                       for term in terms) else "nonpositive_enabled_metric")
        scores[config]["composite"] = {"reason": reason} if isinstance(score_value, str) else {"value": score_value}

    if any(points[metric] is None for points in values.values() for metric in enabled):
        return dict(result, status="unavailable", reason="missing_enabled_metric")

    for config, rank in rank_enabled(values, enabled).items():
        scores[config]["rank"] = rank
    return dict(result, status="complete")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="verified IBS-0003 JSONL input")
    parser.add_argument("--baseline", required=True, help="baseline config ID")
    parser.add_argument("--ratio-weight-bps", type=int, default=5000)
    parser.add_argument("--artifact", action="append", nargs=3, default=[],
                        metavar=("OBS_ID", "SOURCE", "GZIP"))
    args = parser.parse_args()
    try:
        result = score(args.path, args.baseline, args.ratio_weight_bps, args.artifact)
    except (OSError, ValueError, RecursionError) as error:
        print(f"Invalid scoring input: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
