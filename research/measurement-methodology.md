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
