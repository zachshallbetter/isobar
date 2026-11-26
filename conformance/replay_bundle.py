"""Verify the checked-in codec benchmark's saved plan and invocation files."""
from collections import defaultdict
from hashlib import sha256
from io import StringIO
import re

from worker.artifact import MAX_ARTIFACT_BYTES, MAX_DECODED_BYTES
from .verify_evidence import MAX_COUNTER, strict_json


MAX_METADATA_BYTES = 8 * 1024 * 1024
CHECKSUM_ENTRY = re.compile(r"([0-9a-f]{64})  (?:\./)?([A-Za-z0-9._/-]+)")
ARTIFACT_PATH = re.compile(r"artifacts/[A-Za-z0-9_.-]+")


def counter(value, minimum=1, maximum=MAX_COUNTER):
    return type(value) is int and minimum <= value <= maximum


class ReplayBundle:
    def __init__(self, observations_path):
        if observations_path.name != "observations.jsonl":
            raise ValueError("Bundle replay requires observations.jsonl")
        self.root = observations_path.parent.resolve(strict=True)
        self.checksums = {}
        for line in self.read("checksums.sha256").decode("utf-8").splitlines():
            entry = CHECKSUM_ENTRY.fullmatch(line)
            if not entry:
                raise ValueError("Invalid bundle checksum entry")
            digest, name = entry.groups()
            if any(part in ("", ".", "..") for part in name.split("/")) or name in self.checksums:
                raise ValueError("Invalid or duplicate bundle checksum path")
            self.checksums[name] = digest

    def read(self, name, limit=MAX_METADATA_BYTES):
        path = self.root / name
        if path.resolve(strict=True) != path or not path.is_file():
            raise ValueError(f"Bundle file must be a regular file inside the bundle: {name}")
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError(f"Bundle file exceeds size limit: {name}")
        return data

    def checked(self, name, limit=MAX_METADATA_BYTES):
        expected = self.checksums.get(name)
        if expected is None:
            raise ValueError(f"Missing bundle digest: {name}")
        data = self.read(name, limit)
        if sha256(data).hexdigest() != expected:
            raise ValueError(f"Bundle digest mismatch: {name}")
        return data

    def observations(self):
        return [strict_json(line, evidence=True) for line in
                self.lines("observations.jsonl")]

    def lines(self, name):
        return StringIO(self.checked(name).decode("utf-8"), newline="\n")

    def verify_plan(self, manifest, observations):
        if not isinstance(manifest, dict) or not isinstance(manifest.get("plan"), list):
            raise ValueError("Invalid bundle plan")
        planned = {}
        slots = set()
        for step in manifest["plan"]:
            if not isinstance(step, dict):
                raise ValueError("Invalid planned step")
            worker, block, seq, phase, config = (
                step.get(field) for field in ("worker", "block", "sequence", "phase", "config")
            )
            if (not all(counter(n) for n in (worker, block, seq))
                    or phase not in ("warmup", "measurement")
                    or not isinstance(config, str) or not config):
                raise ValueError("Invalid planned step")
            key = f"w{worker}-s{seq}"
            slot = (worker, block, config)
            if key in planned or slot in slots:
                raise ValueError("Duplicate planned step")
            planned[key] = f"worker-{worker}", f"block-{block}", seq, phase, config
            slots.add(slot)

        if not planned or planned.keys() != observations.keys():
            raise ValueError("Planned coverage mismatch")
        if len({row["input_id"] for row in observations.values()}) != 1:
            raise ValueError("Bundle plan requires one input")
        for key, row in observations.items():
            fields = ("worker_id", "block_id", "sequence", "phase", "config_id")
            if (tuple(row[field] for field in fields) != planned[key]
                    or row["run_id"] != manifest.get("run_id") or row["operation"] != "encode"):
                raise ValueError(f"Planned step mismatch: {key}")

    def verify_invocations(self, observations, decode_counts):
        manifest = strict_json(self.checked("manifest.json").decode("utf-8"))
        self.verify_plan(manifest, observations)
        original = self.checked("input.bin", MAX_DECODED_BYTES)
        if (not counter(manifest.get("fixture_bytes"), 0, MAX_DECODED_BYTES)
                or len(original) != manifest["fixture_bytes"]
                or sha256(original).hexdigest() != manifest.get("fixture_sha256")):
            raise ValueError("Bundle input disagrees with manifest")

        batches = defaultdict(dict)
        paths = set()
        for line in self.lines("invocations.jsonl"):
            call = strict_json(line)
            if not isinstance(call, dict):
                raise ValueError("Invalid bundle invocation")
            key, number, path = (call.get(field) for field in ("observation_id", "invocation", "artifact"))
            if (not isinstance(key, str) or key not in observations or not counter(number)
                    or not isinstance(path, str) or not ARTIFACT_PATH.fullmatch(path)
                    or path in paths or number in batches[key]):
                raise ValueError("Invalid or duplicate bundle invocation")
            batches[key][number] = call
            paths.add(path)

        supplied = {}
        for key, row in observations.items():
            count = manifest.get(f"{row['phase']}_invocations_per_batch")
            batch = batches.get(key, {})
            if (not counter(count) or row.get("invocation_count") != count or len(batch) != count
                    or min(batch) != 1 or max(batch) != count):
                raise ValueError(f"Planned invocation coverage mismatch: {key}")
            sizes, elapsed = [], 0
            for call in batch.values():
                work, ns, size = (call.get(field) for field in ("work_bytes", "elapsed_ns", "artifact_bytes"))
                if (not counter(work, 0) or work != len(original) or not counter(ns)
                        or not counter(size, 1, MAX_ARTIFACT_BYTES)):
                    raise ValueError(f"Invalid invocation counters: {key}")
                artifact = self.checked(call["artifact"], MAX_ARTIFACT_BYTES)
                if len(artifact) != size:
                    raise ValueError(f"Artifact size disagrees with invocation: {key}")
                sizes.append(decode_counts(original, artifact))
                elapsed += ns
            if row.get("work_bytes") != len(original) * count or row.get("elapsed_ns") != elapsed:
                raise ValueError(f"Invocation totals disagree with observation: {key}")
            if row["phase"] == "measurement":
                supplied[key] = sizes
        return supplied
