"""Inspect pinned upstream aggregate reports without creating IBS-0003 evidence."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
import sys


LZBENCH_SOURCE = "4afc06fa5fa98f70f1c4185865349567da5d583f"
TURBOBENCH_SNAPSHOT = "e15988f037f4255a818a3407bd09a00a2aa6052b"
TURBOBENCH_REPORT_SHA256 = "be4afc9e683e7ab048c3a3d90cadfb5b782a7f4f8244acdf067b8171d83c3d6c"
MAX_REPORT_BYTES = 1024 * 1024

LZ_HEADER_START = "Compressor name,"
LZ_HEADER_END = ",Original size,Compressed size,Ratio,Filename"
LZ_SPEED = LZ_HEADER_START + "Compression speed,Decompression speed" + LZ_HEADER_END
LZ_TIME = LZ_HEADER_START + "Compression time in us,Decompression time in us" + LZ_HEADER_END

TURBO_HEADERS = (
    "|C Size|ratio%|C MB/s|D MB/s|Rank|Name|File|",
    "|C Size|ratio|C MB/s|D MB/s|Rank|Name|File|",
)
TURBO_BOLD = re.compile(r"\$\\color\{green\}\{\\textbf\{([^{}]+)\}\}\$")
TURBO_RANK = re.compile(r"([0-9]+)([👑🏆🥇🥈🥉])?")
DECIMAL = re.compile(r"[0-9]{1,20}(?:\.[0-9]{1,12})?")
INTEGER = re.compile(r"[0-9]{1,20}")


class UnsupportedReport(ValueError):
    pass


def quantity(cell, unit, *, zero_unknown=False, pattern=DECIMAL, percent=False):
    text = cell.strip()
    numeric = text.removesuffix("%") if percent else text
    if numeric.lower() in {"nan", "-nan", "inf", "-inf", "error"}:
        return {"display": text, "value": None, "unit": unit, "reason": "producer_unavailable"}
    if not pattern.fullmatch(numeric):
        raise ValueError("Unexpected numeric cell")
    reason = None
    if unit is None:
        reason = "ambiguous_unit"
    elif zero_unknown and not numeric.strip("0."):
        reason = "zero_may_indicate_failure_or_rounding"
    result = {"display": text, "value": None if reason else numeric, "unit": unit}
    if reason:
        result["reason"] = reason
    return result


def roster(rows):
    return [{"name": row["name"], "file": row["file"]} for row in rows]


def parse_lzbench(text, provenance):
    lines = text.splitlines()
    if not lines or lines[0] not in (LZ_SPEED, LZ_TIME):
        raise UnsupportedReport("Unrecognized lzbench CSV header")
    source_reviewed = provenance["producer_revision"] == LZBENCH_SOURCE
    time_mode = lines[0] == LZ_TIME
    unit = "us" if time_mode else "MB/s (decimal)" if source_reviewed else None
    operation = "time" if time_mode else "speed"
    size_unit = "bytes" if source_reviewed else None
    ratio_unit = "percent of original" if source_reviewed else None
    rows = []
    for line in lines[1:]:
        if not line.strip():
            continue
        # This emitter writes the filename last without CSV quoting.
        name, encode, decode, original, compressed, ratio, filename = line.split(",", 6)
        if not name or not filename:
            raise ValueError("Empty lzbench label or filename")
        rows.append({
            "name": name, "file": filename,
            f"encode_{operation}": quantity(encode, unit, zero_unknown=True),
            f"decode_{operation}": quantity(decode, unit, zero_unknown=True),
            "original_bytes": quantity(original, size_unit, pattern=INTEGER),
            "compressed_bytes": quantity(compressed, size_unit, pattern=INTEGER),
            "compressed_size_percent": quantity(ratio, ratio_unit, percent=True),
        })
    if not rows:
        raise ValueError("No lzbench rows")
    return [{
        "header": lines[0], "platform": None, "displayed_candidates": roster(rows), "rows": rows,
        "timing": {
            "declared_estimator": provenance["declared_estimator"],
            "reviewed_sampling": "mixed per-invocation durations and batch means" if source_reviewed else None,
            "precision": "truncated microseconds" if time_mode else "rounded speed",
        },
        "validation": {
            "reported_outcomes": None,
            "reviewed_scope": "last decoded output of each batch when decode is enabled" if source_reviewed else None,
        },
        "ranking": None,
    }]


def turbo_cells(line):
    if not line.startswith("|") or not line.endswith("|"):
        raise ValueError("Expected a TurboBench table row")
    cells = [cell.strip() for cell in line[1:-1].split("|")]
    if len(cells) != 7 or any(not cell for cell in cells):
        raise ValueError("Unexpected TurboBench table width or empty cell")
    return cells


def parse_turbo_table(platform, lines, reviewed):
    if len(lines) < 4 or not lines[0].startswith("#### TurboBench: "):
        raise UnsupportedReport("Unrecognized TurboBench section")
    caption, header, separator, *records = lines
    if header not in TURBO_HEADERS:
        raise UnsupportedReport("Unrecognized TurboBench Markdown header")
    if any(not re.fullmatch(r":?-{3,}:?", cell) for cell in turbo_cells(separator)):
        raise ValueError("Invalid TurboBench table separator")
    unit = "MB/s (decimal)" if caption.endswith("MB=1.000.000") else None
    rows = []
    for line in records:
        cells = turbo_cells(line)
        marked_cells = []
        for index, cell in enumerate(cells):
            if match := TURBO_BOLD.fullmatch(cell):
                cells[index] = match[1]
                marked_cells.append(index)
        compressed, ratio, encode, decode, rank, name, filename = cells
        ratio_unit = "percent of original" if header == TURBO_HEADERS[0] or ratio.endswith("%") else None
        rank_match = TURBO_RANK.fullmatch(rank)
        if not rank_match:
            raise ValueError("Unrecognized TurboBench rank")
        rows.append({
            "name": name, "file": filename,
            "compressed_bytes": quantity(compressed, "bytes" if reviewed else None, pattern=INTEGER),
            "compressed_size_percent": quantity(ratio, ratio_unit, percent=True),
            "encode_speed": quantity(encode, unit, zero_unknown=True),
            "decode_speed": quantity(decode, unit, zero_unknown=True),
            "reported_rank": rank_match[1], "rank_marker": rank_match[2],
            "marked_columns": marked_cells,
        })
    return {
        "caption": caption, "header": header, "platform": platform,
        "displayed_candidates": roster(rows), "rows": rows,
        "timing": {
            "declared_estimator": None,
            "reviewed_sampling": "sum of per-chunk fastest normalized batches" if reviewed else None,
            "precision": "rounded speed",
        },
        "validation": {
            "reported_outcomes": None,
            "reviewed_scope": "byte comparison of decoded chunk when enabled" if reviewed else None,
        },
        "ranking": {
            "reported_active_metrics": None, "ranked_population": None,
            "reviewed_method": "Copeland with Borda tie-break" if reviewed else None,
        },
    }


def parse_turbobench(text, provenance):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or not lines[0].startswith("# TurboBench "):
        raise UnsupportedReport("Unrecognized TurboBench publication title")
    reviewed = (provenance["report_revision"] == TURBOBENCH_SNAPSHOT
                and provenance["report_sha256"] == TURBOBENCH_REPORT_SHA256)
    tables = []
    platform = None
    section = []
    for line in lines[1:]:
        if line.startswith("## Platform: "):
            if platform is not None:
                tables.append(parse_turbo_table(platform, section, reviewed))
            platform = line.removeprefix("## Platform: ")
            section = []
        elif platform is None:
            raise UnsupportedReport("Expected a platform section before the table")
        else:
            section.append(line)
    if platform is None:
        raise UnsupportedReport("No platform section")
    tables.append(parse_turbo_table(platform, section, reviewed))
    return tables


PARSERS = {"lzbench-csv-v1": parse_lzbench, "turbobench-markdown-v1": parse_turbobench}


def import_report(raw, *, format_name, source_url=None, report_revision=None, producer_version=None,
                  producer_revision=None, producer_sha256=None, declared_estimator=None,
                  expected_sha256=None):
    digest = sha256(raw).hexdigest()
    result = {
        "aggregate_only": True,
        "eligible_for_qualification": False,
        "eligible_for_uncertainty": False,
        "provenance": {
            "format": format_name, "source_url": source_url, "report_revision": report_revision,
            "report_sha256": digest, "expected_sha256": expected_sha256,
            "producer_version": producer_version, "producer_revision": producer_revision,
            "producer_sha256": producer_sha256, "declared_estimator": declared_estimator,
        },
        "comparison": {"corpus_sha256": None, "timed_boundary": None, "raw_trials": "unavailable"},
    }
    if expected_sha256 is not None and digest != expected_sha256:
        return dict(result, status="unavailable", reason="digest_mismatch")
    if format_name not in PARSERS:
        return dict(result, status="unavailable", reason="unsupported_format")
    try:
        tables = PARSERS[format_name](raw.decode("utf-8"), result["provenance"])
    except UnsupportedReport as error:
        return dict(result, status="unavailable", reason="unsupported_layout", detail=str(error))
    except ValueError as error:
        return dict(result, status="unavailable", reason="malformed_report", detail=str(error))
    return dict(result, status="available", tables=tables)


def hexadecimal_sha256(value):
    if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise argparse.ArgumentTypeError("Expected a SHA-256 digest")
    return value.lower()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--format", required=True, dest="format_name", metavar="FORMAT")
    parser.add_argument("--source-url")
    parser.add_argument("--report-revision")
    parser.add_argument("--producer-version")
    parser.add_argument("--producer-revision")
    parser.add_argument("--producer-sha256", type=hexadecimal_sha256)
    parser.add_argument("--expected-sha256", type=hexadecimal_sha256)
    parser.add_argument("--declared-estimator", choices=("fastest", "mean", "median"))
    kwargs = vars(parser.parse_args())
    path = kwargs.pop("path")
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_REPORT_BYTES + 1)
        if len(raw) > MAX_REPORT_BYTES:
            raise ValueError("Report exceeds the 1 MiB import limit")
    except (OSError, ValueError) as error:
        print(f"Invalid report input: {error}", file=sys.stderr)
        return 1
    result = import_report(raw, **kwargs)
    print(json.dumps(result))
    return 0 if result["status"] == "available" else 1


if __name__ == "__main__":
    sys.exit(main())
