"""Reproduce measurement and ranking behavior from pinned upstream source."""
import argparse
import gzip
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch


PINS = {
    "lzbench": ("4afc06fa5fa98f70f1c4185865349567da5d583f", "bench/lzbench.cpp"),
    "TurboBench": ("e15988f037f4255a818a3407bd09a00a2aa6052b", "turbobench.c"),
    "weissman": ("f28197250f3dc7c42373665218892d21592de8f3", "weissman.py"),
}


def run(argv, **kwargs):
    return subprocess.run(argv, check=True, timeout=30, capture_output=True, **kwargs)


def pinned_source(checkouts, name):
    commit, path = PINS[name]
    return run(["git", "-C", str(checkouts / name), "show", f"{commit}:{path}"],
               text=True).stdout


def extract(source, path, first, last):
    return f'#line {first} "{path}"\n' + "\n".join(source.splitlines()[first - 1:last]) + "\n"


def native(directory, name, parts, compiler, standard, extension):
    source = directory / f"{name}.{extension}"
    binary = directory / name
    source.write_text("\n".join(parts), encoding="utf-8")
    try:
        run([compiler, f"-std={standard}", "-O0", str(source), "-o", str(binary)])
        return json.loads(run([str(binary)], text=True).stdout)
    except subprocess.CalledProcessError as error:
        raise RuntimeError(error.stderr.decode("utf-8", "replace") or str(error)) from error


def lzbench(checkouts, directory, cxx):
    source = pinned_source(checkouts, "lzbench")
    path = PINS["lzbench"][1]
    prelude = r'''
#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
using uint64 = unsigned long long;
using int64 = long long;
#define DISABLE_THREADING
#define GetTime(now) ((now) = clock_tick())
#define GetDiffTime(rate, start, end) ((end) - (start))
#define LZBENCH_PRINT(...) ((void)0)
#define MIN(a, b) ((a) < (b) ? (a) : (b))
static size_t common(const char *, const char *) { return 0; }
static unsigned call, clock_reads;
static uint64_t tick;
static bool first_wrong, last_wrong;
static uint64_t clock_tick() {
    if (clock_reads++ == 3) tick += 2000;
    return tick;
}
static int lzbench_compress(void *, int, int, int, const char *, char *, size_t, int *) {
    tick += call++ == 0 ? 12000 : 30000;
    return 1;
}
static int lzbench_decompress(void *, int, int, int, const char *, char *out, int *) {
    *out = (call++ == 0 ? first_wrong : last_wrong) ? 'x' : 'a';
    tick += 12000;
    return 1;
}
'''
    encode = r'''
static void encode() {
    struct { uint64_t cloop_time = 42000; } settings;
    auto *params = &settings;
    struct { int compress = 0; } codec;
    auto *desc = &codec;
    int sizes = 0, compr_sizes = 0, codec_options = 0, i = 0, g_exit_result = 0;
    int *effective_chunk_sizes = &sizes;
    size_t insize = 1, comprsize = 1;
    char inbuf[] = "a", compbuf[1] = {};
    uint64_t loop_ticks, start_ticks, end_ticks, nanosec;
    int64_t complen;
    std::vector<uint64_t> ctime;
    [[maybe_unused]] bool comp_error = false;
    [[maybe_unused]] float speed;
    call = clock_reads = 0;
    tick = 0;
'''
    report_encode = r'''
    if (ctime.size() != 3 || call != 2) std::abort();
    std::sort(ctime.begin(), ctime.end());
    printf("{\"mixed_ns\":[%llu,%llu,%llu],", (uint64)ctime[0], (uint64)ctime[1], (uint64)ctime[2]);
    return;
stats:
    std::abort();
}
'''
    decode = r'''
static bool decode(bool bad_first, bool bad_last) {
    struct { uint64_t dloop_time = 24000; int verbose = 0; } settings;
    auto *params = &settings;
    struct { int decompress = 0; const char *name = "fixture"; } codec;
    auto *desc = &codec;
    int sizes = 0, compr_sizes = 0, codec_options = 0, i = 0, g_exit_result = 0;
    int *effective_chunk_sizes = &sizes;
    size_t insize = 1, max_chunk_size = 1;
    char inbuf[] = "a", compbuf[1] = {}, decomp[1] = {};
    uint64_t loop_ticks, start_ticks, end_ticks, nanosec;
    int64_t decomplen;
    std::vector<uint64_t> dtime;
    bool decomp_error = false;
    first_wrong = bad_first;
    last_wrong = bad_last;
    call = clock_reads = 0;
    tick = 0;
    do {
'''
    report_decode = r'''
    } while (false);
    if (dtime.size() != 3 || call != 2 || g_exit_result != (decomp_error ? 11 : 0))
        std::abort();
    return decomp_error;
}
int main() {
    encode();
    printf("\"clean_failed\":%d,\"first_wrong_failed\":%d,\"last_wrong_failed\":%d}\n",
           decode(false, false), decode(true, false), decode(false, true));
}
'''
    return native(directory, "lzbench", [
        prelude, encode, extract(source, path, 601, 630), '#line 1 "fixture-encode"',
        report_encode, decode, extract(source, path, 650, 705),
        '#line 1 "fixture-decode"', report_decode,
    ], cxx, "c++17", "cpp")


def turbobench(checkouts, directory, cc):
    source = pinned_source(checkouts, "TurboBench")
    fixture = r'''
int main(void) {
    int a = 0, b = 1;
    double tied[] = {0, 0};
    plug_t rows[] = {{.id = 4, .len = 10, .tc = 2, .td = 1},
                     {.id = 5, .len = 10, .tc = 1, .td = 1}};
    g_plug = rows;
    g_copeland = g_borda = tied;
    rankmode = RANK_RATIO;
    int self = final_cmp(&a, &a);
    int different_ids = final_cmp(&a, &b);
    rows[1].id = 4;
    int same_ab = final_cmp(&a, &b), same_ba = final_cmp(&b, &a);
    rows[1].id = 5;
    plugrank(rows, 2);
    unsigned ratio_a = rows[0].rank, ratio_b = rows[1].rank;
    rankmode |= RANK_COMP;
    plugrank(rows, 2);
    unsigned both_a = rows[0].rank, both_b = rows[1].rank;
    rows[1].tc = rows[0].tc;
    plugrank(rows, 2);
    printf("{\"self\":%d,\"different_ids\":%d,\"same_id_pair\":[%d,%d],"
           "\"ratio_only_ranks\":[%u,%u],\"ratio_and_encode_ranks\":[%u,%u],"
           "\"equal_metric_ranks\":[%u,%u]}\n",
           self, different_ids, same_ab, same_ba, ratio_a, ratio_b, both_a, both_b,
           rows[0].rank, rows[1].rank);
}
'''
    return native(directory, "turbobench", [
        "#include <stdio.h>\n#include <stdlib.h>",
        extract(source, PINS["TurboBench"][1], 668, 785),
        '#line 1 "fixture-rank"', fixture,
    ], cc, "c11", "c")


def weissman(checkouts, directory):
    module = {"__name__": "weissman_fixture"}
    exec(compile(pinned_source(checkouts, "weissman"), "weissman.py", "exec"), module)
    tick = 100
    close_completed = False

    class TrackedGzip:
        def __init__(self, *args, **kwargs):
            self.stream = gzip.open(*args, **kwargs)

        def __enter__(self):
            return self

        def writelines(self, lines):
            nonlocal tick
            self.stream.writelines(lines)
            tick += 2

        def __exit__(self, *exc):
            nonlocal tick, close_completed
            self.stream.close()
            tick += 3
            close_completed = True

    def closed_temp(**kwargs):
        fd, name = tempfile.mkstemp(dir=directory, **kwargs)
        os.close(fd)
        return fd, name

    data = directory / "input"
    data.write_bytes(b"Isobar\n")
    with patch.dict(module, time=SimpleNamespace(time=lambda: tick),
                    gzip=SimpleNamespace(open=TrackedGzip),
                    tempfile=SimpleNamespace(mkstemp=closed_temp)):
        _, baseline_elapsed = module["gzip_compr_test"](data)

    child = directory / "emit.py"
    child.write_text("from pathlib import Path\nPath('output').write_bytes(b'x')\n",
                     encoding="ascii")
    previous = Path.cwd()
    try:
        os.chdir(directory)
        ratio, _ = module["target_compr_test"]("python3 emit.py", data, "output")
    finally:
        os.chdir(previous)

    def score(reference_time, candidate_time):
        with patch.dict(module, target_compr_test=lambda *_: (1, candidate_time),
                        gzip_compr_test=lambda *_: (1, reference_time)):
            return module["weissman"]("", "", "", 1, 1)

    try:
        score(2, 1)
    except ZeroDivisionError:
        singularity = "ZeroDivisionError"
    else:
        raise AssertionError("Expected upstream singularity")
    return {"baseline_elapsed_ticks": baseline_elapsed, "total_ticks": tick - 100,
            "close_completed": close_completed, "non_gzip_target_ratio": ratio,
            "singularity": singularity, "subunit_score": score(0.5, 0.25)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkouts", type=Path)
    parser.add_argument("--cc", default="cc")
    parser.add_argument("--cxx", default="c++")
    args = parser.parse_args()
    checkouts = args.checkouts.resolve()
    with tempfile.TemporaryDirectory(prefix="isobar-reproduce-") as scratch:
        directory = Path(scratch)
        result = {
            "lzbench": lzbench(checkouts, directory, args.cxx),
            "TurboBench": turbobench(checkouts, directory, args.cc),
            "weissman": weissman(checkouts, directory),
        }
    expected = json.loads(Path(__file__).with_name("upstream-expected.json").read_text())
    print(json.dumps(result, indent=2, sort_keys=True))
    if result != expected:
        raise SystemExit("Upstream fixture results differ from the expected results")


if __name__ == "__main__":
    main()
