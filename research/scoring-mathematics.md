# Scoring mathematics: initial derivations

Status: mathematical analysis (M), draft design proposals (P), not an accepted standard.

## Historical Weissman

W = alpha * (R/R0) * ln(T0)/ln(T).

With equal ratios, alpha=1, reference 10 s and candidate 5 s, W is approximately 1.431 in seconds and 1.081 in milliseconds. Scaling times by k adds ln(k) to both logarithms, generally changing their quotient. With reference 0.5 s and candidate 0.25 s the score is 0.5, despite faster encoding. T=1 is singular; nonpositive times are outside the logarithm domain. Changing logarithm base alone cancels in the quotient. These facts follow algebraically; they do not depend on a specific implementation.

Compatibility profile must pin time unit, alpha, reference, duration aggregation and valid domain. Historical numeric behavior should be labeled, not silently repaired. Never use it as sole eligibility or ranking criterion.

## Dimensionless composite (P)

S = 100 * exp(sum(w_i * ln(q_i))), with nonnegative weights totaling one. For higher-is-better positive metrics q=x/x0; for lower-is-better q=x0/x. Exclude zero-weight dimensions from evaluation. Require finite positive inputs for included metrics, matching units and measurement scopes. A missing required value yields unavailable, not zero. Reference scores 100. Equal unit conversions cancel. Score is monotonically increasing in normalized performance for positive weights, but weight choices remain subjective. Guard overflow/underflow, nonfinite values and invalid weights. Version profiles and baseline identity.

## Aggregation

For files with original sizes 1 and 100 MB and outputs 0.1 and 50 MB, arithmetic mean of ratios is 6; total-byte ratio is 101/50.1 = approximately 2.016. Both answer different questions. Define aggregation before comparing. Throughput over serial work is total bytes / total elapsed time, not an unweighted mean of rates. Empty-input ratio undefined; retain bytes and status, do not substitute infinity. Report elapsed clock resolution and zero observations explicitly.

## Pareto

A dominates B iff all selected comparable dimensions are no worse and at least one is strictly better. Equal vectors are ties. Missing dimensions invalidate comparisons within that profile. Test minimize/maximize directions, duplicates and permutation invariance. Uncertainty-aware dominance is research, not yet a defined algorithm.

## Conformance vectors (proposed)

| Case | Expected |
| --- | --- |
| Composite all ratios 1 | 100 |
| One metric doubled, weight 1 | 200 |
| Two ratios 2 and 0.5, weights 0.5 each | 100 |
| Convert both times seconds to ns | Same composite |
| Required missing/zero/NaN | Unavailable or typed validation error |
| Negative weight or sum not one | Reject |
| Historical T=1 | Undefined |
| Pareto equal points | Neither dominates |
| Pareto tradeoff | Neither dominates |

Source for historical implementation: [weissman.py](https://github.com/lorenzosaino/weissman-score/blob/f28197250f3dc7c42373665218892d21592de8f3/weissman.py#L130-L139); see existing-tools.md for inspected blob. Numerical examples are derived, not measured codec results.

[Executable conformance vectors](../conformance/vectors.json) cover these cases, exact dominance with disabled dimensions, permutation invariance, and finite log-space cancellation when individual normalized ratios would overflow or underflow.
