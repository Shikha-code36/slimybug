"""ReferenceAdapter: this project's own Protocol implementation (RFC 0001
SS5) -- Toxiproxy + Service A/B + k6, wired exactly as slimybug/runner.py
drove them before this split. A different adapter would replace this
module entirely without touching slimybug/runner.py, which knows nothing
about Service A/B, Toxiproxy, or k6.

Split out of slimybug/runner.py in RFC 0001 SS9 Phase A milestone 2, with
no behavior change from milestone 1's inline version -- see
scripts/verify_replay.py for how that equivalence is checked, and a live
smoke-test run (RFC migration notes) for the milestone 1 confirmation this
split doesn't undo. Phase B (milestone 3) added declared_keys + fail-fast
validation and the measure_window/timestamp checks; the run_id design
decision (SS9 Phase B) made this adapter own its own warmup/measure/
cooldown/pool-size defaults and run_id naming (describe_run) instead of
Runner supplying them.

Not yet fully decoupled: Runner.run() still directly calls
http_get_json/http_json against SERVICE_A_URL/SERVICE_B_URL for a few
things migration hasn't covered (fetching a_config/b_config for
metadata.json, arrival_trace and admission_decision_trace reset/read) --
those constants are still defined in slimybug/runner.py, imported here
rather than duplicated. This is a known, RFC-acknowledged gap (SS10 Tier 1:
how much of today's logic actually generalizes needs a 2nd adapter to
know), not something this migration claims to have resolved.
"""

from __future__ import annotations

import json
import subprocess
import sys

from slimybug.runner import (
    PROXY_NAME,
    REPO_ROOT,
    SERVICE_A_URL,
    SERVICE_B_URL,
    TOXIC_NAME,
    TOXIPROXY_URL,
    Protocol,
    _select_window,
    http_get_json,
    http_json,
)

# Adapter-owned defaults -- moved from slimybug/runner.py when Runner.run()
# stopped supplying them itself (RFC 0001 SS9 Phase B, "Runner is
# mechanism-blind"): configure()/drive_load() apply these when the caller's
# config dict omits the corresponding key.
DEFAULT_WARMUP_S = 30
DEFAULT_MEASURE_S = 90
DEFAULT_COOLDOWN_S = 15
DEFAULT_POOL_SIZE = 10  # matches docker-compose.yml's POOL_MAX_SIZE default


def set_toxic_latency(latency_ms: int) -> dict:
    """Replace any existing latency toxic with one matching latency_ms.

    latency_ms == 0 means "no toxic configured" (clean baseline).
    """
    http_json("DELETE", f"{TOXIPROXY_URL}/proxies/{PROXY_NAME}/toxics/{TOXIC_NAME}")

    if latency_ms > 0:
        http_json(
            "POST",
            f"{TOXIPROXY_URL}/proxies/{PROXY_NAME}/toxics",
            {
                "name": TOXIC_NAME,
                "type": "latency",
                "stream": "downstream",
                "attributes": {"latency": latency_ms, "jitter": 0},
            },
        )

    return {"active": latency_ms > 0, "configured_latency_ms": latency_ms}


def set_experiment_config(
    retry_policy: str,
    breaker_enabled: bool,
    breaker_window_size: int = 20,
    enable_arrival_trace: bool = False,
    pool_size: int = DEFAULT_POOL_SIZE,
    admission_control_enabled: bool = False,
    admission_control_mode: str = "instantaneous",
    admission_ewma_half_life_s: float = 2.0,
    admission_u_low: float = 0.8,
    admission_grace_ms: float = 20.0,
    enable_admission_decision_trace: bool = False,
) -> None:
    """Recreate Service A / Service B with the given config if needed.

    Written to a .env file (which every `docker compose` invocation reads
    automatically) rather than passed as a one-off subprocess env var --
    run_k6() below also shells out to `docker compose run loadgen`, and
    since loadgen depends_on service-a, compose re-resolves the whole
    project against whatever environment it sees and will silently recreate
    service-a back to the default config if these vars aren't set there too.
    A .env file keeps every subsequent compose call consistent.

    docker compose only recreates a container when its resolved config
    changed, so passing both services to `up` is a no-op for whichever one
    didn't change.
    """
    current_a = http_get_json(f"{SERVICE_A_URL}/internal/config")
    current_b = http_get_json(f"{SERVICE_B_URL}/internal/config")
    current_b_trace = http_get_json(f"{SERVICE_B_URL}/internal/arrival_trace")
    current_b_decision_trace = http_get_json(f"{SERVICE_B_URL}/internal/admission_decision_trace")
    if (
        current_a.get("retry_policy") == retry_policy
        and current_a.get("breaker_enabled") == breaker_enabled
        and current_a.get("breaker_window_size") == breaker_window_size
        and current_b_trace.get("enabled") == enable_arrival_trace
        and current_b.get("pool_max_size") == pool_size
        and current_b.get("admission_control_enabled") == admission_control_enabled
        and current_b.get("admission_control_mode") == admission_control_mode
        and current_b.get("admission_ewma_half_life_s") == admission_ewma_half_life_s
        and current_b.get("admission_u_low") == admission_u_low
        and current_b.get("admission_grace_ms") == admission_grace_ms
        and current_b_decision_trace.get("enabled") == enable_admission_decision_trace
    ):
        return

    (REPO_ROOT / ".env").write_text(
        f"RETRY_POLICY={retry_policy}\n"
        f"BREAKER_ENABLED={'true' if breaker_enabled else 'false'}\n"
        f"BREAKER_WINDOW_SIZE={breaker_window_size}\n"
        f"ENABLE_ARRIVAL_TRACE={'true' if enable_arrival_trace else 'false'}\n"
        f"POOL_MAX_SIZE={pool_size}\n"
        f"ADMISSION_CONTROL_ENABLED={'true' if admission_control_enabled else 'false'}\n"
        f"ADMISSION_CONTROL_MODE={admission_control_mode}\n"
        f"ADMISSION_EWMA_HALF_LIFE_S={admission_ewma_half_life_s}\n"
        f"ADMISSION_U_LOW={admission_u_low}\n"
        f"ADMISSION_GRACE_MS={admission_grace_ms}\n"
        f"ENABLE_ADMISSION_DECISION_TRACE={'true' if enable_admission_decision_trace else 'false'}\n"
    )
    subprocess.run(
        ["docker", "compose", "up", "-d", "--wait", "service-a", "service-b"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )


def _poll_one(url: str) -> dict:
    try:
        return http_get_json(url, timeout=2.0)
    except Exception as exc:
        return {"error": str(exc)}


def run_k6(run_dir, target_url: str, rps: int, warmup_s: int, measure_s: int, cooldown_s: int):
    results_dir = run_dir / "loadgen"
    results_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "docker",
        "compose",
        "run",
        "--rm",
        "-v",
        f"{results_dir}:/results",
        "loadgen",
        "run",
        "-e",
        f"TARGET_URL={target_url}",
        "-e",
        f"RPS={rps}",
        "-e",
        f"WARMUP_S={warmup_s}",
        "-e",
        f"MEASURE_S={measure_s}",
        "-e",
        f"COOLDOWN_S={cooldown_s}",
        "--out",
        "json=/results/raw.jsonl",
        "/scripts/load.js",
    ]

    proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    (results_dir / "k6_stdout.log").write_text(proc.stdout)
    (results_dir / "k6_stderr.log").write_text(proc.stderr)
    if proc.returncode != 0:
        print(f"k6 exited with code {proc.returncode}; see {results_dir}/k6_stderr.log", file=sys.stderr)
    return results_dir


def summarize_service_a(window: list[dict]) -> dict:
    if not window:
        return {"sample_count": 0}

    in_flight_values = [s["in_flight"] for s in window]
    p95_latencies = [s["recent_latency_ms"]["p95"] for s in window if s["recent_latency_ms"]["p95"] is not None]
    open_state_p95_latencies = [
        s["recent_short_circuit_latency_ms"]["p95"]
        for s in window
        if s.get("recent_short_circuit_latency_ms", {}).get("p95") is not None
    ]
    retry_delay_p50s = [
        s["recent_retry_delay_ms"]["p50"]
        for s in window
        if s.get("recent_retry_delay_ms", {}).get("p50") is not None
    ]
    retry_delay_p95s = [
        s["recent_retry_delay_ms"]["p95"]
        for s in window
        if s.get("recent_retry_delay_ms", {}).get("p95") is not None
    ]

    first, last = window[0]["cumulative"], window[-1]["cumulative"]

    return {
        "sample_count": len(window),
        "in_flight_max": max(in_flight_values) if in_flight_values else None,
        "in_flight_avg": sum(in_flight_values) / len(in_flight_values) if in_flight_values else None,
        "request_latency_p95_ms_max": max(p95_latencies) if p95_latencies else None,
        "open_state_latency_p95_ms_max": max(open_state_p95_latencies) if open_state_p95_latencies else None,
        # Rolling-window retry delay percentiles, averaged across the
        # measure window's per-second samples (not maxed, unlike the
        # latency fields above) -- approximates the delay distribution
        # actually sampled over the run, for verifying a jitter
        # implementation matches its intended random(0, backoff) shape
        # rather than confirming a single moment's worst case.
        "retry_delay_p50_ms_avg": (
            sum(retry_delay_p50s) / len(retry_delay_p50s) if retry_delay_p50s else None
        ),
        "retry_delay_p95_ms_avg": (
            sum(retry_delay_p95s) / len(retry_delay_p95s) if retry_delay_p95s else None
        ),
        "offered_count": last["total_count"] - first["total_count"],
        "success_count": last["success_count"] - first["success_count"],
        "timeout_count": last["timeout_count"] - first["timeout_count"],
        "upstream_error_count": last["upstream_error_count"] - first["upstream_error_count"],
        "error_count": last["error_count"] - first["error_count"],
        "retry_count": last["retry_count"] - first["retry_count"],
        "retry_success_count": last["retry_success_count"] - first["retry_success_count"],
        "short_circuit_count": last.get("short_circuit_count", 0) - first.get("short_circuit_count", 0),
    }


def summarize_breaker(window: list[dict]) -> dict:
    if not window:
        return {"sample_count": 0}

    states_seen = sorted({s["state"] for s in window})
    first, last = window[0], window[-1]

    return {
        "sample_count": len(window),
        "states_seen": states_seen,
        "final_state": last["state"],
        "open_count_in_window": last["open_count"] - first["open_count"],
        "open_seconds_in_window": round(last["open_seconds_total"] - first["open_seconds_total"], 3),
        "short_circuit_count_in_window": last["short_circuit_count"] - first["short_circuit_count"],
        "probe_count_in_window": last["probe_count"] - first["probe_count"],
        "probe_success_count_in_window": last["probe_success_count"] - first["probe_success_count"],
    }


def summarize_service_b(window: list[dict]) -> dict:
    if not window:
        return {"sample_count": 0}

    pool_active_values = [s["pool_active"] for s in window]
    pool_wait_p95 = [s["recent_pool_wait_ms"]["p95"] for s in window if s["recent_pool_wait_ms"]["p95"] is not None]
    admission_rejected_latency_p95 = [
        s["recent_admission_rejected_latency_ms"]["p95"]
        for s in window
        if s.get("recent_admission_rejected_latency_ms", {}).get("p95") is not None
    ]
    admission_ewma_values = [
        s["admission_ewma_utilization"] for s in window if s.get("admission_ewma_utilization") is not None
    ]

    first, last = window[0]["cumulative"], window[-1]["cumulative"]

    return {
        "sample_count": len(window),
        "pool_active_max": max(pool_active_values) if pool_active_values else None,
        "pool_wait_p95_ms_max": max(pool_wait_p95) if pool_wait_p95 else None,
        "admission_rejected_latency_p95_ms_max": (
            max(admission_rejected_latency_p95) if admission_rejected_latency_p95 else None
        ),
        # Experiment 007 only: the EWMA admission controller's trailing
        # utilization estimate over the measure window -- max/mean lets
        # post-hoc analysis check it behaves consistently with its stated
        # half-life (the model-fidelity gate), rather than just inferring
        # it indirectly from rejection counts.
        "admission_ewma_utilization_max": max(admission_ewma_values) if admission_ewma_values else None,
        "admission_ewma_utilization_mean": (
            sum(admission_ewma_values) / len(admission_ewma_values) if admission_ewma_values else None
        ),
        # received_count is requests that attempted pool.acquire() -- this
        # is the analog of Experiment 003's "requests reaching B" metric,
        # since here every request reaches Service B's HTTP handler
        # regardless of admission control; only pool-acquisition attempts
        # are meaningfully comparable to what the client-side breaker
        # prevented from reaching B at all.
        "received_count": last["total_count"] - first["total_count"],
        "success_count": last["success_count"] - first["success_count"],
        "pool_timeout_count": last["pool_timeout_count"] - first["pool_timeout_count"],
        "query_timeout_count": last["query_timeout_count"] - first["query_timeout_count"],
        "error_count": last["error_count"] - first["error_count"],
        "admission_rejected_count": (
            last.get("admission_rejected_count", 0) - first.get("admission_rejected_count", 0)
        ),
    }


def summarize_app_metrics(
    samples: list[dict],
    warmup_s: int,
    cooldown_s: int,
    measure_window: dict | None = None,
) -> dict:
    window_a = _select_window(samples, "a", warmup_s, cooldown_s, measure_window)
    window_b = _select_window(samples, "b", warmup_s, cooldown_s, measure_window)
    window_breaker = _select_window(samples, "breaker", warmup_s, cooldown_s, measure_window)

    service_a = summarize_service_a(window_a)
    service_b = summarize_service_b(window_b)
    breaker = summarize_breaker(window_breaker)

    offered = service_a.get("offered_count")
    received = service_b.get("received_count")
    amplification_factor = (received / offered) if offered else None
    retry_rate = (service_a.get("retry_count") / offered) if offered else None
    retry_success_rate = (
        service_a.get("retry_success_count") / service_a.get("success_count")
        if service_a.get("success_count")
        else None
    )
    # Probes are rare, discrete events (a handful per run), and probe_count
    # increments when a probe is granted while probe_success_count only
    # increments once its result arrives up to ~2s later -- so a probe that
    # starts just before the measure window and resolves just after can make
    # the windowed success count exceed the windowed grant count. Clamp the
    # denominator rather than report a >100% rate from that boundary lag.
    probe_count_in_window = breaker.get("probe_count_in_window") or 0
    probe_success_count_in_window = breaker.get("probe_success_count_in_window") or 0
    probe_success_rate = (
        probe_success_count_in_window / max(probe_count_in_window, probe_success_count_in_window)
        if (probe_count_in_window or probe_success_count_in_window)
        else None
    )

    # Fraction of requests Service B's HTTP handler saw that were rejected
    # by admission control before ever attempting pool.acquire(). Denominator
    # is admission-rejected + pool-acquire-attempted, i.e. every request
    # that reached Service B at all -- not just the ones that got in.
    admission_rejected_count = service_b.get("admission_rejected_count") or 0
    pool_attempted_count = service_b.get("received_count") or 0
    reached_b_count = admission_rejected_count + pool_attempted_count
    admission_rejection_rate = (
        admission_rejected_count / reached_b_count if reached_b_count else None
    )

    return {
        "service_a": service_a,
        "service_b": service_b,
        "breaker": breaker,
        "amplification_factor": amplification_factor,
        "retry_rate": retry_rate,
        "retry_success_rate": retry_success_rate,
        "probe_success_rate": probe_success_rate,
        "admission_rejection_rate": admission_rejection_rate,
    }


class ReferenceAdapter(Protocol):
    """Toxiproxy + Service A/B + k6, wired exactly as slimybug/runner.py
    drove them before this split. `configure()` is a container restart
    (writes .env, recreates Compose services); `inject_fault()` is a
    Toxiproxy call; `drive_load()` shells out to k6 and returns its parsed
    summary.json; `snapshot()` polls Service A/B's /internal/* endpoints.
    See RFC 0001 SS5.

    `summarize_app_metrics()` and `describe_run()` are not part of the
    Protocol contract (SS4's four verbs are frozen) -- both are
    adapter-specific extras Runner.run() calls via duck typing on whatever
    adapter it was given. A different adapter isn't required to provide
    either under these exact names; that's the open question RFC 0001
    SS10 Tier 1 (summarize_app_metrics) and the run_id design decision
    (describe_run, SS9 Phase B) flag, not settled generically here.
    """

    # The declaration Runner validates a run config against before calling
    # configure/inject_fault/drive_load (RFC 0001 SS5, SS9 Phase B) -- an
    # active contract, not documentation: a matrix() entry referencing a
    # key outside this set fails fast, before any real infrastructure
    # call. One unified set, not split per Protocol verb, because Runner
    # now passes the same full config dict to all three calls alike (SS6)
    # -- a key meant for drive_load() would look "unrecognized" under a
    # configure()-only check.
    declared_keys = frozenset(
        {
            "retry_policy",
            "breaker_enabled",
            "breaker_window_size",
            "enable_arrival_trace",
            "pool_size",
            "admission_control_enabled",
            "admission_control_mode",
            "admission_ewma_half_life_s",
            "admission_u_low",
            "admission_grace_ms",
            "enable_admission_decision_trace",
            "latency_ms",
            "rps",
            "warmup_s",
            "measure_s",
            "cooldown_s",
            "target_url",
        }
    )

    def __init__(self):
        # inject_fault() is typed -> None (matches the Protocol), but the
        # underlying Toxiproxy call does return the resulting proxy state
        # and Runner.run() still needs to record it in proxy_state.json --
        # stashed here rather than widening the Protocol's return type for
        # one adapter's own bookkeeping.
        self.last_proxy_state: dict | None = None

    def configure(self, params: dict) -> None:
        set_experiment_config(
            retry_policy=params.get("retry_policy", "none"),
            breaker_enabled=params.get("breaker_enabled", False),
            breaker_window_size=params.get("breaker_window_size", 20),
            enable_arrival_trace=params.get("enable_arrival_trace", False),
            pool_size=params.get("pool_size", DEFAULT_POOL_SIZE),
            admission_control_enabled=params.get("admission_control_enabled", False),
            admission_control_mode=params.get("admission_control_mode", "instantaneous"),
            admission_ewma_half_life_s=params.get("admission_ewma_half_life_s", 2.0),
            admission_u_low=params.get("admission_u_low", 0.8),
            admission_grace_ms=params.get("admission_grace_ms", 20.0),
            enable_admission_decision_trace=params.get("enable_admission_decision_trace", False),
        )

    def inject_fault(self, fault: dict) -> None:
        self.last_proxy_state = set_toxic_latency(fault.get("latency_ms", 0))

    def drive_load(self, spec: dict) -> dict:
        # target_url/warmup_s/measure_s/cooldown_s now default here rather
        # than being supplied by Runner (RFC 0001 SS6) -- Runner no longer
        # knows Service A's URL or owns a warmup/measure/cooldown concept.
        run_k6(
            spec["run_dir"],
            target_url=spec.get("target_url", "http://service-a:8000"),
            rps=spec["rps"],
            warmup_s=spec.get("warmup_s", DEFAULT_WARMUP_S),
            measure_s=spec.get("measure_s", DEFAULT_MEASURE_S),
            cooldown_s=spec.get("cooldown_s", DEFAULT_COOLDOWN_S),
        )
        summary_path = spec["run_dir"] / "loadgen" / "summary.json"
        return json.loads(summary_path.read_text()) if summary_path.exists() else {}

    def snapshot(self) -> dict:
        return {
            "a": _poll_one(f"{SERVICE_A_URL}/internal/snapshot"),
            "b": _poll_one(f"{SERVICE_B_URL}/internal/snapshot"),
            "breaker": _poll_one(f"{SERVICE_A_URL}/internal/breaker"),
        }

    def summarize_app_metrics(
        self, samples: list[dict], warmup_s: int, cooldown_s: int, measure_window: dict | None = None
    ) -> dict:
        return summarize_app_metrics(samples, warmup_s, cooldown_s, measure_window=measure_window)

    def describe_run(self, config: dict) -> str:
        """Runner's run_id design decision (RFC 0001 SS9 Phase B): an
        adapter-owned, duck-typed hook Runner falls back to a content hash
        without. Reproduces the exact suffix format runner.py has always
        produced for this adapter, byte-for-byte -- folder names for this
        adapter don't change shape just because Runner's signature did."""
        rps = config["rps"]
        latency_ms = config["latency_ms"]
        retry_policy = config.get("retry_policy", "none")
        breaker_enabled = config.get("breaker_enabled", False)
        breaker_window_size = config.get("breaker_window_size", 20)
        pool_size = config.get("pool_size", DEFAULT_POOL_SIZE)
        admission_control_enabled = config.get("admission_control_enabled", False)
        admission_control_mode = config.get("admission_control_mode", "instantaneous")

        breaker_suffix = "_breaker-on" if breaker_enabled else ""
        window_suffix = f"_win{breaker_window_size}" if breaker_enabled and breaker_window_size != 20 else ""
        pool_suffix = f"_pool{pool_size}" if pool_size != DEFAULT_POOL_SIZE else ""
        admission_suffix = "_admission-on" if admission_control_enabled else ""
        mode_suffix = (
            f"_{admission_control_mode}"
            if admission_control_enabled and admission_control_mode in ("ewma", "graduated", "bounded_grace")
            else ""
        )
        return (
            f"_rps{rps}_lat{latency_ms}_{retry_policy}{breaker_suffix}{window_suffix}"
            f"{pool_suffix}{admission_suffix}{mode_suffix}"
        )
