# Runner and ranking source review

Static inspection, 2026-11-26. This page records the static pass. The [follow-up report](measurement-evidence.md) records which paths were executed.

## lzbench

[bench/lzbench.cpp](https://github.com/inikep/lzbench/blob/4afc06fa5fa98f70f1c4185865349567da5d583f/bench/lzbench.cpp), blob 4025b4450953f1d7326078c7c60d8ff1505c7b9e.

| ID | Lines | Observation | Implication |
| --- | --- | --- | --- |
| L01 | 288–318 | Sorts durations and selects fastest, mean or median; errors represented as zero duration in result | Import estimator and error state explicitly; zero is not a valid speed measurement. |
| L02 | 604–637, 653–671 | Individual timings >=10,000 ns and per-batch mean enter the same vector | Samples have different observation units and correlated membership. Do not treat the vector as independent identically distributed trials; examine effect on median/mean experimentally. |
| L03 | 673–704 | Checks decoded length and memcmp after each batch; clears output and sets error exit status | Strong correctness precedent, but checks final batch output rather than each invocation. Isobar should declare validation cadence and avoid claiming per-call validation if not performed. |
| L04 | 645–646 | Compression-only path skips decode loop | Mark results unverified unless separately validated. |
| L05 | 594–637 | Stops only after both iteration and minimum duration criteria | Adopt minimum work and duration separately from precision-based stopping. |

The mixed sample vector is an estimator concern, not proof that published measurements are wrong. The follow-up report examines the clock branches and representative Zstandard adapters.

## TurboBench

[turbobench.c](https://github.com/powturbo/TurboBench/blob/e15988f037f4255a818a3407bd09a00a2aa6052b/turbobench.c), blob d17811dec1a4f9c37922419b331ca95110ac0a13.

| ID | Lines | Observation | Implication |
| --- | --- | --- | --- |
| T01 | 694–701, 706–733 | Majority comparison of compressed length and encode/decode time; Borda tie-break | Useful ordinal approach but ignores magnitude and depends on candidate pool. |
| T02 | 740–749 | Final comparator returns -1 even for equal IDs with all preceding keys equal | Comparator fails reflexivity; static defect confirmed by direct comparator calls in the follow-up. Effects on a given sort implementation remain untested. |
| T03 | 771–780 | IDs <=2 receive sentinel Copeland values after pairwise and Borda calculation | Such entries still influence other scores; inspect intended control semantics before calling this a defect. |
| T04 | 777–780 | Shared rank requires equality on all three raw metrics even if rankmode excludes a metric | Disabled dimensions can affect tie assignment. The follow-up reproduces this for ratio-only ranking. |
| T05 | 692–698 | Comment says higher tc/td better, implementation compares lower values | Documentation inconsistency; inspect time representation before proposing fix. |
| T06 | 1656–1672 | Ranking is computed on report candidate array | Record candidate-set identity when importing ordinal scores. |

## Remaining reproduction work

Run both full executables on controlled adapters and document how the sampling and validation effects change with batch size and threading. For TurboBench, measure candidate-set sensitivity by adding duplicate or dominated candidates.

## Decision

Do not use external ranking or timing columns as equivalent to native Isobar evidence without explicit estimator, validation and comparability metadata. Keep ordinal scores optional and separate from normalized magnitude scores.
