"""Executable whole-artifact checks against local candidate and reference paths."""
import gzip
import json
import os
import unittest

from conformance.check_artifacts import Libdeflate, VECTORS, Zstandard, check_vectors
from worker.artifact import MAX_ARTIFACT_BYTES, gzip_members
from worker.measure import measure


ZSTD_MAGIC = bytes.fromhex("28b52ffd")


def skippable(payload):
    return bytes.fromhex("502a4d18") + len(payload).to_bytes(4, "little") + payload


def zstd_rle(size, window_descriptor):
    # Non-single-segment header, then one last RLE block.
    return (ZSTD_MAGIC + bytes((0, window_descriptor))
            + ((size << 3) | 3).to_bytes(3, "little") + b"x")


class ArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.libdeflate = Libdeflate(os.environ["ISOBAR_LIBDEFLATE"])
        cls.zstd = Zstandard(os.environ["ISOBAR_ZSTD_LIB"])
        cls.vectors = json.loads(VECTORS.read_text(encoding="utf-8"))["codecs"]
        cls.artifacts = {case["id"]: bytes.fromhex(case["input_hex"]) for case in cls.vectors}

    def decoders(self, format):
        if format == "gzip":
            return gzip_members, self.libdeflate.decode
        return self.zstd.decode_stream, self.zstd.decode_frames

    def assert_complete(self, format, artifact, expected, **limits):
        for decoder in self.decoders(format):
            with self.subTest(format=format, decoder=decoder.__qualname__):
                result = decoder(artifact, **limits)
                self.assertTrue(result.matches(artifact, expected), result)
                self.assertFalse(result.matches(artifact, expected + b"wrong"))

    def assert_rejected(self, format, artifact, *, completed, exact_prefix):
        for decoder in self.decoders(format):
            with self.subTest(format=format, decoder=decoder.__qualname__):
                result = decoder(artifact)
                self.assertEqual(result.status, "rejected")
                self.assertEqual(result.completed_bytes, completed)
                self.assertGreaterEqual(result.decoded_bytes, len(exact_prefix))
                # A valid decoded prefix is never returned as the artifact's output.
                self.assertIsNone(result.output)
                self.assertFalse(result.matches(artifact, exact_prefix))
                if result.consumed_bytes is not None:
                    self.assertLessEqual(completed, result.consumed_bytes)
                    self.assertLessEqual(result.consumed_bytes, len(artifact))

    def test_all_original_vectors_run_through_both_roles(self):
        records = list(check_vectors(self.vectors, self.libdeflate, self.zstd))
        self.assertEqual(len(records), 2 * len(self.vectors))
        for index, vector in enumerate(self.vectors):
            candidate, reference = records[index * 2:index * 2 + 2]
            self.assertEqual([candidate["adapter"], reference["adapter"]], ["candidate", "reference"])
            for record in (candidate, reference):
                with self.subTest(vector=vector["id"], role=record["adapter"]):
                    self.assertEqual(record["vector"], vector["id"])
                    build = record["build"]
                    self.assertRegex(build.get("library_sha256", build.get("binding_sha256")),
                                     r"^[0-9a-f]{64}$")
                    if record["status"] == "complete":
                        self.assertEqual(record["consumed_bytes"], record["artifact_bytes"])
                        self.assertEqual(record["decoded_bytes"], len(vector["expected"]["output_hex"]) // 2)

    def test_gzip_member_chain_includes_empty_member(self):
        a = self.artifacts["gzip-single"]
        b = gzip.compress(b"B", mtime=0)
        self.assert_complete("gzip", a + self.artifacts["gzip-empty"] + b, b"AB")

    def test_gzip_truncated_second_member_preserves_completed_prefix(self):
        artifact = self.artifacts["gzip-truncated-second"]
        completed = len(self.artifacts["gzip-single"])
        self.assert_rejected("gzip", artifact, completed=completed, exact_prefix=b"A")
        candidate, reference = (decoder(artifact) for decoder in self.decoders("gzip"))
        self.assertEqual(candidate.consumed_bytes, len(artifact))
        self.assertIsNone(reference.consumed_bytes)
        self.assertEqual(reference.decoded_bytes, 1)

    def test_zstd_skip_frames_at_both_ends_and_skip_only(self):
        skip = skippable(b"metadata")
        a = self.artifacts["zstd-single"]
        self.assert_complete("zstd", skip + a + skip, b"A")
        self.assert_complete("zstd", skip + skippable(b""), b"", output_limit=0)

    def test_zstd_rejects_truncated_data_frame_and_skip_tail(self):
        a = self.artifacts["zstd-single"]
        truncated = self.artifacts["zstd-truncated-second"]
        for artifact in (truncated, a + skippable(b"metadata")[:-1], a + skippable(b"metadata")[:7]):
            self.assert_rejected("zstd", artifact, completed=len(a), exact_prefix=b"A")
        self.assertEqual(self.zstd.decode_stream(truncated).consumed_bytes, len(truncated))
        self.assertIsNone(self.zstd.decode_frames(truncated).consumed_bytes)

    def test_empty_body_and_non_format_tail(self):
        for format in ("gzip", "zstd"):
            with self.subTest(format=format):
                self.assert_complete(format, self.artifacts[f"{format}-empty"], b"", output_limit=0)
                self.assert_rejected(format, b"", completed=0, exact_prefix=b"")
                a = self.artifacts[f"{format}-single"]
                self.assert_rejected(format, a + b"\0", completed=len(a), exact_prefix=b"A")

    def test_zstd_stream_flushes_multiple_output_buffers(self):
        size = 65537
        artifact = zstd_rle(size, window_descriptor=0x38)
        self.assert_complete("zstd", artifact, b"x" * size, output_limit=size)

    def test_local_output_and_artifact_limits(self):
        for format, artifact in (("gzip", gzip.compress(b"x" * 30, mtime=0)),
                                 ("zstd", zstd_rle(30, window_descriptor=0x38))):
            self.assert_complete(format, artifact, b"x" * 30, output_limit=30)
            for decoder in self.decoders(format):
                with self.subTest(format=format, decoder=decoder.__qualname__):
                    expanded = decoder(artifact, output_limit=10)
                    self.assertEqual(expanded.status, "limit")
                    self.assertIsNone(expanded.output)
                    oversized = decoder(bytes(MAX_ARTIFACT_BYTES + 1))
                    self.assertEqual(oversized.status, "limit")
                    self.assertEqual(oversized.consumed_bytes, 0)
                    self.assertIsNone(oversized.output)
                    with self.assertRaisesRegex(ValueError, "Output limit"):
                        decoder(artifact, output_limit=-1)

    def test_zstd_streaming_window_budget_is_local(self):
        artifact = zstd_rle(30, window_descriptor=0x58)  # Declares a 2 MiB window.
        result = self.zstd.decode_stream(artifact)
        self.assertEqual(result.status, "limit")
        self.assertIsNone(result.output)
        self.assertTrue(self.zstd.decode_frames(artifact).matches(artifact, b"x" * 30))

    def test_qualification_uses_strict_gzip_completion(self):
        member = self.artifacts["gzip-single"]
        complete = measure(b"A", encode=lambda _: member, clock=iter((0, 5)).__next__)
        self.assertTrue(complete.qualified)
        for artifact in (b"", member + b"\0", self.artifacts["gzip-truncated-second"]):
            with self.subTest(artifact=artifact):
                result = measure(b"A", encode=lambda _: artifact, clock=iter((0, 5)).__next__)
                self.assertEqual(result.validation, "decode_error")
                self.assertFalse(result.qualified)


if __name__ == "__main__":
    unittest.main()
