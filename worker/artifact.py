"""Trusted local artifact completion checks."""
from dataclasses import dataclass
import zlib


MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_DECODED_BYTES = 1024 * 1024


@dataclass(frozen=True)
class DecodeResult:
    """Local diagnostics. Output is available after complete decoding only."""

    status: str
    consumed_bytes: int | None = None
    completed_bytes: int = 0
    decoded_bytes: int = 0
    output: bytes | None = None

    @classmethod
    def complete(cls, output, consumed):
        return cls("complete", consumed, consumed, len(output), bytes(output))

    def matches(self, encoded, expected):
        return (self.status == "complete" and self.consumed_bytes == len(encoded)
                and self.completed_bytes == len(encoded) and self.decoded_bytes == len(expected)
                and self.output == expected)


def check_decode_limits(encoded, output_limit):
    if type(output_limit) is not int or not 0 <= output_limit <= MAX_DECODED_BYTES:
        raise ValueError("Output limit must be an integer between 0 and 1 MiB")
    if not encoded:
        return DecodeResult("rejected", consumed_bytes=0)
    if len(encoded) > MAX_ARTIFACT_BYTES:
        return DecodeResult("limit", consumed_bytes=0)

    return None


def gzip_members(encoded, *, output_limit=MAX_DECODED_BYTES):
    denied = check_decode_limits(encoded, output_limit)
    if denied is not None:
        return denied

    source = memoryview(encoded)
    output = bytearray()
    offset = 0
    while offset < len(encoded):
        member = zlib.decompressobj(wbits=31)
        try:
            part = member.decompress(source[offset:], output_limit - len(output) + 1)
        except zlib.error:
            # zlib's Python binding does not expose progress from a failed call.
            return DecodeResult("rejected", completed_bytes=offset, decoded_bytes=len(output))

        output.extend(part)
        consumed = len(encoded) - len(member.unused_data or member.unconsumed_tail)
        if len(output) > output_limit:
            return DecodeResult("limit", consumed, offset, len(output))
        if not member.eof or consumed <= offset:
            return DecodeResult("rejected", consumed, offset, len(output))
        # eof includes the gzip trailer check. Continue on any remaining bytes.
        offset = consumed

    return DecodeResult.complete(output, offset)


def decode_gzip(encoded):
    result = gzip_members(encoded)
    if result.status != "complete":
        raise EOFError(f"Gzip artifact did not complete: {result.status}")
    return result.output
