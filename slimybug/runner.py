"""Runner: the shared, mechanism-agnostic execution engine for any
SlimyBug experiment (RFC 0001 SS4/SS6).

Drives one run config (as declared by an Experiment's matrix()) against
whatever Protocol-implementing adapter it's given -- generic orchestration
(snapshot polling, windowing, git-commit/environment stamping, raw-artifact
writing) lives here; everything Service-A/B/Toxiproxy/k6-specific moved to
slimybug/reference_adapter.py in RFC 0001 SS9 Phase A milestone 2.

Runner.run() takes one flat, opaque `config` dict -- the same object is
passed to configure()/inject_fault()/drive_load() alike, Runner never
partitions it or inspects individual keys to decide what goes where (RFC
0001 SS6, "Runner is mechanism-blind"; SS9 Phase B). run_id naming is the
adapter's job too, via an optional `describe_run(config) -> str` duck-typed
method (not part of the frozen four-verb Protocol) -- Runner falls back to
a content hash if the adapter doesn't provide one.

Runner still directly calls http_get_json/http_json against
SERVICE_A_URL/SERVICE_B_URL for a few things this doesn't cover
(a_config/b_config for metadata.json, arrival_trace/admission_decision_trace
reset+read, both gated on config.get("enable_...") -- still Runner reading
adapter-specific keys by name) -- a known, RFC-acknowledged gap (SS10 Tier
1), not full mechanism-blindness for artifact writing, only for the four
Protocol calls themselves.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import threading
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SERVICE_A_URL = "http://localhost:8000"
SERVICE_B_URL = "http://localhost:8001"
TOXIPROXY_URL = "http://localhost:8474"
PROXY_NAME = "postgres"
TOXIC_NAME = "latency_downstream"


class AdapterValidationError(Exception):
    """A run config, LoadSummary, or snapshot sample didn't satisfy the
    active adapter's own declaration or the Protocol's required fields.
    Raised before any adapter method runs (for declared-key checks) or
    immediately after (for the measure_window/timestamp checks that can
    only be known once the adapter has actually returned something) --
    RFC 0001 SS9 Phase A milestone 3."""


class Protocol(ABC):
    """The mechanism contract Runner drives against (RFC 0001 SS4). Phase A
    milestone 1: extracted as an interface with no behavior change --
    ReferenceAdapter below implements it by wrapping today's existing
    functions verbatim, still in this file. Milestone 2 moves
    ReferenceAdapter into its own module; this class doesn't move until
    a second, independently-written adapter exists to prove the interface
    generalizes."""

    @abstractmethod
    def configure(self, params: dict) -> None:
        """Apply a set of adapter-defined config knobs, opaque to core."""

    @abstractmethod
    def inject_fault(self, fault: dict) -> None:
        """Apply/clear a fault. Adapter-defined shape."""

    @abstractmethod
    def drive_load(self, spec: dict) -> dict:
        """Run a load spec (adapter-defined), return a LoadSummary dict."""

    @abstractmethod
    def snapshot(self) -> dict:
        """One point-in-time read of adapter-defined state."""


def _validate_declared_keys(adapter: Protocol, declared_attr: str, config: dict, call_label: str) -> None:
    """Fail fast if `config` references a key the adapter doesn't declare
    support for, rather than discovering it partway through a real run
    (RFC 0001 SS5: "an active contract, not documentation"). An adapter
    that hasn't defined `declared_attr` at all is skipped, not failed --
    declaration is opt-in per adapter, not enforced by the Protocol ABC
    itself (SS4 is frozen; this stays a duck-typed convention layered on
    top of it, not a new abstractmethod)."""
    declared = getattr(adapter, declared_attr, None)
    if declared is None:
        return
    unknown = sorted(set(config) - set(declared))
    if unknown:
        raise AdapterValidationError(
            f"{type(adapter).__name__}.{call_label}() does not declare support for: "
            f"{', '.join(unknown)} (declared via {declared_attr}: {sorted(declared)})"
        )


def _git_commit_info() -> dict:
    """The commit a run was executed against, plus whether the working
    tree was dirty at that moment. This is what makes it valid to omit a
    `version` field from ExperimentMetadata (see slimybug/experiment.py) --
    that reasoning only holds if a run's metadata actually records which
    commit it ran against, rather than assuming it can be reconstructed
    after the fact."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
            ).stdout.strip()
        )
        return {"commit": commit, "dirty": dirty}
    except Exception:
        return {"commit": None, "dirty": None}


def _environment_info() -> dict:
    """Host environment a run executed on -- platform, CPU count, and
    Docker version. Not needed to interpret a single research-grade run
    (the git commit already pins the code), but load-bearing for
    reference-grade evidence, where "reproduce this later" has to mean
    something more than "same repo state" -- see reference/R001-.../README.md."""
    try:
        docker_version = subprocess.run(
            ["docker", "--version"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:
        docker_version = None

    return {
        "platform": platform.platform(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "python_version": platform.python_version(),
        "docker_version": docker_version,
    }


def http_get_json(url: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def http_json(method: str, url: str, payload: dict | None = None, timeout: float = 5.0):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


class SnapshotPoller:
    def __init__(self, adapter: "Protocol", interval_s: float = 1.0):
        self.adapter = adapter
        self.interval_s = interval_s
        self.samples: list[dict] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self):
        while not self._stop.is_set():
            start = time.monotonic()
            now = time.monotonic()
            self.samples.append({"poll_monotonic": now, **self.adapter.snapshot()})
            elapsed = time.monotonic() - start
            self._stop.wait(max(0.0, self.interval_s - elapsed))

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)


def _select_window(samples: list[dict], key: str, warmup_s: int, cooldown_s: int, measure_window: dict | None):
    """Pick the sub-list of per-service snapshots (a or b) inside the measure window."""
    valid = [s[key] for s in samples if "error" not in s.get(key, {"error": "missing"})]
    if not valid:
        return []

    if measure_window is not None:
        # Preferred: k6 reports the measurement window as absolute epoch-ms,
        # timestamped from its own scenario clock. Aligning app samples to
        # that -- rather than to when our polling thread happened to start --
        # avoids skew from docker/container startup latency between the two.
        measure_start = measure_window["start_epoch_ms"] / 1000
        measure_end = measure_window["end_epoch_ms"] / 1000
        return [s for s in valid if measure_start <= s["timestamp"] <= measure_end]

    poll_times = [s["poll_monotonic"] for s in samples if "error" not in s.get(key, {"error": "missing"})]
    t0, t_last = poll_times[0], poll_times[-1]
    measure_start = t0 + warmup_s
    measure_end = t_last - cooldown_s
    return [s for s, t in zip(valid, poll_times) if measure_start <= t <= measure_end]


class Runner:
    """Executes one run config for a given experiment, writing artifacts
    to <experiment>/runs/<run_id>/. `adapter` must be supplied by the
    caller (e.g. scripts/run_experiment.py constructs a
    slimybug.reference_adapter.ReferenceAdapter()) -- Runner itself
    doesn't know any adapter's concrete type, only the Protocol it
    implements."""

    def __init__(self, runs_dir: Path, experiment_id: str, adapter: Protocol):
        self.runs_dir = runs_dir
        self.experiment_id = experiment_id
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.adapter = adapter

    def _build_run_id(self, config: dict, timestamp: datetime) -> str:
        describe = getattr(self.adapter, "describe_run", None)
        if describe is not None:
            suffix = describe(config)
        else:
            # No naming convention declared -- fall back to a short,
            # stable hash of the config so run_ids stay unique without
            # Runner inspecting any specific key (RFC 0001 SS6).
            digest = hashlib.sha1(json.dumps(config, sort_keys=True, default=str).encode()).hexdigest()[:8]
            suffix = f"_{digest}"
        return f"{timestamp.strftime('%Y%m%dT%H%M%SZ')}{suffix}"

    def run(self, config: dict, run_id: str | None = None) -> Path:
        timestamp = datetime.now(timezone.utc)

        # Fail fast, before any adapter method runs or touches real
        # infrastructure -- RFC 0001 SS9 Phase A milestone 3. Validated
        # against the raw config, before run_dir is merged in below --
        # run_dir is Runner's own trusted addition, not user input to
        # typo-check.
        _validate_declared_keys(self.adapter, "declared_keys", config, "run")

        run_id = run_id or self._build_run_id(config, timestamp)
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        # The same flat dict goes to configure/inject_fault/drive_load
        # alike -- each adapter method picks out whatever keys it cares
        # about and ignores the rest (RFC 0001 SS6). Runner never asks
        # "which keys does configure() want" vs "which does drive_load()
        # want".
        run_config = {**config, "run_dir": run_dir}

        print(f"[{self.experiment_id}/{run_id}] configuring: {config}")
        self.adapter.configure(run_config)

        self.adapter.inject_fault(run_config)
        proxy_state = self.adapter.last_proxy_state

        # Still Runner reading adapter-specific keys by name -- the same
        # acknowledged gap as a_config/b_config below, just reached via
        # config.get() instead of a named parameter now. Not resolved by
        # this milestone; see module docstring.
        if config.get("enable_arrival_trace"):
            http_json("POST", f"{SERVICE_B_URL}/internal/arrival_trace/reset")

        if config.get("enable_admission_decision_trace"):
            http_json("POST", f"{SERVICE_B_URL}/internal/admission_decision_trace/reset")

        poller = SnapshotPoller(self.adapter, interval_s=1.0)
        try:
            a_config = http_get_json(f"{SERVICE_A_URL}/internal/config")
            b_config = http_get_json(f"{SERVICE_B_URL}/internal/config")

            poller.start()

            print(f"[{self.experiment_id}/{run_id}] driving load")
            load_results = self.adapter.drive_load(run_config)
            # LoadSummary.measure_window is required (RFC 0001 SS4), not
            # optional -- Runner's windowing depends on it whenever it's
            # present. Caught here, inside the try, so the finally below
            # still cleans up the injected fault before this propagates.
            if "measure_window" not in load_results:
                raise AdapterValidationError(
                    f"{type(self.adapter).__name__}.drive_load() returned a LoadSummary without "
                    "'measure_window' (required by RFC 0001 SS4)"
                )
        finally:
            poller.stop()
            # Always leave the proxy clean so a crashed/interrupted run never
            # leaves stale latency injected for whatever runs next.
            self.adapter.inject_fault({"latency_ms": 0})

        # snapshot()'s protocol-required 'timestamp' field (SS4) is what
        # _select_window actually reads today -- nested under each of this
        # adapter's per-service fragments ("a"/"b"/"breaker"), since a
        # multi-service snapshot has no single top-level moment. Checked
        # against the first successful sample only (a spot check, not a
        # per-sample audit) once real polling has happened, not before --
        # unlike the declared-key checks above, there's nothing to check
        # until the adapter has actually produced a sample.
        first_sample = next((s for s in poller.samples if len(s) > 1), None)
        if first_sample is not None:
            for key, fragment in first_sample.items():
                if key == "poll_monotonic" or "error" in fragment:
                    continue
                if "timestamp" not in fragment:
                    raise AdapterValidationError(
                        f"{type(self.adapter).__name__}.snapshot()['{key}'] is missing 'timestamp' "
                        "(required by RFC 0001 SS4 for windowing against LoadSummary.measure_window)"
                    )

        raw_samples_path = run_dir / "raw_app_samples.jsonl"
        with raw_samples_path.open("w") as f:
            for sample in poller.samples:
                f.write(json.dumps(sample) + "\n")

        if config.get("enable_arrival_trace"):
            trace = http_get_json(f"{SERVICE_B_URL}/internal/arrival_trace")
            arrival_trace_path = run_dir / "arrival_trace.csv"
            with arrival_trace_path.open("w") as f:
                f.write("arrival_ns\n")
                for arrival_ns in trace.get("arrivals_ns", []):
                    f.write(f"{arrival_ns}\n")
            print(f"[{self.experiment_id}/{run_id}] arrival trace: {trace.get('count', 0)} arrivals -> {arrival_trace_path}")

        if config.get("enable_admission_decision_trace"):
            decision_trace = http_get_json(f"{SERVICE_B_URL}/internal/admission_decision_trace")
            decision_trace_path = run_dir / "admission_decision_trace.csv"
            with decision_trace_path.open("w") as f:
                # deferred/wait_ms/pool_active_2 are only populated under
                # Experiment 009's bounded_grace mode -- blank otherwise.
                # t_ns (decision-start monotonic time, added for Experiment
                # 010) is blank only for runs recorded before this field
                # existed -- absent from the source dict entirely, not None.
                f.write("t_ns,pool_active,rejected,deferred,wait_ms,pool_active_2\n")
                for d in decision_trace.get("decisions", []):
                    t_ns = d.get("t_ns")
                    deferred = d.get("deferred", False)
                    wait_ms = d.get("wait_ms")
                    pool_active_2 = d.get("pool_active_2")
                    f.write(
                        f"{'' if t_ns is None else t_ns},"
                        f"{d['pool_active']},{d['rejected']},{deferred},"
                        f"{'' if wait_ms is None else wait_ms},"
                        f"{'' if pool_active_2 is None else pool_active_2}\n"
                    )
            print(
                f"[{self.experiment_id}/{run_id}] admission decision trace: "
                f"{decision_trace.get('count', 0)} decisions -> {decision_trace_path}"
            )

        # warmup_s/cooldown_s are only used by _select_window's poll_monotonic
        # fallback path, which is unreachable now that measure_window is
        # required (checked above) -- passed through anyway rather than
        # assuming config has these keys at all (a different adapter's
        # config might not use a warmup/measure/cooldown model, RFC 0001
        # SS6: "Runner does not own warmup/measure/cooldown as a protocol
        # concept").
        app_metrics = self.adapter.summarize_app_metrics(
            poller.samples,
            config.get("warmup_s"),
            config.get("cooldown_s"),
            measure_window=load_results.get("measure_window"),
        )

        # rps/injected_latency_ms/warmup_s/measure_s/cooldown_s/duration_s
        # come from what the adapter actually reports back (load_results'
        # echoed run_config, proxy_state's echoed fault), not from `config`
        # directly -- authoritative for what was actually applied, robust
        # to whatever defaults the adapter filled in for keys `config`
        # omitted (Runner no longer supplies these defaults itself).
        effective = load_results.get("run_config", {})
        git_info = _git_commit_info()
        metadata = {
            "run_id": run_id,
            "experiment_id": self.experiment_id,
            "timestamp": timestamp.isoformat(),
            "git_commit": git_info["commit"],
            "git_dirty": git_info["dirty"],
            "environment": _environment_info(),
            "rps": effective.get("rps"),
            "injected_latency_ms": proxy_state.get("configured_latency_ms"),
            "retry_policy": config.get("retry_policy", "none"),
            "breaker_enabled": config.get("breaker_enabled", False),
            "breaker_window_size": config.get("breaker_window_size", 20),
            "enable_arrival_trace": config.get("enable_arrival_trace", False),
            "admission_control_enabled": b_config.get("admission_control_enabled"),
            "admission_control_mode": b_config.get("admission_control_mode"),
            "admission_ewma_half_life_s": b_config.get("admission_ewma_half_life_s"),
            "admission_u_low": b_config.get("admission_u_low"),
            "admission_grace_ms": b_config.get("admission_grace_ms"),
            "enable_admission_decision_trace": config.get("enable_admission_decision_trace", False),
            "max_attempts": a_config.get("max_attempts"),
            "pool_size": b_config.get("pool_max_size"),
            "query_timeout_s": b_config.get("query_timeout"),
            "http_timeout_s": a_config.get("http_timeout"),
            "warmup_s": effective.get("warmup_s"),
            "measure_s": effective.get("measure_s"),
            "cooldown_s": effective.get("cooldown_s"),
            "duration_s": effective.get("total_s"),
        }

        (run_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))
        (run_dir / "results.json").write_text(
            json.dumps({"load_generator": load_results, "application": app_metrics}, indent=2)
        )
        (run_dir / "proxy_state.json").write_text(json.dumps(proxy_state, indent=2))

        print(f"[{self.experiment_id}/{run_id}] done -> {run_dir}")
        return run_dir
