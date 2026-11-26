# Experimental conformance vectors

These public test vectors exercise the draft measurement, evidence and scoring rules along with a small set of gzip and Zstandard artifact cases. The checker exercises proposed records and metric behavior. The JSONL readers below verify individual records and qualification.

From the repository root, use Python 3.10 or later and Zstandard CLI 1.5.7. The tested reference CLI was built at [f8745da6](https://github.com/facebook/zstd/tree/f8745da6ff1ad1e7bab384bd1f9d742439278e99) with `make -C programs zstd HAVE_ZLIB=0 HAVE_LZMA=0 HAVE_LZ4=0`.

```sh
python3 -m venv /tmp/isobar-conformance
/tmp/isobar-conformance/bin/python -m pip install -r conformance/requirements.txt
/tmp/isobar-conformance/bin/python -B conformance/check.py --zstd zstd
```

Pass the absolute path to the built binary with `--zstd` if it is not on PATH. The [vector file](vectors.json) stores fixed artifact bytes and exact expected output. Resetting, byte accounting and timing across platforms require additional adapter tests. For the metric examples, numeric values use a relative comparison tolerance of `1e-12`, and the prototype uses an absolute tolerance of `1e-12` on the weight sum. These tolerances apply only to this executable proposal.

Structural schema checks, semantic reference checks and measurement qualification are separate assertions. The measurement records use opaque fixture identifiers. They do not define a run identity or a hash canonicalization scheme.

## Run the complete local suite

Set up the Python and Ruby dependencies, native libraries, and pinned report inputs described below. From the repository root, set `upstream` to the [pinned source checkout directory](../research/experiments/README.md).

```sh
: "${ISOBAR_LIBDEFLATE:?Set the libdeflate path}" "${ISOBAR_ZSTD_LIB:?Set the libzstd path}"
: "${ISOBAR_LZBENCH_REPORT_DIR:?Set the lzbench report directory}" "${ISOBAR_TURBOBENCH_REPORT:?Set the pinned TurboBench report path}"
: "${upstream:?Set the pinned source checkout directory}"
/tmp/isobar-conformance/bin/python -B -m unittest discover -v
/tmp/isobar-conformance/bin/python -B conformance/check.py --zstd /path/to/zstd
/tmp/isobar-conformance/bin/python -B research/experiments/upstream.py "$upstream"
```

The dependency checks keep the pinned-report tests enabled and surface missing library paths before discovery. The vector checker and upstream reproducer run separately from the unit suite.

## Verify worker evidence

Install the Python dependency above. Run the reader against an experimental IBS-0003 JSONL file.

```sh
/tmp/isobar-conformance/bin/python -B conformance/verify_evidence.py /tmp/isobar-example.jsonl
```

The reader emits one verdict per observation after validating the whole input. Exit 0 means the input is valid, including when a verdict is unqualified. Exit 1 means the input is invalid and produces no verdicts.

The independent conformance reader uses Ruby with JSON 2.18 or later and JSONSchemer 2.4.0. The end-to-end tests compare both readers against actual worker output and controlled failures.

```sh
gem install json --version 2.18.0
gem install json_schemer --version 2.4.0
ruby conformance/verify_evidence.rb /tmp/isobar-example.jsonl
/tmp/isobar-conformance/bin/python -B -m unittest -v worker.test_measure conformance.test_evidence
```

## Verify whole gzip and Zstandard artifacts

The artifact checker runs each of the 11 existing format vectors through its candidate and reference paths. Use Python 3.10 or later and trusted local shared builds of [libdeflate 1.25](https://github.com/ebiggers/libdeflate/tree/c8c56a20f8f621e6a966b716b31f1dedab6a41e3) and [Zstandard 1.5.7](https://github.com/facebook/zstd/tree/f8745da6ff1ad1e7bab384bd1f9d742439278e99). From the respective source trees, build libdeflate with CMake and Zstandard with Make.

```sh
# In the libdeflate source tree
cmake -B build -DLIBDEFLATE_BUILD_GZIP=OFF -DLIBDEFLATE_BUILD_STATIC_LIB=OFF
cmake --build build

# In the Zstandard source tree
make -C lib libzstd
```

From the Isobar root, point the commands at the shared libraries. The example uses macOS filenames. Use the corresponding `.so` files on Linux.

```sh
export ISOBAR_LIBDEFLATE=/path/to/libdeflate/build/libdeflate.dylib
export ISOBAR_ZSTD_LIB=/path/to/zstd/lib/libzstd.dylib
python3 -B -m conformance.check_artifacts --libdeflate "$ISOBAR_LIBDEFLATE" --zstd-lib "$ISOBAR_ZSTD_LIB"
python3 -B -m unittest -v conformance.test_artifacts
```

The gzip candidate is the worker's per-member zlib path. The gzip reference uses libdeflate. Zstandard uses streaming and frame-by-frame one-shot paths in the same libzstd build. These are separate adapter paths with a shared implementation. The original conformance command still checks the Zstandard CLI.

Each diagnostic records the adapter build, terminal status, compressed input length, actual consumption when available, the last completed boundary and the number of output bytes observed. A rejection exposes no accepted output. A missing consumed count means the failing API did not report it. The completed prefix remains visible.

Local bounds are 2 MiB compressed and 1 MiB decoded. The Zstandard streaming path also caps its window at 1 MiB. The one-shot reference writes to a bounded output buffer. These limits describe the local fixture runner. HTTP content encoding has its own requirements under [RFC 9659](https://datatracker.ietf.org/doc/html/rfc9659).

## Replay qualified scoring

The Python scorer and independent Ruby verifier use the evidence readers above. Replay throughput from an existing qualified run with the compression ratio disabled.

```sh
/tmp/isobar-conformance/bin/python -B -m conformance.scoring research/experiments/blocked-macos.jsonl --baseline reference --ratio-weight-bps 0
ruby conformance/score_evidence.rb research/experiments/blocked-macos.jsonl --baseline reference --ratio-weight-bps 0
/tmp/isobar-conformance/bin/python -B -m unittest -v conformance.test_scoring
```

The default assigns 5,000 basis points each to ratio and throughput. To score ratio, repeat `--artifact OBS_ID ORIGINAL_PATH ARTIFACT_PATH` once per invocation in the observation. The scorer identifies gzip or Zstandard by magic bytes and checks the complete artifact against its original and compares input contents across complete artifact sets in a matched slot. Set `ISOBAR_ZSTD_LIB` for Zstandard artifacts. The Ruby replay uses Fiddle and the same native library through its own frame loop. The archived timing records lack artifact sizes, so replaying them with ratio enabled reports a missing metric. Set `--ratio-weight-bps 10000` for ratio-only ranking.

The readers report exact totals, rational metrics, observation references, versioned tie rules and the experimental qualification result. Python uses binary64 logarithms. Ruby independently uses decimal logarithms and exact rationals. Executable checks compare exact reported data and allow a relative tolerance of `1e-12` for the composite. See the [IBS-0004 replay profile](../specifications/IBS-0004.md#experimental-replay-from-evidence) for the applicability and publication rules.

## Inspect pinned upstream reports

The importer reads a local file with a limit of 1 MiB. It emits a diagnostic JSON object that preserves aggregate rows and their provenance. The supported layouts are `lzbench-csv-v1` and `turbobench-markdown-v1`. An unknown layout or failed digest check produces an unavailable result and no rows. Ambiguous cells keep their displayed value and have no usable numeric value.

From the Isobar root, extract the published TurboBench snapshot from a checkout containing the pinned commit. The checkout can remain on another revision.

```sh
turbo_commit=e15988f037f4255a818a3407bd09a00a2aa6052b
git -C /path/to/TurboBench show "$turbo_commit:benchmark.md" > /tmp/isobar-turbobench-pinned.md
python3 -B -m conformance.import_reports /tmp/isobar-turbobench-pinned.md \
  --format turbobench-markdown-v1 --report-revision "$turbo_commit" \
  --source-url "https://github.com/powturbo/TurboBench/blob/$turbo_commit/benchmark.md" \
  --expected-sha256 be4afc9e683e7ab048c3a3d90cadfb5b782a7f4f8244acdf067b8171d83c3d6c
```

Use lzbench source at `4afc06fa5fa98f70f1c4185865349567da5d583f` to generate a fresh CSV without downloading a corpus. The timing cells vary on each run. Record the generated report digest if retaining a capture.

```sh
lz_source=/path/to/lzbench
lz_build=$(mktemp -d)
make -C "$lz_build" -f "$lz_source/Makefile" BUILD_ONLY=ZSTD
awk 'BEGIN { for (i = 1; i <= 8192; i++) printf "isobar lzbench fixture %05d\n", i }' > "$lz_build/fixture.txt"
(
  cd "$lz_build" || exit
  ./lzbench -V > version.txt
  ./lzbench -o4 -p3 -x -t0,0 -i3,3 -ezstd,1,3 fixture.txt > speed.csv
  ./lzbench -o4 -p3 -x -z -t0,0 -i3,3 -ezstd,1,3 fixture.txt > time.csv
)
python3 -B -m conformance.import_reports "$lz_build/speed.csv" \
  --format lzbench-csv-v1 --producer-version 2.4.2 \
  --producer-revision 4afc06fa5fa98f70f1c4185865349567da5d583f --declared-estimator median
```

The `--report-revision` identifies the published report snapshot. The `--producer-revision`, `--producer-version`, `--producer-sha256` and `--declared-estimator` describe externally established generator provenance. The importer never treats a report revision as proof of a binary revision.

Run the synthetic and gate tests with the Python and Ruby dependencies above. Set both paths to enable the pin-specific integration tests.

```sh
export ISOBAR_LZBENCH_REPORT_DIR="$lz_build"
export ISOBAR_TURBOBENCH_REPORT=/tmp/isobar-turbobench-pinned.md
/tmp/isobar-conformance/bin/python -B -m unittest -v conformance.test_import_reports
```

See the [upstream import evidence](../research/measurement-evidence.md#aggregate-report-imports) for the observed layouts, source limits and fixture handling.
