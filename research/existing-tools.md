# Existing tools: initial evidence review

Reviewed 2026-11-26. Source snapshots identified by Git blob SHA where available; links to branches may change. No upstream code was executed or modified.

## Weissman score calculator — source inspection (S)

Source: [weissman.py](https://github.com/lorenzosaino/weissman-score/blob/master/weissman.py), blob SHA `accb5a8c05735c80780a5f44544a86851f5d3a81`, declares version 0.2.

| ID | Evidence in source | Consequence / proposed Isobar response |
|---|---|---|
| W-01 | gzip_compr_test times writelines inside the gzip context; target_compr_test times subprocess.call | Reference excludes finalization on close while candidate includes process lifetime; require matched boundaries. |
| W-02 | Both use time.time | Wall time may change; use monotonic elapsed clock and declare resolution. |
| W-03 | Both calculate sizes; no decode/compare step | Size alone cannot establish losslessness; require exact reconstruction outside timed region. |
| W-04 | weissman directly divides log(T_b) by log(T) | Nonpositive durations and T=1 are not guarded; unit sensitivity is inherent in formula. |
| W-05 | Candidate repetitions precede reference repetitions | Order can confound thermal/cache effects; use seeded randomized or balanced blocks. |
| W-06 | tempfile.mkstemp(...)[1] discards descriptor | Descriptor not explicitly closed; use scoped temporary resources. |
| W-07 | command.split(" ") | Quoted paths and arguments are not faithfully parsed; use explicit argument arrays. |
| W-08 | mean of ratio and time observations; returns only score | Preserve per-trial records and uncertainty. |
| W-09 | Removes supplied output path after success | Use exclusive harness-owned workspace; never remove preexisting user output. |
| W-10 | gzip default level 9, candidate arbitrary command | Disclose levels and versions; compare configuration sweeps or named profiles. |

These are static observations; severity depends on intended workload. Reproduce with disposable files, fake adapters, invalid output, zero/near-one timings, and paths containing spaces. No upstream issue has been filed.

## lzbench — documentation review (D)

[README](https://github.com/inikep/lzbench/blob/master/README.md), blob `838c27e0a37926c199503320f0a9b6f01fcf2646`.

Documents in-memory execution in a common executable, output equality checks, codec/level selection, adjustable time and iteration counts, and compiler/build integration. This is a strong precedent for a native codec profile. Common compiler does not itself prove equivalent configuration or optimization. Inspect runner, timing, verification and codec-specific build patches before asserting implementation differences. Do not duplicate its adapter coverage unnecessarily; investigate import or bridge compatibility.

## TurboBench — documentation review (D)

[README](https://github.com/powturbo/TurboBench/blob/master/README.md), blob `e6197458003d9930a750c101c213ba25edf6daf6`; [published results](https://github.com/powturbo/TurboBench/blob/master/benchmark.md).

Documents in-memory tests, codec parameters, multi-file and block modes, minimum iteration time, thermal mitigation controls, optional verification modes, memory/stack reporting, rank aggregation and multiple output formats. Published results label Pareto points. These features overlap significantly with Isobar's proposed scope. Verify implementation and defaults before comparing. Evaluate whether an importer can retain configuration and provenance without implying unavailable raw observations.

## Open investigations (U)

Squash, TurboBench and lzbench source-level review; benchmark licenses and redistribution; reference codec build/format interoperability; historical score conventions; supported hardware counters. No claim of unique feature coverage or superiority is warranted.

## Decision (P)

Prioritize interoperable evidence format, explicit comparison classes, reproducible run protocol and independently versioned scoring. Existing tools may be producers of compatible evidence; missing fields remain missing, never invented. Evaluate a bridge before writing a broad codec catalog.
