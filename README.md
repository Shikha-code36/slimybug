# SlimyBug

Reproducible overload experimentation for distributed systems: find where a
system stops degrading gracefully, and why.

SlimyBug injects real failures into small, real systems, observes how those
failures propagate, and explains the mechanism behind them.

```
Load Generator (k6) -> Service A (FastAPI, retry client + circuit breaker) -> Service B (FastAPI + asyncpg) -> Toxiproxy -> PostgreSQL
```

Service A is a thin HTTP client with a configurable retry policy (none /
immediate / retry-with-backoff / exponential backoff with full jitter) and a
circuit breaker that can wrap the call to Service B. Service B owns the
bounded connection pool and is the one instrumented behind Toxiproxy's
injected network latency, on a real PostgreSQL database. Every component in
the request path is real.

## Current experiments

| Experiment | Status | Key finding |
|---|---|---|
| [001 — collapse boundary](experiments/001-collapse-boundary/README.md) | closed | Injected DB latency propagates harmlessly until the connection pool saturates; the latency needed to trigger saturation drops sharply as offered load rises. |
| [002 — retry amplification](experiments/002-retry-amplification/README.md) | closed | Retries roughly double load on an already-saturated dependency without increasing completed throughput, and don't measurably shift the collapse boundary. |
| [003 — circuit breaker](experiments/003-circuit-breaker/README.md) | closed | A minimal breaker cuts load reaching the saturated dependency by up to ~44%, cuts client-visible errors by up to ~61 points, and every recovery probe succeeded — confirming the collapse is a queueing effect, not a hard failure. |
| [004 — jitter](experiments/004-jitter/README.md) | closed | Full jitter measurably desynchronizes retry arrivals at every saturated RPS tested, but that desynchronization doesn't move amplification, error rate, or completed throughput — a fixed-capacity pool, not burst-induced overflow, remains the dominant constraint once saturated. |
| [005 — connection pool capacity](experiments/005-connection-pool-capacity/README.md) | closed | The collapse boundary scales exactly linearly with pool size (10→20→40, RPS 14→28→56, all at a 1.4 RPS-per-connection ratio) with no curvature — a bigger pool moves the collapse point but doesn't soften it: client-visible success still collapses to near 0% at the new boundary, while Service B keeps completing about the same amount of real work. |
| [006 — server-side admission control](experiments/006-admission-control/README.md) | closed | Gating on the pool's own state before ever calling acquire() turns the binary ~100% collapse into a load-proportional curve (16.67%→22.99%→33.31% error at RPS 14/16/18) with every admitted request succeeding — roughly half the error rate of Experiment 003's client-side breaker at the same RPS points. |
| [007 — EWMA admission signal](experiments/007-ewma-admission-signal/README.md) | closed | A trailing EWMA of pool utilization (half-life 2.0s) behaves far closer to no admission control at all than to an instantaneous signal at the same location — errors come from expensive pool timeouts, not cheap rejections — showing information freshness is a causally significant, independent factor in arbitration, not just decision location. |
| [008 — graduated admission onset](experiments/008-graduated-admission-onset/README.md) | closed (inconclusive on primary hypothesis) | The graduated rule's implementation was verified correct (Gate 1 passed), but the preregistered onset (`u_low=0.8`) admitted rejections below the established collapse boundary, confounding rule-shape with earlier onset — a falsified design assumption, not a bug, so whether decision-rule continuity matters on its own remains open. |
| [009 — bounded admission deferral](experiments/009-bounded-grace-period/README.md) | closed | A single 20ms bounded postponement of a provisional reject rescues 77% of contested requests near the collapse boundary (RPS 14: 16.7%→13.1% error), but that benefit collapses to near-zero further into sustained overload (RPS 16-18) — the value of an arbitration mechanism depends on where the system sits relative to the collapse boundary, at a precisely quantified latency cost (~1 grace interval, paid by rescued and rejected requests alike). |
| [010 — RPS16's bimodal rescue rate](experiments/010-rps16-bimodal-mechanism/README.md) | closed | Time-resolved tracing (new `t_ns` instrumentation) explains the bimodal variance R002 found at RPS16: it's not two discrete regimes but a rare, self-locking connection-pool saturation event. Most runs show the pool flickering in and out of capacity in short streaks (3-5 decisions, self-resolving); occasionally a run's pool saturates once, early, and never recovers for the rest of the run — one continuous 2000+ decision streak instead of 150+ short ones. Consistent with RPS16 sitting close enough to critical pool utilization for timing jitter to occasionally tip a run into a self-reinforcing saturated state. |
| [011 — EWMA half-life sensitivity sweep](experiments/011-half-life-sensitivity/README.md) | closed | Sweeping the EWMA admission signal's half-life (0.06s–4.0s, log-spaced) at RPS 16 shows error rate rising smoothly and continuously from instantaneous-like (23–26%) to off-like (99.93%, exactly matching no admission control) — no sharp cliff between any adjacent pair of half-lives. The steepest region sits between 0.25s and 1.0s, between the interarrival timescale and the measured request-service-time. Pool timeouts appear and grow monotonically with half-life (0→316), the same mechanism Experiment 007 found, now shown to scale continuously with the degree of signal lag rather than being specific to one tested value. |
| [012 — circuit breaker window-size sensitivity sweep](experiments/012-breaker-window-sensitivity/README.md) | closed | Sweeping the breaker's sliding-window size (5–80 requests) at RPS 16 shows amplification (downstream load reaching Service B) rising cleanly and monotonically (0.584→0.774) as the window grows and reacts more slowly — but client-visible error rate does not move monotonically at single-run resolution, leaving continuity vs. a cliff unresolved without replication. RPS 12 stayed at exactly 0% error for every window size, no false positives anywhere in the sweep. |

## Reference-grade evidence

Experiments under `experiments/` are **research-grade**: one causal
question each, single run per condition, optimized for learning quickly.
`reference/` holds a different evidence standard — **reference-grade**:
no new causal question, replicated runs, reported variance, aimed at an
external, skeptical reader rather than a returning collaborator. A
reference-grade entry validates a specific claim an experiment already
made; it never reopens or revises the original, which stays exactly as
published.

| Reference | Status | Key finding |
|---|---|---|
| [R001 — pool-capacity validation](reference/R001-pool-capacity-validation/README.md) | closed | Replicating the six operating points behind Experiment 005's linearity claim (5 independent runs each, 30 total, run order randomized) found the behavior remarkably stable — 0.00% error across all 15 clean-edge runs, near-total error with <0.1 percentage-point spread across all 15 collapse-point runs. This doesn't re-derive 005's boundary or ratios (the same points were deliberately re-measured, not re-searched); it shows the behavior at those points isn't an artifact of one lucky run. |
| [R002 — bounded-deferral validation](reference/R002-bounded-deferral-validation/README.md) | closed | Replicated Experiment 009's regime-dependent claim across all four RPS points (both compared conditions, 50 runs total). RPS14's large rescue effect (16.8%→13.1% error, 76% rescue rate) and RPS18's near-zero benefit both replicate closely. RPS16 — escalated to N=10 after unusually wide variance — turned out bimodal rather than intermediate: the system intermittently flips between two distinct deferred-decision behaviors (~20% vs ~62% rescue rate) while landing on nearly identical aggregate error either way, a mechanism invisible to 009's single-run design. |
| [R003 — half-life sensitivity validation](reference/R003-half-life-sensitivity-validation/README.md) | closed | Replicated Experiment 011's continuous-degradation claim across all eight RPS16 cells (40 runs, N=5 each). Every cell's spread was tight — no bimodal split like Experiment 010 found at this same RPS point — and adjacent-cell gaps stayed far larger than within-cell variance, confirming the smooth, no-cliff shape. One correction replication revealed: 011's single run happened to measure half_life=4.0s as identical to `off` (99.93% both); five replicates instead show a small, tight, reproducible ~4.8-point gap (95.11% vs 99.90%) — the curve approaches `off` asymptotically rather than exactly reaching it within the range tested. |

## Running the stack

```
docker compose up -d --build postgres toxiproxy toxiproxy-init service-b service-a
```

Wait for `service-a` to report healthy, then confirm it can reach the
database through the proxy:

```
curl http://localhost:8000/healthz
curl http://localhost:8000/work?id=1
```

## Running an experiment

Every experiment lives under `experiments/<id>-<slug>/` and declares its own
run matrix in `experiment.py` (see `slimybug/experiment.py` for the model).
To run one end to end:

```
python scripts/run_experiment.py 005
python scripts/analyze_results.py 005 --csv
```

`run_experiment.py` loads the experiment's `matrix()` and executes each run
via the shared `Runner`, writing artifacts to
`experiments/<id>-<slug>/runs/<run_id>/`:

- `metadata.json` — run configuration (rps, injected latency, retry policy, breaker enabled/window size, pool size, timeout)
- `results.json` — load generator summary plus `service_a`/`service_b`/`breaker`
  metric summaries and the derived `amplification_factor` / `retry_rate` /
  `retry_success_rate` / `probe_success_rate`
- `proxy_state.json` — the Toxiproxy configuration used
- `raw_app_samples.jsonl` — per-second snapshots from both services plus the breaker
- `arrival_trace.csv` — one arrival timestamp per request reaching Service B, only when the experiment enables arrival tracing
- `loadgen/summary.json`, `loadgen/raw.jsonl` — k6 summary and per-request events

`analyze_results.py` aggregates those runs into `experiments/<id>-<slug>/summary.csv`
and reports each experiment's saturation/collapse analysis (see
`slimybug/analysis.py`): a run is flagged saturated when its error rate is
above 0%, any pool-acquisition timeout occurred on Service B, or Service B's
pool ran at its configured max size. Injected latency alone always makes
requests slower — that's just propagation, not saturation, so it isn't used
as a signal on its own.

## Browsing Findings

Every closed experiment and reference-grade validation above has a
structured `finding.json` alongside its narrative `README.md` (schema in
`slimybug/finding.py`):

```
python scripts/findings.py list
python scripts/findings.py show 011
python scripts/findings.py compare 011 R003
```

`compare` first checks whether two Findings are even meaningfully
comparable — connected by a `refines`/`supersedes` relationship, or
sweeping the same variable with overlapping fixed parameters — before
diffing their status, evidence, and validity checks.

## Starting a new experiment

```
python scripts/new_experiment.py 006 my-slug \
    --title "..." --question "..." --hypothesis "..." --variable my_variable
```

Scaffolds `experiments/006-my-slug/` with an `experiment.py` stub (fill in
`matrix()`) and a `README.md` skeleton. See `experiments/005-connection-pool-capacity/`
for a complete example of the model in use.

For guidance on picking a question, choosing an evidence grade, and what
review checks for, see [CONTRIBUTING.md](CONTRIBUTING.md).
