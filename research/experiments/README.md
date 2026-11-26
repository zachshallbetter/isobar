# Reproducing the measurement experiments

Use Python 3.10 or later, Git, a C11 compiler and a C++17 compiler. Fetch the exact revisions in a disposable directory. The scripts create any generated source and binaries in a temporary directory.

```sh
upstream=$(mktemp -d)
fetch_source() {
  git init -q "$upstream/$1" &&
    git -C "$upstream/$1" fetch -q --depth=1 "$2" "$3"
}
fetch_source lzbench https://github.com/inikep/lzbench.git 4afc06fa5fa98f70f1c4185865349567da5d583f
fetch_source TurboBench https://github.com/powturbo/TurboBench.git e15988f037f4255a818a3407bd09a00a2aa6052b
fetch_source weissman https://github.com/lorenzosaino/weissman-score.git f28197250f3dc7c42373665218892d21592de8f3
python3 -B research/experiments/upstream.py "$upstream"
python3 -B research/experiments/gzip-boundaries.py > /tmp/isobar-gzip-boundaries.jsonl
```

The upstream run checks its observations against [expected results](upstream-expected.json). It does not build the full benchmark applications. `--cc` and `--cxx` select the compilers. The gzip pilot prints fresh timing data. Compare the shape, work and validation of the run with the [recorded run](gzip-boundaries.jsonl). Timing values will vary.

See the [evidence report](../measurement-evidence.md) for what each run establishes.

## Run the blocked timing pilot

From the repository root, use Python 3.10 or later on macOS or Linux.

```sh
python3 -B -m research.experiments.blocked_timing --run-id local-pilot > /tmp/isobar-blocked.json
python3 -B -m research.experiments.blocked_timing --simulate 5000
```

The pilot report includes the fixed plan, per-worker clock probes, existing IBS-0003 records and the derived result. The exact saved observation and validation records are in [macOS JSONL](blocked-macos.jsonl) and [Linux VM JSONL](blocked-linux-qemu.jsonl). The [methods and results](../measurement-methodology.md#executed-blocked-timing-pilot) specify the estimand, stopping rule, exclusions and conditions.

Install the [conformance dependencies](../../conformance/README.md) to run the automated checks and independently verify the archived records.

```sh
/tmp/isobar-conformance/bin/python -B -m unittest -v research.experiments.test_blocked_timing
/tmp/isobar-conformance/bin/python -B conformance/verify_evidence.py research/experiments/blocked-macos.jsonl
/tmp/isobar-conformance/bin/python -B conformance/verify_evidence.py research/experiments/blocked-linux-qemu.jsonl
```
