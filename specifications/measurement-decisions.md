# Measurement architecture decisions

Status: proposals for review alongside draft IBS-0001–0004. The [evidence report](../research/measurement-evidence.md) records the measured and reproduced findings.

## Match work at the operation boundary

A measurement profile defines starting state and completion. For the first in-memory codec profile, begin with input loaded and a declared context state. End encoding once the complete format artifact is available. End decoding after the whole artifact has been consumed under the declared profile. Output buffer allocation, codec initialization, finalization and copies each need a documented boundary. File I/O and subprocess startup belong to a separate end-to-end profile. Use separate comparison classes when initialization or timed work cannot be matched.

Tradeoff: one invocation per observation gives straightforward accounting and exact correctness checks. Timer overhead becomes comparatively large for tiny workloads. Permit explicit batching when the runner can preserve total work and each invocation's validation evidence. An interval below the usable resolution should stay unranked until a separately specified timing profile can measure it.

## Separate measurement from eligibility

An operation can complete with a valid clock reading and produce incorrect output. Retain that observation and the separate validation outcome. Competitive eligibility requires exact reconstructed bytes, artifact completion and declared coverage of each timed invocation. The validator runs outside the interval unless the measurement profile explicitly times it. A batch-end check alone covers only the final retained artifact. To qualify a larger batch, retain or verify each output without silently expanding the respective profile's timing boundary.

An initial harness should support trusted local adapters. Use a fresh exclusive scratch directory, explicit argv, bounded transport and an owned worker lifetime. Production containment, archive extraction and untrusted adapter execution require a separate design review. Record timeout and early exit independently of bytes emitted.

## Preserve the experimental unit and provenance

Keep worker, block and observed sequence. Use a complete block of comparable trials for a within-block contrast, and preserve process nesting when workers contribute multiple blocks. Store aggregate total bytes and elapsed time alongside the observation references. Imported summaries carry the upstream estimator and coverage when known, and remain aggregate-only when source rows do not provide reconstructible trials.

Use a versioned schema for record validation and a separate verifier for reference integrity and eligibility. The experimental format uses opaque IDs and exact numeric counters. A future decision must define canonical run identity, artifact hashing, migration and signatures before they are relied on for public verification.

## Report metrics and ranking as separate layers

Publish comparable raw metrics and failures first. Exact Pareto dominance and supplemental composite or ordinal scores can be derived under a specific profile. Disabled dimensions have no effect on equality or dominance. A secondary display order is permissible for ties. If an ordinal score depends on the competitor pool, the report must identify that pool and tie policy. Preserve paired observations for uncertainty rather than treating rounded report values as raw inputs.
