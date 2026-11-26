"""Bounded whole-artifact Zstandard paths for trusted local work."""
import ctypes as c
from hashlib import sha256
from pathlib import Path

from .artifact import DecodeResult, MAX_DECODED_BYTES, check_decode_limits


ZSTD_D_WINDOW_LOG_MAX = 100  # ZSTD_d_windowLogMax in zstd.h
ZSTD_WINDOW_TOO_LARGE = 16  # ZSTD_error_frameParameter_windowTooLarge
ZSTD_DST_TOO_SMALL = 70  # ZSTD_error_dstSize_tooSmall
STREAM_CHUNK_BYTES = 16 * 1024


def bind(library, name, result_type, *argument_types):
    function = getattr(library, name)
    function.argtypes = argument_types
    function.restype = result_type
    return function


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def is_zstandard(encoded):
    magic = encoded[:4]
    return (magic == b"\x28\xb5\x2f\xfd"
            or (len(magic) == 4 and magic[0] & 0xf0 == 0x50
                and magic[1:] == b"\x2a\x4d\x18"))


class ZstdBuffer(c.Structure):
    _fields_ = [("data", c.c_void_p), ("size", c.c_size_t), ("pos", c.c_size_t)]


class Zstandard:
    def __init__(self, path):
        path = Path(path).resolve(strict=True)
        library = c.CDLL(str(path))
        version = bind(library, "ZSTD_versionString", c.c_char_p)
        self.identity = {"implementation": "libzstd", "version": version().decode("ascii"),
                         "library_sha256": digest(path)}
        self.compress_bound = bind(library, "ZSTD_compressBound", c.c_size_t, c.c_size_t)
        self.compress = bind(library, "ZSTD_compress", c.c_size_t,
                             c.c_void_p, c.c_size_t, c.c_void_p, c.c_size_t, c.c_int)
        self.create = bind(library, "ZSTD_createDStream", c.c_void_p)
        self.free = bind(library, "ZSTD_freeDStream", c.c_size_t, c.c_void_p)
        self.set_parameter = bind(library, "ZSTD_DCtx_setParameter", c.c_size_t,
                                  c.c_void_p, c.c_int, c.c_int)
        self.stream = bind(library, "ZSTD_decompressStream", c.c_size_t, c.c_void_p,
                           c.POINTER(ZstdBuffer), c.POINTER(ZstdBuffer))
        self.frame_size = bind(library, "ZSTD_findFrameCompressedSize", c.c_size_t,
                               c.c_void_p, c.c_size_t)
        self.decode_frame = bind(library, "ZSTD_decompress", c.c_size_t,
                                 c.c_void_p, c.c_size_t, c.c_void_p, c.c_size_t)
        self.is_error = bind(library, "ZSTD_isError", c.c_uint, c.c_size_t)
        self.error_code = bind(library, "ZSTD_getErrorCode", c.c_int, c.c_size_t)

    def encode(self, data):
        capacity = self.compress_bound(len(data))
        destination = c.create_string_buffer(capacity)
        size = self.compress(destination, capacity, data, len(data), 3)
        if self.is_error(size):
            raise RuntimeError(f"Zstandard encode failed: {self.error_code(size)}")
        return destination[:size]

    def decode(self, encoded):
        result = self.decode_stream(encoded)
        if result.status != "complete":
            raise EOFError(f"Zstandard artifact did not complete: {result.status}")
        return result.output

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
