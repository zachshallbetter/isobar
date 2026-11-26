"""Check the experimental schema, metric and codec vectors."""
import argparse
import gzip
import math
from pathlib import Path
import subprocess
import sys
import zlib

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conformance.scoring import composite, positive
from conformance.verify_evidence import strict_json, validator, verify_records


HERE = Path(__file__).resolve().parent


def load(path):
    return strict_json(path.read_text(encoding="utf-8"))


def schema_cases(vectors):
    for vector in vectors["records"]:
        observation = dict(vectors["observation"], **vector.get("change_observation", {}))
        validation = dict(vectors["validation"], **vector.get("change_validation", {}))
        for key in vector.get("drop_observation", []):
            del observation[key]
        if vector.get("no_validation"):
            validation = None
        try:
            records = [observation, validation] if validation is not None else [observation]
            (verdict,) = verify_records(records)
        except ValueError:
            actual = "invalid"
        else:
            actual = "qualified" if verdict["qualified"] else "unqualified"
        require_equal(vector, actual)
    for vector in vectors["json"]:
        try:
            strict_json(vector["raw"], evidence=True)
        except ValueError:
            actual = "invalid"
        else:
            actual = "valid"
        require_equal(vector, actual)
    for vector in vectors["numeric_wire"]:
        observation = dict(vectors["observation"])
        try:
            observation["elapsed_ns"] = strict_json(vector["raw"], evidence=True)
            valid = validator("observation").is_valid(observation)
        except ValueError:
            valid = False
        require_equal(vector, "valid" if valid else "invalid")
    return len(vectors["records"]) + len(vectors["json"]) + len(vectors["numeric_wire"])


def weissman(reference_time, candidate_time):
    if not positive(reference_time) or not positive(candidate_time) or candidate_time == 1:
        return "unavailable"
    # These vectors hold both ratios and alpha equal to one.
    return math.log(reference_time) / math.log(candidate_time)


def dominates(a, b, directions):
    pairs = [{"max": (x, y), "min": (y, x)}[direction]
             for x, y, direction in zip(a, b, directions, strict=True)
             if direction != "disabled"]
    if any(x is None or y is None for x, y in pairs):
        return "unavailable"
    return all(x >= y for x, y in pairs) and any(x > y for x, y in pairs)


def pareto(points, directions):
    dominated = set()
    for candidate, a in points.items():
        for other, b in points.items():
            comparison = dominates(b, a, directions)
            if comparison == "unavailable":
                return comparison
            if other != candidate and comparison:
                dominated.add(candidate)
    return sorted(points.keys() - dominated)


def metric(vector):
    operation = vector["operation"]
    if operation == "composite":
        return composite(vector["metrics"])
    if operation == "historical":
        return weissman(vector["reference_time"], vector["candidate_time"])
    if operation == "dominates":
        return dominates(vector["a"], vector["b"], vector["directions"])
    if operation == "frontier":
        return pareto(vector["points"], vector["directions"])
    if operation == "ratio":
        original, compressed = map(sum, zip(*vector["sizes"], strict=True))
        return original / compressed if original and compressed else "unavailable"
    if operation == "throughput_bytes_per_second":
        work, elapsed = map(sum, zip(*vector["trials"], strict=True))
        if any(duration <= 0 for _, duration in vector["trials"]):
            return "unavailable"
        return work * 1_000_000_000 / elapsed
    raise ValueError(f"Unknown metric operation: {operation}")


def require_equal(vector, actual):
    expected = vector["expected"]
    same = (math.isclose(actual, expected, rel_tol=1e-12, abs_tol=0)
            if type(expected) is float and type(actual) in (int, float)
            else type(expected) is type(actual) and actual == expected)
    if not same:
        raise AssertionError(f"{vector['id']}: expected {expected!r}, got {actual!r}")


def zstd_result(zstd, encoded, vector):
    result = subprocess.run([zstd, "-q", "-d", "-c"], input=encoded, capture_output=True,
                            timeout=10)
    if result.returncode < 0:
        raise RuntimeError(f"zstd ended on signal {-result.returncode}")
    output_hex = result.stdout.hex()
    if result.returncode == 0:
        return {"output_hex": output_hex}
    expected_prefix = vector.get("rejected_output_hex")
    if expected_prefix is not None and output_hex != expected_prefix:
        raise AssertionError(f"{vector['id']}: unexpected prefix on failure {output_hex!r}")
    return "rejected"


def codec_cases(vectors, zstd):
    for vector in vectors:
        encoded = bytes.fromhex(vector["input_hex"])
        if vector["format"] == "gzip":
            try:
                actual = {"output_hex": gzip.decompress(encoded).hex()}
            except (OSError, EOFError, zlib.error):
                actual = "rejected"
            if vector.get("first_member_control") is not None:
                decoder = zlib.decompressobj(wbits=31)
                first = decoder.decompress(encoded)
                control = {"output_hex": first.hex(), "at_member_end": decoder.eof,
                           "unused_bytes": len(decoder.unused_data)}
                if control != vector["first_member_control"]:
                    raise AssertionError(f"{vector['id']}: unexpected one-member result {control}")
        elif vector["format"] == "zstd":
            actual = zstd_result(zstd, encoded, vector)
        else:
            raise ValueError(f"Unknown format: {vector['format']}")
        require_equal(vector, actual)
    return len(vectors)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zstd", default="zstd", help="Zstandard reference CLI")
    args = parser.parse_args()
    vectors = load(HERE / "vectors.json")
    records = schema_cases(vectors["evidence"])
    for vector in vectors["metrics"]:
        require_equal(vector, metric(vector))
    codecs = codec_cases(vectors["codecs"], args.zstd)
    print(f"Passed {records} evidence, {len(vectors['metrics'])} metric and {codecs} codec vectors.")


if __name__ == "__main__":
    main()
