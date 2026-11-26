# Measurement methodology and validation plan

Status: proposed measurement program. The [experimental evidence report](measurement-evidence.md) records the completed gzip boundary pilot, selected upstream reproductions and subsequent literature review.

## Literature and corpus references

- Kalibera and Jones, *Rigorous benchmarking in reasonable time* (2013), [DOI 10.1145/2464157.2464160](https://doi.org/10.1145/2464157.2464160). Systems experiments require repetition at relevant levels and variation estimates. See the follow-up report for sections reviewed and remaining estimator choices.
- [Canterbury Corpus](https://corpus.canterbury.ac.nz/), official landing page reviewed: established lossless evaluation files and results. Verify per-file provenance, licensing and hashes before redistribution.
- [Silesia Corpus](https://sun.aei.polsl.pl/~sdeor/index.php?page=silesia), candidate corpus; source and licenses not yet reviewed.
- lzbench and TurboBench documentation: see existing-tools.md for snapshots.

## Draft measurement contract (P)

Define independent classes: memory-to-memory codec, stream-to-stream, file-to-file, archive operation. Do not rank across classes. Specify initialization, allocation, finalization and I/O boundaries; match candidate/reference. Declare byte unit (MB=10^6, MiB=2^20), clock source, resolution, warm-up and repetition. Record each observation, execution order and status. Preserve failed observations and reasons. Record hardware, OS, virtualization, build flags, binary digest, codec version/level, threads, dictionary identity, input manifest and seed. Use isolated workers with timeouts and resource bounds; process isolation is not a sandbox. Validate exact decoded bytes and independent decoder interoperability where applicable. Exclude invalid configurations from competitive ranking without hiding failures.

## Experimental program

| ID | Experiment | Acceptance evidence |
| --- | --- | --- |
| E01 | No-op / known-delay adapter | Timing boundaries and resolution demonstrably correct |
| E02 | Candidate/reference reversal and seeded blocks | Order and variation visible in raw trials |
| E03 | Small vs cache-exceeding inputs | Cache regime labeled, no pooled ranking |
| E04 | Cold vs reused contexts | Initialization and allocation policy recorded |
| E05 | Payload vs framed output | Byte counts reconcile with actual artifact sizes |
| E06 | Incorrect decoder, truncated output, timeout | Typed failures; never eligible |
| E07 | Repeated independent worker launches | Within-run and between-run variation reported |
| E08 | Cross-platform probes | Unsupported memory/energy fields explicitly unavailable |
| E09 | Dictionary amortization | Embedded and external dependencies separately accounted |
| E10 | Import external benchmark | Unknown provenance and missing raw trials preserved |

Do not claim statistical significance based on a fixed arbitrary trial count; pilot variability, choose precision goals and stopping policy, report estimator and interval assumptions. Shared CI is suitable for functional conformance; performance comparisons need controlled hardware or explicit noise qualification.

## Corpus plan

Start with deterministic synthetic fixtures (empty, tiny, repeated, structured and seeded pseudorandom bytes) and user-supplied files. Record generator version and seed. Add licensed historical corpora via verified manifests, not unreviewed redistribution. Separate dictionary training and evaluation. Expand to source code, binary/firmware, structured data, media, ML tensors and mixed directories with distinct workload profiles.

## Open questions

Select statistical estimators and hierarchical repetition model after literature review; determine platform-specific CPU/memory/energy probe semantics; define comparable memory measures; specify threat model for third-party adapters; review corpus rights; choose baseline implementations and versioning; exercise complete upstream runners and additional adapter paths. These are prerequisites for normative IBS drafts.

## Executed blocked timing pilot

### Fixed design and estimand

For each platform, 12 fresh Python worker processes ran sequentially against a fixed interpreter and linked zlib build. Each process ran two warm-up blocks with one encode per arm and eight measured blocks with three per arm. Each block used the same deterministic 65,536-byte fixture and compared gzip level 6 with level 1. Encodes used fresh contexts, included finalization and passed whole-artifact reconstruction outside the timed region. There was no cache flush, CPU affinity or thermal control. The fixture digest was `dc40b68e6518351f9510a9a6bb8595ac8562aefbfaf449eb9cb2788fdf62c5d0`.

For each adjacent pair of blocks, a seeded draw selects the first order and the second block reverses it. The seed is `20261126`, and each worker initializes its generator with seed plus worker index. This balances order within a worker and phase. A controlled multiplicative second-position slowdown of 1.5 changed a true ratio of 2.0 to 3.0 under fixed reference-first order. Pairing one block in each order recovered 2.0. The control assumes a stable position effect that multiplies both configurations equally. It does not establish that a real codec has no carryover or position interaction. [Experimental setup bias study](https://web.eecs.umich.edu/~weimerw/2022-481F/readings/producing-wrong-data.pdf).

The primary estimand is the exponential of the expected worker-level mean log elapsed-time ratio, candidate over reference, for this fixture and fixed design. Each log ratio divides comparable batch durations inside the same block. The two-sided nominal 95% interval uses Student's t on the 12 worker means, with 11 degrees of freedom, then exponentiates the endpoints. Fresh processes separate process state. A common host and sequential schedule can still introduce drift or shared noise. [Hierarchical systems benchmarking](https://kar.kent.ac.uk/33611/45/p63-kaliber.pdf) · [Student's t critical values](https://www.itl.nist.gov/div898/handbook/eda/section3/eda3672.htm).

A batch stores the sum of its three disjoint timed encode intervals and total input bytes. The sum excludes pauses between calls and the validation work. Serial throughput is total bytes divided by total elapsed time for each arm, using measured batches once. Warm-ups are excluded. The schedule is fixed before execution with a 10-second wall budget per worker, with no early stopping or precision-driven extension. Any unqualified or missing measured arm suppresses the primary interval and rates for that run. The record and the planned positions remain visible, and a process-level timeout records the worker and planned observations even when no completed worker report is available.

### Local timing observations

Each platform completed all 96 measured blocks and accounted for 18,874,368 input bytes per arm. The Linux VM used QEMU 11.1.3 with HVF acceleration and two virtual CPUs on the same Mac. Its Alpine virt kernel was `6.12.115-0-virt`, SHA-256 `ebf299aa6829b15166008db24b076ac0a8b3e778c72ed66be488742aaf05d741`. The guest used the official Python container image for ARM64 Alpine, manifest `sha256:a9b1e8cb5d25a3c62c0a248913331c9031fc0a45d97975aafe459d41bdfef36d`. The macOS build was CPython 3.13.18 with zlib 1.2.12 on Darwin 27.2.0, and the Linux build was CPython 3.13.13 with zlib 1.3.2. Each environment was analyzed separately. [Alpine virt kernel](https://dl-cdn.alpinelinux.org/alpine/v3.22/releases/aarch64/netboot/) · [Official Python image](https://hub.docker.com/_/python).

| Platform | Candidate/reference time, nominal interval | Reference/candidate serial MB/s | Between-worker SD of log ratio | Median within-worker SD of log ratio |
| --- | --- | --- | ---: | ---: |
| macOS ARM64 | 1.5316 [1.5039, 1.5598] | 506.50 / 330.68 | 0.0287 | 0.0375 |
| Linux ARM64 VM | 1.3242 [1.3172, 1.3312] | 339.51 / 256.41 | 0.0084 | 0.0194 |

| Platform | Clock | Reported resolution | Back-to-back clock minimum / median of worker medians | Warm-up → measured median time per encode, reference / candidate |
| --- | --- | ---: | ---: | --- |
| macOS ARM64 | `mach_absolute_time` | 41.67 ns | 0 / 30.75 ns | 175.46 → 127.58 µs / 231.27 → 196.10 µs |
| Linux ARM64 VM | `CLOCK_MONOTONIC` | 1 ns | 125 / 167 ns | 238.90 → 193.56 µs / 309.46 → 255.14 µs |

The clock probe used 128 back-to-back timestamp pairs per worker, before warm-up. It includes call overhead and quantization. No time was subtracted. Measured medians use each batch's mean encode duration. Peak memory and energy probes are unsupported in this pilot. These results are workload-specific observations. Differences between the native macOS run and Linux guest combine platform, runtime and build effects. [macOS raw evidence](experiments/blocked-macos.jsonl) · [Linux VM raw evidence](experiments/blocked-linux-qemu.jsonl).

### Seeded coverage experiment

The simulation imposed a candidate/reference ratio of 1.08, a second-position log offset of ±0.18 and Gaussian within-worker log noise with SD 0.06. It compared the worker-level t interval with a t interval that treats 48 averages of counterbalanced block pairs as independent. Each scenario ran 5,000 repetitions using seed `20261126`.

| Worker-level effect | Worker-level coverage | Unclustered coverage |
| --- | ---: | ---: |
| Homogeneous | 94.86% | 95.00% |
| Gaussian heterogeneity, SD 0.16 | 95.60% | 66.98% |
| Centered exponential heterogeneity, SD 0.16 | 90.22% | 64.28% |

The worker-level interval was close to nominal under the Gaussian scenarios and undercovered under the skewed worker model. The test therefore supports retaining worker clustering and limits any nominal coverage claim to a reviewed model. Production rankings need calibration across additional workloads and operating conditions, along with a decision on the interval procedure. [Reproduction commands](experiments/README.md#run-the-blocked-timing-pilot).
