"""Experiment 012: Circuit breaker window-size sensitivity sweep."""

from __future__ import annotations

from slimybug.experiment import Experiment, ExperimentMetadata

METADATA = ExperimentMetadata(
    id="012",
    slug="breaker-window-sensitivity",
    title="Circuit breaker window-size sensitivity sweep",
    status="closed",
    question=(
        "Experiment 003 found a fixed 20-request sliding window (fixed "
        "threshold 0.5, fixed cooldown 2.0s) substantially reduces "
        "client-visible error above the collapse boundary. As the window "
        "shrinks toward more reactive/noisier or grows toward more "
        "stable/slower, does error rate change continuously, or is there "
        "a sharp cliff at some window size?"
    ),
    hypothesis=(
        "If error rate moves smoothly across the window-size sweep, the "
        "breaker's reactivity has a graded effect and no single window "
        "size is privileged. If instead there is a sharp knee, only "
        "sufficiently large (stable) windows meaningfully help and small "
        "windows behave like no breaker at all."
    ),
    primary_variable="breaker_window_size",
    fixed_params={
        "injected_latency_ms": 400,
        "retry_policy": "none",
        "breaker_enabled": True,
    },
)

# Same shape as 011: one saturated RPS point (the sweep), plus RPS 12 kept
# only as the standard below-boundary false-positive check every prior
# sensitivity-sweep experiment has run. RPS 16 matches 003 and 011's own
# choice -- 003 already showed it clearly saturated with the breaker on
# (58.9% error at the default window=20).
WINDOW_LATENCY_MS = METADATA.fixed_params["injected_latency_ms"]
WINDOW_RPS = [12, 16]

# Doubling steps around the current hardcoded default (20), which is kept
# in the sweep as the tie-back point to Experiment 003's own result rather
# than reused from its historical data (011 applied this same discipline
# against 007). 5 is the lower anchor: below it the sliding window barely
# outlives a handful of requests, functionally close to reacting on
# single-request noise. 80 is the upper anchor: at RPS 16 with the breaker
# initially closed, filling an 80-request window takes ~5s, comfortably
# inside warmup -- large enough to test "does a slower/stabler window
# plateau" without risking the window never filling within the run.
WINDOW_SIZES = [5, 10, 20, 40, 80]


class ExperimentBreakerWindowSensitivity(Experiment):
    metadata = METADATA

    def matrix(self) -> list[dict]:
        configs = []
        for rps in WINDOW_RPS:
            for window_size in WINDOW_SIZES:
                configs.append(
                    {
                        "rps": rps,
                        "latency_ms": WINDOW_LATENCY_MS,
                        "retry_policy": "none",
                        "breaker_enabled": True,
                        "breaker_window_size": window_size,
                    }
                )
        return configs

    def analyze(self, runs: list[dict]) -> dict:
        from slimybug.analysis import annotate_saturation, first_saturation_points

        annotate_saturation(runs)
        collapse_points = first_saturation_points(runs, group_key="breaker_window_size")

        # Validity check, mirroring 011: at RPS 12, error_rate should be
        # ~0% for every window size. NOTE: deliberately not using the
        # generic `saturated` flag here, even though annotate_saturation()
        # already ran above -- pool_size defaults to 10, and at RPS 12
        # (400ms latency) the pool's active-connection count legitimately
        # touches 10 by momentary variance even at 0% error, so `saturated`
        # is True at RPS 12 for every run regardless of window size (the
        # same artifact already present in Experiment 011's own summary.csv
        # -- not new to this experiment). error_rate is the actual
        # client-visible signal the false-positive question is about.
        rps12_by_window = {
            r["breaker_window_size"]: r.get("error_rate")
            for r in runs
            if r.get("rps") == 12
        }

        # The actual sweep result: error rate and downstream load by
        # window size at the primary RPS 16 point, sorted so continuity
        # vs. a cliff is directly readable.
        sweep_rps16 = sorted(
            (
                {
                    "breaker_window_size": r.get("breaker_window_size"),
                    "error_rate": r.get("error_rate"),
                    "amplification_factor": r.get("amplification_factor"),
                    "probe_success_rate": r.get("probe_success_rate"),
                    "b_received_count": r.get("b_received_count"),
                }
                for r in runs
                if r.get("rps") == 16
            ),
            key=lambda row: row["breaker_window_size"],
        )

        return {
            "collapse_points": collapse_points,
            "false_positive_check_rps12": rps12_by_window,
            "window_sweep_rps16": sweep_rps16,
        }


experiment = ExperimentBreakerWindowSensitivity()
