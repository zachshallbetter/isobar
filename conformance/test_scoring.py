"""Cross-check the experimental IBS-0004 scorers against qualified evidence."""
from fractions import Fraction
import gzip
from hashlib import sha256
import io
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from conformance.scoring import VERSIONS, composite


ROOT = Path(__file__).resolve().parent.parent
READERS = (
    [sys.executable, "-B", "-m", "conformance.scoring"],
    ["ruby", "conformance/score_evidence.rb"],
)


def invoke(argv, argv_input=None):
    return subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, input=argv_input, timeout=15)


class ScoringTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.evidence = self.root / "test.jsonl"
        self.records, self.pairs = [], []

    def add_trial(self, config, block, contents, elapsed_ns, *, label="", phase="measurement"):
        observation_id = f"{config}-{block}"
        source = self.root / f"{observation_id}.raw"
        artifact = self.root / f"{observation_id}.gz"
        source.write_bytes(contents)
        buffer = io.BytesIO()
        with gzip.GzipFile(filename=label, mode="wb", fileobj=buffer, mtime=0) as stream:
            stream.write(contents)
        artifact.write_bytes(buffer.getvalue())
        self.records.extend([
            {
                "schema_version": "0.1-experimental", "run_id": "scoring-fixture",
                "observation_id": observation_id, "worker_id": "w1", "block_id": str(block),
                "sequence": block, "input_id": f"i{block}", "config_id": config,
                "operation": "encode", "phase": phase, "status": "ok", "elapsed_ns": elapsed_ns,
                "invocation_count": 1, "work_bytes": len(contents), "validation_ref": f"v-{observation_id}",
            },
            {
                "schema_version": "0.1-experimental", "run_id": "scoring-fixture",
                "validation_id": f"v-{observation_id}", "observation_id": observation_id,
                "cadence": "every_invocation", "checked_invocations": 1,
                "status": "verified", "method": "exact_bytes",
            },
        ])
        self.pairs.extend(["--artifact", observation_id, str(source), str(artifact)])
        return artifact

    def compare(self, *, pairs=(), baseline="reference", ratio_bps=5000, invalid=False, path=None):
        if path is None:
            self.evidence.write_text("".join(json.dumps(row) + "\n" for row in self.records))
            path = self.evidence
        results = []
        for reader in READERS:
            result = invoke([*reader, str(path), "--baseline", baseline,
                             "--ratio-weight-bps", str(ratio_bps), *pairs])
            if invalid:
                self.assertEqual(result.returncode, 1, (reader, result.stdout, result.stderr))
                self.assertEqual(result.stdout, "")
                self.assertIn("Invalid scoring input:", result.stderr)
                if isinstance(invalid, str):
                    self.assertIn(invalid, result.stderr)
                continue
            self.assertEqual(result.returncode, 0, (reader, result.stderr))
            results.append(json.loads(result.stdout))

        if invalid:
            return
        primary, reference = results
        for config, score in primary["scores"].items():
            score_result = score.get("composite", {})
            if "value" in score_result:
                peer = reference["scores"][config]["composite"]["value"]
                self.assertTrue(math.isclose(score_result["value"], peer, rel_tol=1e-12))
                reference["scores"][config]["composite"]["value"] = score_result["value"]
        self.assertEqual(primary, reference)
        return primary

    def test_total_byte_ratio_serial_throughput_and_composite(self):
        compressed = 0
        for block, (content, time) in enumerate(((b"A" * 10, 100), (b"B" * 1000, 900)), 1):
            compressed += self.add_trial("reference", block, content, time).stat().st_size
            self.add_trial("candidate", block, content, time * 4)
        self.add_trial("reference", 3, b"warm-up", 100_000, phase="warmup")
        result = self.compare(pairs=self.pairs[:-4])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["versions"], VERSIONS)
        self.assertEqual(result["publication"], "withheld_reviewed_uncertainty_required")
        self.assertEqual(result["candidate_set"], ["candidate", "reference"])
        reference, candidate = (result["scores"][config] for config in ("reference", "candidate"))
        self.assertEqual(reference["observations"], ["reference-1", "reference-2"])
        self.assertEqual((reference["work_bytes"], reference["artifact_bytes"], reference["elapsed_ns"]),
                         ("1010", str(compressed), "1000"))
        self.assertEqual(Fraction(reference["metrics"]["ratio"]["value"]), Fraction(1010, compressed))
        self.assertEqual(candidate["metrics"]["throughput"]["value"], "252500000/1")
        self.assertEqual(reference["composite"]["value"], 100)
        self.assertAlmostEqual(candidate["composite"]["value"], 50)
        self.assertEqual((reference["rank"], candidate["rank"]), (1, 2))

    def test_ratio_only_ties_ignore_disabled_throughput_and_input_order(self):
        for config, time, label in (("reference", 100, ""), ("equal", 200, ""), ("bigger", 50, "header-name")):
            self.add_trial(config, 1, b"A" * 300, time, label=label)
        result = self.compare(pairs=self.pairs, ratio_bps=10000)
        ranks = {entry: score["rank"] for entry, score in result["scores"].items()}
        self.assertEqual(ranks, {"reference": 1, "equal": 1, "bigger": 3})
        self.assertEqual(result["scores"]["equal"]["composite"]["value"], 100)
        self.records.reverse()
        self.assertEqual(self.compare(pairs=self.pairs, ratio_bps=10000), result)

    def test_archived_qualified_batches_replay_without_invented_sizes(self):
        path = ROOT / "research/experiments/blocked-macos.jsonl"
        result = self.compare(path=path, ratio_bps=0)
        self.assertEqual(result["status"], "complete")
        reference = result["scores"]["reference"]
        self.assertEqual(reference["work_bytes"], "18874368")
        self.assertEqual(reference["elapsed_ns"], "37264547")
        self.assertEqual(reference["metrics"]["throughput"]["value"],
                         str(Fraction(18874368 * 10**9, 37264547)))
        self.assertEqual(reference["metrics"]["ratio"], {"reason": "missing_artifact_bytes"})
        self.assertEqual(result["scores"]["candidate"]["rank"], 2)
        unavailable = self.compare(path=path)
        self.assertEqual(unavailable["reason"], "missing_enabled_metric")
        self.assertNotIn("rank", unavailable["scores"]["reference"])

    def test_failure_and_unmatched_work_do_not_produce_scores(self):
        self.add_trial("reference", 1, b"abc", 100)
        self.add_trial("candidate", 1, b"abc", 150)
        self.records[-1]["status"] = "mismatch"
        result = self.compare(ratio_bps=0)
        self.assertEqual(result["reason"], "unqualified_evidence")
        self.assertNotIn("metrics", result["scores"]["reference"])
        self.records[-1]["status"] = "verified"
        self.records[-2]["work_bytes"] = 4
        result = self.compare(ratio_bps=0)
        self.assertEqual(result["reason"], "incomparable_work")

    def test_rejects_bad_artifact_equal_size_wrong_input_and_incomplete_batch(self):
        self.add_trial("reference", 1, b"aaa", 100)
        candidate = self.add_trial("candidate", 1, b"bbb", 100)
        self.compare(pairs=self.pairs, invalid=True)
        candidate.write_bytes(gzip.compress(b"bbb", mtime=0) + gzip.compress(b"", mtime=0)[:-1])
        self.compare(pairs=self.pairs[4:], invalid=True)
        self.records[-2]["invocation_count"] = self.records[0]["invocation_count"] = 2
        self.records[-1]["checked_invocations"] = self.records[1]["checked_invocations"] = 2
        self.records[-2]["work_bytes"] = self.records[0]["work_bytes"] = 6
        result = self.compare(pairs=self.pairs[:4])
        self.assertEqual(result["reason"], "missing_enabled_metric")
        self.assertEqual(result["scores"]["reference"]["metrics"]["ratio"], {"reason": "missing_artifact_bytes"})

    def test_batch_ratio_counts_each_complete_artifact_including_members(self):
        for config, elapsed in (("reference", 100), ("candidate", 150)):
            first = self.add_trial(config, 1, b"abc", elapsed)
            self.records[-2].update(work_bytes=7, invocation_count=2)
            self.records[-1]["checked_invocations"] = 2
            original = self.root / f"{config}-second.raw"
            artifact = self.root / f"{config}-second.gz"
            original.write_bytes(b"deUV")
            artifact.write_bytes(gzip.compress(b"de", mtime=0) + gzip.compress(b"UV", mtime=0))
            self.pairs.extend(["--artifact", f"{config}-1", str(original), str(artifact)])
            expected_size = first.stat().st_size + artifact.stat().st_size
        result = self.compare(pairs=self.pairs)
        for score in result["scores"].values():
            self.assertEqual(score["artifact_bytes"], str(expected_size))
            self.assertEqual(Fraction(score["metrics"]["ratio"]["value"]), Fraction(7, expected_size))

    def test_mixed_codec_replay_counts_all_zstandard_frames_and_skippable_bytes(self):
        gzip_artifact = self.add_trial("reference", 1, b"AB", 200)
        # The extension deliberately stays .gz. Replay dispatches by frame magic.
        zstd_artifact = self.add_trial("candidate", 1, b"AB", 100)
        skip = bytes.fromhex("5f2a4d180300000078797a")
        frames = bytes.fromhex("28b52ffd20010900004128b52ffd200109000042")
        valid = skip + frames + skip
        zstd_artifact.write_bytes(valid)
        result = self.compare(pairs=self.pairs)
        self.assertEqual(result["status"], "complete")
        candidate = result["scores"]["candidate"]
        self.assertEqual(candidate["artifact_bytes"], str(len(valid)))
        self.assertEqual(Fraction(candidate["metrics"]["ratio"]["value"]), Fraction(2, len(valid)))
        self.assertAlmostEqual(candidate["composite"]["value"],
                               100 * math.sqrt(2 * gzip_artifact.stat().st_size / len(valid)))

        for invalid in (valid[:-1], skip + frames[:-1], valid + b"junk",
                        valid + gzip_artifact.read_bytes(), frames[:-1] + b"C", frames + frames):
            with self.subTest(invalid=invalid.hex()):
                zstd_artifact.write_bytes(invalid)
                self.compare(pairs=self.pairs, invalid=True)

    def test_zstandard_skip_only_artifact_counts_bytes_for_empty_input(self):
        artifact = self.add_trial("reference", 1, b"", 100)
        skip = bytes.fromhex("502a4d1800000000")
        artifact.write_bytes(skip)
        result = self.compare(pairs=self.pairs, ratio_bps=0)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["scores"]["reference"]["artifact_bytes"], str(len(skip)))
        self.assertEqual(result["scores"]["reference"]["metrics"]["ratio"], {"reason": "undefined_ratio"})

    def write_bundle(self):
        plan, calls = [], []
        artifacts = self.root / "artifacts"
        artifacts.mkdir()
        original = b"one fixture for both codecs"
        (self.root / "input.bin").write_bytes(original)
        self.evidence = self.root / "observations.jsonl"
        for block in (1, 2):
            for config in ("reference", "candidate"):
                artifact = self.add_trial(config, block, original, 100)
                row, validation = self.records[-2:]
                seq = len(plan) + 1
                key = f"w1-s{seq}"
                plan.append(dict(worker=1, block=block, sequence=seq, phase="measurement", config=config))
                row.update(observation_id=key, worker_id="worker-1", block_id=f"block-{block}",
                           sequence=seq, input_id="fixture")
                validation["observation_id"] = key
                relative = f"artifacts/{key}-1.gz"
                target = self.root / relative
                target.write_bytes(artifact.read_bytes())
                calls.append(dict(observation_id=key, invocation=1, artifact=relative,
                                  work_bytes=len(original), elapsed_ns=100,
                                  artifact_bytes=target.stat().st_size))
        manifest = dict(run_id=self.records[0]["run_id"], plan=plan, fixture_bytes=len(original),
                        fixture_sha256=sha256(original).hexdigest(), measurement_invocations_per_batch=1,
                        warmup_invocations_per_batch=1)
        (self.root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        invocation_path = self.root / "invocations.jsonl"
        invocation_path.write_text("".join(json.dumps(row) + "\n" for row in calls), encoding="utf-8")
        self.evidence.write_text("".join(json.dumps(row) + "\n" for row in self.records), encoding="utf-8")
        return [self.evidence, invocation_path, self.root / "manifest.json", self.root / "input.bin",
                *sorted(artifacts.iterdir())]

    def write_bundle_checksums(self, files):
        (self.root / "checksums.sha256").write_text(
            "".join(f"{sha256(path.read_bytes()).hexdigest()}  {path.relative_to(self.root).as_posix()}\n"
                    for path in files),
            encoding="utf-8",
        )

    def test_bundle_checksum_line_endings_and_dot_prefix_agree_across_readers(self):
        files = self.write_bundle()
        self.write_bundle_checksums(files)
        checksums = self.root / "checksums.sha256"
        lines = checksums.read_bytes().split(b"\n")[:-1]
        options = dict(path=self.evidence, pairs=("--verify-bundle",))
        expected = self.compare(**options)
        self.assertEqual(expected["status"], "complete")

        for prefix in (b"", b"./"):
            entries = [digest + b"  " + prefix + path
                       for digest, path in (line.split(b"  ", 1) for line in lines)]
            for separator in (b"\n", b"\r\n"):
                with self.subTest(prefix=prefix, separator=separator):
                    checksums.write_bytes(separator.join(entries) + separator)
                    self.assertEqual(self.compare(**options), expected)

        for data in (b"\r".join(lines) + b"\r", b"\n".join(lines) + b"\r",
                     b"\n".join(lines)):
            with self.subTest(ending=data[-10:]):
                checksums.write_bytes(data)
                if data.endswith(b"\r"):
                    self.compare(**options, invalid="Invalid bundle checksum entry")
                else:
                    self.assertEqual(self.compare(**options), expected)

    def test_bundle_rejects_missing_matched_block_with_consistent_checksums(self):
        files = self.write_bundle()
        self.write_bundle_checksums(files)
        result = self.compare(path=self.evidence, pairs=("--verify-bundle",))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["scores"]["reference"]["work_bytes"], str(2 * files[3].stat().st_size))

        # The remaining observations still form a complete match between codecs.
        self.evidence.write_text("".join(json.dumps(row) + "\n" for row in self.records[:4]),
                                 encoding="utf-8")
        calls = files[1].read_text(encoding="utf-8").splitlines(keepends=True)
        files[1].write_text("".join(calls[:2]), encoding="utf-8")
        self.write_bundle_checksums(files)
        self.assertEqual(self.compare(path=self.evidence, ratio_bps=0)["status"], "complete")
        self.compare(path=self.evidence, pairs=("--verify-bundle",), ratio_bps=0,
                     invalid="Planned coverage mismatch")

    def test_bundle_rejects_same_size_substitute_that_still_decodes(self):
        files = self.write_bundle()
        self.write_bundle_checksums(files)
        self.assertEqual(self.compare(path=self.evidence, pairs=("--verify-bundle",))["status"], "complete")

        artifact = files[-1]
        original = files[3].read_bytes()
        captured = artifact.read_bytes()
        replacement = bytearray(captured)
        replacement[4] ^= 1  # mtime, independent of the encoded payload and trailer
        self.assertNotEqual(bytes(replacement), captured)
        self.assertEqual(len(replacement), len(captured))
        self.assertEqual(gzip.decompress(replacement), original)
        artifact.write_bytes(replacement)
        self.compare(path=self.evidence, pairs=("--verify-bundle",), invalid="Bundle digest mismatch: artifacts/")
        artifact.write_bytes(captured)
        self.write_bundle_checksums(files[:-1])
        self.compare(path=self.evidence, pairs=("--verify-bundle",), invalid="Missing bundle digest: artifacts/")

    def test_empty_original_is_undefined_ratio_and_zero_throughput(self):
        self.add_trial("reference", 1, b"", 100)
        result = self.compare(pairs=self.pairs)
        self.assertEqual(result["scores"]["reference"]["metrics"]["ratio"], {"reason": "undefined_ratio"})
        result = self.compare(pairs=self.pairs, ratio_bps=0)
        self.assertEqual(result["scores"]["reference"]["metrics"]["throughput"], {"value": "0/1"})
        self.assertEqual(result["scores"]["reference"]["composite"], {"reason": "nonpositive_enabled_metric"})

    def test_wide_aggregate_counters_are_exact(self):
        maximum = (1 << 53) - 1
        for block in (1, 2):
            self.add_trial("reference", block, b"x", maximum)
            self.records[-2]["work_bytes"] = maximum
        result = self.compare(ratio_bps=0)
        reference = result["scores"]["reference"]
        self.assertEqual(reference["work_bytes"], str(2 * maximum))
        self.assertEqual(reference["elapsed_ns"], str(2 * maximum))
        self.assertEqual(reference["metrics"]["throughput"]["value"], "1000000000/1")

    def test_independent_composite_vectors_and_unit_direction(self):
        metrics = json.loads((ROOT / "conformance/vectors.json").read_text())["metrics"]
        vectors = [row for row in metrics if row["operation"] == "composite"]
        program = """
require_relative "conformance/score_evidence"
vectors = JSON.parse($stdin.read)
puts JSON.generate(vectors.map { |row| weighted_score(row.fetch("metrics").map { |term| term.transform_keys(&:to_sym) }) })
"""
        reference = invoke(["ruby", "-e", program], json.dumps(vectors))
        self.assertEqual(reference.returncode, 0, reference.stderr)
        for vector, peer in zip(vectors, json.loads(reference.stdout), strict=True):
            with self.subTest(vector=vector["id"]):
                expected = vector["expected"]
                primary = composite(vector["metrics"])
                if isinstance(expected, str):
                    self.assertEqual(primary, expected)
                    self.assertEqual(peer, expected)
                else:
                    self.assertTrue(math.isclose(primary, expected, rel_tol=1e-12))
                    self.assertTrue(math.isclose(peer, expected, rel_tol=1e-12))


if __name__ == "__main__":
    unittest.main()
