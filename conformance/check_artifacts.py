"""Check whole-artifact completion across candidate and reference decoders."""
import argparse
import ctypes as c
import json
from pathlib import Path
import sys
import zlib

from worker.artifact import DecodeResult, MAX_DECODED_BYTES, check_decode_limits, gzip_members
from worker.zstandard import Zstandard, bind, digest


VECTORS = Path(__file__).with_name("vectors.json")
LIBDEFLATE_INSUFFICIENT_SPACE = 3


class Libdeflate:
    def __init__(self, path):
        path = Path(path).resolve(strict=True)
        lib = c.CDLL(str(path))
        self.identity = {"implementation": "libdeflate", "library_sha256": digest(path)}
        self.allocate = bind(lib, "libdeflate_alloc_decompressor", c.c_void_p)
        self.free = bind(lib, "libdeflate_free_decompressor", None, c.c_void_p)
        self.decode_member = bind(
            lib, "libdeflate_gzip_decompress_ex", c.c_int,
            c.c_void_p, c.c_void_p, c.c_size_t, c.c_void_p, c.c_size_t,
            c.POINTER(c.c_size_t), c.POINTER(c.c_size_t),
        )

    def decode(self, encoded, *, output_limit=MAX_DECODED_BYTES):
        denied = check_decode_limits(encoded, output_limit)
        if denied is not None:
            return denied

        decoder = self.allocate()
        if not decoder:
            raise MemoryError("libdeflate decoder allocation failed")
        try:
            source = c.create_string_buffer(encoded)
            destination = c.create_string_buffer(output_limit + 1)
            output = bytearray()
            offset = 0
            while offset < len(encoded):
                consumed, produced = c.c_size_t(), c.c_size_t()
                status = self.decode_member(
                    decoder, c.byref(source, offset), len(encoded) - offset,
                    destination, output_limit - len(output) + 1,
                    c.byref(consumed), c.byref(produced),
                )
                if status != 0:
                    # The API guarantees byte counts only on success.
                    outcome = "limit" if status == LIBDEFLATE_INSUFFICIENT_SPACE else "rejected"
                    return DecodeResult(outcome, completed_bytes=offset, decoded_bytes=len(output))
                if not 0 < consumed.value <= len(encoded) - offset:
                    raise RuntimeError("libdeflate reported invalid member consumption")
                output.extend(destination[:produced.value])
                if len(output) > output_limit:
                    return DecodeResult("limit", offset + consumed.value, offset, len(output))
                offset += consumed.value

            return DecodeResult.complete(output, offset)
        finally:
            self.free(decoder)


def zlib_identity():
    module = getattr(zlib, "__file__", None)
    return {"implementation": "CPython zlib", "version": zlib.ZLIB_RUNTIME_VERSION,
            "binding_sha256": digest(Path(module or sys.executable))}


def check_vectors(vectors, libdeflate, zstandard):
    adapters = {
        "gzip": (("candidate", gzip_members, zlib_identity()),
                 ("reference", libdeflate.decode, libdeflate.identity)),
        "zstd": (("candidate", zstandard.decode_stream, zstandard.identity),
                 ("reference", zstandard.decode_frames, zstandard.identity)),
    }
    for vector in vectors:
        encoded = bytes.fromhex(vector["input_hex"])
        expected = vector["expected"]
        for role, decoder, identity in adapters[vector["format"]]:
            result = decoder(encoded)
            label = f"{vector['id']} ({role})"
            if expected == "rejected":
                if result.status != "rejected" or result.output is not None:
                    raise AssertionError(f"{label}: expected rejected artifact, got {result.status}")
            elif not result.matches(encoded, bytes.fromhex(expected["output_hex"])):
                raise AssertionError(f"{label}: expected complete artifact and exact output")

            yield {
                "vector": vector["id"], "adapter": role, "build": identity,
                "status": result.status, "artifact_bytes": len(encoded),
                "consumed_bytes": result.consumed_bytes, "completed_bytes": result.completed_bytes,
                "decoded_bytes": result.decoded_bytes,
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--libdeflate", type=Path, required=True, help="libdeflate shared library")
    parser.add_argument("--zstd-lib", type=Path, required=True, help="libzstd shared library")
    args = parser.parse_args()
    vectors = json.loads(VECTORS.read_text(encoding="utf-8"))["codecs"]
    for outcome in check_vectors(vectors, Libdeflate(args.libdeflate), Zstandard(args.zstd_lib)):
        print(json.dumps(outcome))


if __name__ == "__main__":
    main()
