"""Verify experimental IBS-0003 JSONL records and derive qualification."""
import argparse
from decimal import Decimal, InvalidOperation
from functools import cache
import json
from pathlib import Path
import sys

from jsonschema import Draft202012Validator


MAX_COUNTER = (1 << 53) - 1
SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"


def reject_constant(value):
    raise ValueError(f"Non-JSON constant: {value}")


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate property: {key!r}")
        result[key] = value
    return result


def evidence_number(raw):
    try:
        number = Decimal(raw)
    except InvalidOperation as error:
        # Even an exponent larger than Decimal supports leaves exact zero unchanged.
        significand = raw.lower().split("e", 1)[0]
        if set(significand) <= set("0.-"):
            return 0
        raise ValueError("Counter exponent out of range") from error
    if number != number.to_integral_value() or number.copy_abs() > MAX_COUNTER:
        raise ValueError("Counter is not an exactly representable integer")
    return int(number)


def strict_json(raw, *, evidence=False):
    return json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique_pairs,
                      parse_float=evidence_number if evidence else None)


@cache
def validator(name):
    schema = strict_json((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def verify_records(records):
    observations = {}
    validations = {}
    for record in records:
        is_validation = isinstance(record, dict) and "validation_id" in record
        kind = "validation" if is_validation else "observation"
        error = next(validator(kind).iter_errors(record), None)
        if error is not None:
            fields = ".".join(str(part) for part in error.absolute_path)
            raise ValueError(f"Invalid {kind} schema at {fields or 'record'}")
        index = validations if is_validation else observations
        key = (record["run_id"], record[f"{kind}_id"])
        if key in index:
            raise ValueError(f"Duplicate {kind} identity {key!r}")
        for value in record.values():
            if isinstance(value, str):
                value.encode("utf-8")
        index[key] = record

    if not observations:
        raise ValueError("No observations")

    linked = set()
    verdicts = []
    for key, observation in observations.items():
        run_id, observation_id = key
        qualified = False
        if "validation_ref" in observation:
            validation_key = (run_id, observation["validation_ref"])
            validation = validations.get(validation_key)
            if validation is None or validation["observation_id"] != observation_id:
                raise ValueError(f"Missing or mismatched validation for {key!r}")
            count = (observation["invocation_count"]
                     if validation["cadence"] == "every_invocation" else 1)
            if validation["checked_invocations"] != count:
                raise ValueError(f"Validation cadence and count disagree for {key!r}")
            linked.add(validation_key)
            qualified = (observation["phase"] == "measurement" and observation["status"] == "ok"
                         and validation["status"] == "verified"
                         and count == observation["invocation_count"])
        verdicts.append({"run_id": run_id, "observation_id": observation_id, "qualified": qualified})

    if len(linked) != len(validations):
        raise ValueError("Unreferenced validation record")
    return verdicts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="UTF-8 JSONL file containing observation and validation records")
    args = parser.parse_args()
    try:
        with args.path.open(encoding="utf-8", newline="\n") as source:
            verdicts = verify_records(strict_json(line, evidence=True) for line in source)
    except (OSError, ValueError, RecursionError) as error:
        print(f"Invalid evidence: {error}", file=sys.stderr)
        return 1
    for verdict in verdicts:
        print(json.dumps(verdict))
    return 0


if __name__ == "__main__":
    sys.exit(main())
