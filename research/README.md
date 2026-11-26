# Research ledger

Updated 2026-11-26. This ledger indexes source reviews, reproductions, measured observations and design proposals.

- [Existing tools and source review](existing-tools.md)
- [Runner and ranking source review](runner-source-review.md)
- [Measurement and conformance evidence](measurement-evidence.md)
- [Pinned upstream aggregate imports](measurement-evidence.md#aggregate-report-imports)
- [Executable upstream fixtures and raw observations](experiments/README.md)
- [Experimental conformance vectors and evidence schemas](../conformance/README.md)
- [Architecture decisions under review](../specifications/measurement-decisions.md)
- [Scoring mathematics](scoring-mathematics.md)
- [Experimental methodology and validation plan](measurement-methodology.md)
- [Blocked timing pilot and interval coverage](measurement-methodology.md#executed-blocked-timing-pilot)

Evidence levels: S = inspected source; D = inspected documentation; M = mathematical derivation; R = reproduced behavior in an executable fixture; E = measured observation; P = proposed requirement; U = unresolved. Static source findings are not runtime reproductions. Record source path, revision or blob hash, access date, evidence level and reproduction status for new findings. Do not infer absence of a feature from documentation silence.

Pinned reproductions cover selected Weissman, lzbench and TurboBench paths. The gzip pilot records nested timing boundaries. See the evidence report for environments, controls and limits. Open work includes full-runner trials, cross-platform instrumentation, imported provenance and statistical interval calibration.
