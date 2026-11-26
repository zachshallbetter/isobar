"""Check whole-artifact completion across candidate and reference decoders."""
import argparse
import ctypes as c
from hashlib import sha256
import json
from pathlib import Path
import sys
import zlib

from worker.artifact import DecodeResult, MAX_DECODED_BYTES, check_decode_limits, gzip_members


VECTORS = Path(__file__).with_name("vectors.json")
ZSTD_D_WINDOW_LOG_MAX = 100  # ZSTD_d_windowLogMax in zstd.h
ZSTD_WINDOW_TOO_LARGE = 16  # ZSTD_error_frameParameter_windowTooLarge
ZSTD_DST_TOO_SMALL = 70  # ZSTD_error_dstSize_tooSmall
LIBDEFLATE_INSUFFICIENT_SPACE = 3
STREAM_CHUNK_BYTES = 16 * 1024


def bind(library, name, result_type, *argument_types):
    function = getattr(library, name)
    function.argtypes = argument_types
    function.restype = result_type
    return function


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


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


class ZstdBuffer(c.Structure):
    _fields_ = [("data", c.c_void_p), ("size", c.c_size_t), ("pos", c.c_size_t)]


class Zstandard:
    def __init__(self, path):
        path = Path(path).resolve(strict=True)
        lib = c.CDLL(str(path))
        version = bind(lib, "ZSTD_versionString", c.c_char_p)
        self.identity = {"implementation": "libzstd", "version": version().decode("ascii"),
                         "library_sha256": digest(path)}
        self.create = bind(lib, "ZSTD_createDStream", c.c_void_p)
        self.free = bind(lib, "ZSTD_freeDStream", c.c_size_t, c.c_void_p)
        self.set_parameter = bind(lib, "ZSTD_DCtx_setParameter", c.c_size_t,
                                  c.c_void_p, c.c_int, c.c_int)
        self.stream = bind(lib, "ZSTD_decompressStream", c.c_size_t, c.c_void_p,
                           c.POINTER(ZstdBuffer), c.POINTER(ZstdBuffer))
        self.frame_size = bind(lib, "ZSTD_findFrameCompressedSize", c.c_size_t,
                               c.c_void_p, c.c_size_t)
        self.decode_frame = bind(lib, "ZSTD_decompress", c.c_size_t,
                                 c.c_void_p, c.c_size_t, c.c_void_p, c.c_size_t)
        self.is_error = bind(lib, "ZSTD_isError", c.c_uint, c.c_size_t)
        self.error_code = bind(lib, "ZSTD_getErrorCode", c.c_int, c.c_size_t)

    def decode_stream(self, encoded, *, output_limit=MAX_DECODED_BYTES):
        denied = check_decode_limits(encoded, output_limit)
        if denied is not None:
            return denied

        decoder = self.create()
        if not decoder:
            raise MemoryError("Zstandard stream allocation failed")
        try:
            # Local conformance budget permits at most a 1 MiB streaming window.
            if self.is_error(self.set_parameter(decoder, ZSTD_D_WINDOW_LOG_MAX, 20)):
                raise RuntimeError("Could not set the Zstandard streaming window limit")
            source = c.create_string_buffer(encoded)
            destination = c.create_string_buffer(STREAM_CHUNK_BYTES)
            incoming = ZstdBuffer(c.addressof(source), len(encoded), 0)
            output = bytearray()
            completed = 0
            while True:
                previous = incoming.pos
                capacity = min(len(destination), output_limit - len(output) + 1)
                outgoing = ZstdBuffer(c.addressof(destination), capacity, 0)
                hint = self.stream(decoder, c.byref(outgoing), c.byref(incoming))
                if self.is_error(hint):
                    status = "limit" if self.error_code(hint) == ZSTD_WINDOW_TOO_LARGE else "rejected"
                    return DecodeResult(status, completed_bytes=completed, decoded_bytes=len(output))

                output.extend(destination[:outgoing.pos])
                if len(output) > output_limit:
                    return DecodeResult("limit", incoming.pos, completed, len(output))
                if hint == 0:
                    completed = incoming.pos
                    if completed == len(encoded):
                        return DecodeResult.complete(output, completed)
                if incoming.pos == previous and outgoing.pos == 0:
                    # An incomplete input must never pass merely because all bytes were read.
                    return DecodeResult("rejected", incoming.pos, completed, len(output))
        finally:
            self.free(decoder)

    def decode_frames(self, encoded, *, output_limit=MAX_DECODED_BYTES):
        denied = check_decode_limits(encoded, output_limit)
        if denied is not None:
            return denied

        source = c.create_string_buffer(encoded)
        destination = c.create_string_buffer(output_limit + 1)
        output = bytearray()
        offset = 0
        while offset < len(encoded):
            start = c.byref(source, offset)
            frame_size = self.frame_size(start, len(encoded) - offset)
            if self.is_error(frame_size):
                return DecodeResult("rejected", completed_bytes=offset, decoded_bytes=len(output))
            if not 0 < frame_size <= len(encoded) - offset:
                raise RuntimeError("Zstandard reported an invalid frame size")
            produced = self.decode_frame(destination, output_limit - len(output) + 1,
                                         start, frame_size)
            if self.is_error(produced):
                status = "limit" if self.error_code(produced) == ZSTD_DST_TOO_SMALL else "rejected"
                return DecodeResult(status, completed_bytes=offset, decoded_bytes=len(output))
            output.extend(destination[:produced])
            if len(output) > output_limit:
                return DecodeResult("limit", offset + frame_size, offset, len(output))
            offset += frame_size

        return DecodeResult.complete(output, offset)


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
