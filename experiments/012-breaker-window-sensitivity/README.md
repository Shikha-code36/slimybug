# Experiment 012 -- Circuit breaker window-size sensitivity sweep

**Status.** closed

**Why this experiment.** Experiment 003 found a circuit breaker with a
fixed 20-request sliding window (fixed failure-rate threshold 0.5, fixed
2.0s cooldown) substantially reduces both downstream load and
client-visible error above the collapse boundary. 20 was a hardcoded
constant, never swept. This experiment also serves a second, architectural
purpose: RFC 0001 (Stage 3's methodology-platform design) named Circuit
Breaker as the concrete "second mechanism template" needed to test whether
Stage 3's Finding schema and grading concepts generalize beyond admission
control. Grading a Finding for 003 itself already grounded part of that
question (see `experiments/003-circuit-breaker/finding.json`); this
experiment supplies the piece 003 couldn't -- a *continuous* tunable
parameter, the same shape as Experiment 011's EWMA half-life sweep,
instead of 003's binary breaker-on/off comparison.

**Question.** As the breaker's sliding window shrinks toward more
reactive/noisier or grows toward more stable/slower, does error rate
change continuously, or is there a sharp cliff at some window size?

**Hypothesis.** If error rate moves smoothly across the window-size
sweep, the breaker's reactivity has a graded effect and no single window
size is privileged. If instead there is a sharp knee, only sufficiently
large (stable) windows meaningfully help and small windows behave like no
breaker at all.

**Primary variable.** `breaker_window_size`, swept at `5, 10, 20, 40, 80`
-- doubling steps around the current hardcoded default (20), kept in the
sweep as the tie-back point to Experiment 003's own result rather than
reused from its historical data (the same discipline 011 applied against
007). 5 is the lower anchor: below it the sliding window barely outlives
a handful of requests. 80 is the upper anchor: at RPS 16 with the breaker
initially closed, filling an 80-request window takes ~5s, comfortably
inside warmup.

**Fixed parameters.** `injected_latency_ms=400`, `retry_policy=none`,
`breaker_enabled=true`, failure-rate threshold and cooldown left at their
existing hardcoded values (0.5, 2.0s) -- one-variable-per-experiment
discipline, consistent with 007/009/011.

**Why a single saturated RPS, not a sweep.** Same reasoning as 011:
sweeping RPS and window size together would conflate two variables. RPS
16 is the primary point, matching 003 and 011's own choice -- 003 already
showed it clearly saturated with the breaker on (58.9% error at the
default window=20). RPS 12 is kept only as the standard below-boundary
validity check.

**Architectural note: making this parameter sweepable required real code
changes, not just a new experiment file.** `breaker_window_size` did not
previously exist as a runtime-configurable knob -- `WINDOW_SIZE` was a
hardcoded module constant in `services/service-a/app/breaker.py`. Wiring
it through required: an env var read in `breaker.py`; echoing it back via
`/internal/config` (`client.py`); a new `set_experiment_config()`
parameter, `declared_keys` entry, and `describe_run()` suffix in
`slimybug/reference_adapter.py`; and a new field in `slimybug/runner.py`'s
`metadata.json` construction and `slimybug/aggregate.py`'s
`SUMMARY_FIELDNAMES`. All changes verified bit-for-bit non-disruptive to
every existing closed experiment via `scripts/verify_replay.py` (the new
field is `null` for historical runs, nothing else changed), then
confirmed live against real Docker containers before any real run
executed.

## Finding

**Validity check passed cleanly.** At RPS 12, error rate was exactly
0.00% at every window size (5, 10, 20, 40, 80) -- no false positives
below the collapse boundary anywhere in the swept range. Note: the
generic `saturated` flag (`slimybug/analysis.py`) reads `True` at RPS 12
regardless of window size, because `pool_size` defaults to 10 and the
pool's active-connection count legitimately touches 10 by momentary
variance even at 0% error -- the same artifact already present,
unremarked, in Experiment 011's own summary.csv. `error_rate` is the
actual client-visible signal this check is about; `saturated` was not
used for it.

**RPS 16 sweep, sorted by window size:**

| Window size | Error rate | Amplification | Requests reaching B | Pool timeouts |
|---|---|---|---|---|
| 5 | 60.64% | 0.584 | 832 | 0 |
| 10 | 60.50% | 0.598 | 830 | 0 |
| 20 | 58.82% | 0.639 | 907 | 0 |
| 40 | 55.73% | 0.737 | 1042 | 16 |
| 80 | 62.18% | 0.774 | 1086 | 41 |

Every half-open probe succeeded at every window size (100% probe success
rate throughout) -- consistent with 003's read that this collapse is a
queueing effect, not a hard failure, independent of window size.

**Amplification (downstream load reaching Service B) rises with window
size, cleanly.** 0.584 -> 0.774 as window grows 5 -> 80, with requests
reaching B climbing 832 -> 1086. This is mechanistically direct: a larger
window takes longer to accumulate enough forwarded failures to cross the
0.5 failure-rate threshold, so more traffic gets through before the
breaker first opens. Pool timeouts appearing only at window=40/80 (16 and
41 respectively, versus 0 at windows 5/10/20) is the same signature 011
found for stale EWMA signals: once enough load reaches the pool before
the mechanism reacts, errors stop being cheap short-circuit rejections
and start being expensive pool timeouts.

**Error rate does not move monotonically with window size, and this
experiment cannot say whether that's real or noise.** 58.82% -> 55.73%
(window 20 -> 40) is a *decrease* even though amplification and pool
timeouts both increase over the same step; 55.73% -> 62.18% (window 40 ->
80) then reverses again. Unlike 011's half-life sweep -- where the
error-rate curve was smooth enough at n=1 to support a continuity claim
on its own -- this sweep's error-rate curve is not monotonic at n=1, and
with no replication, a 5-8 percentage-point non-monotonic wiggle is
indistinguishable from single-run noise. **This experiment does not
establish continuity or a cliff for error rate as a function of window
size** -- only that amplification (downstream load) responds to window
size cleanly and monotonically, while client-visible error rate's
response is not resolved by this data.

**What this means for RFC 0001 Tier 1, item 2 (grading generalization).**
Combined with `experiments/003-circuit-breaker/finding.json`: the
Finding schema, validity-check pattern, and shared saturation heuristic
all continue to hold for a second mechanism family. What this experiment
adds beyond 003 is the first real test of a *continuous* breaker
parameter against a Gate1/Gate2-shaped question -- and the honest result
is inconclusive at single-run resolution, the same evidentiary gap 011
had before R003 replicated it. This is not a generalization failure; it's
the same "single run isn't enough to distinguish a smooth curve from
noise" limitation the project has already named and already has a
methodology for (reference-grade replication), just newly demonstrated
outside admission control.

**Documented limitations.** Single run per condition -- no
reference-grade replication exists for this claim. A single saturated RPS
point (16) was tested. Failure-rate threshold (0.5) and cooldown (2.0s)
were held fixed; whether window size interacts with either is untested.
If a future replication is warranted to resolve the error-rate
non-monotonicity specifically, it would follow the R001/R003 precedent --
not attempted here.
