"""Construct real Finding instances (slimybug/finding.py, schema per RFC
0001 SS3) from Experiment 011 and its three reference-grade validations
(R001-R003), plus Experiment 003 (circuit breaker), using each one's
already-published README claim plus its actual recorded runs, and write
each as finding.json colocated with its experiment/reference directory --
the storage convention `slimybug findings list/show` (scripts/findings.py)
reads from.

Originated as a schema-validation exercise (RFC 0001 SS9, independent
track item 5) -- the schema held up against real evidence; see RFC 0001
SS3's "Grounded against real evidence" note and SS10 Tier 2 item 5 for
what that surfaced. Extended to cover 003 as RFC 0001 Tier 1 item 2's
"second mechanism template" grounding step -- 003 predates the Finding
schema and the Protocol/Adapter split entirely (it ran under the old
monolithic runner), so constructing a Finding for it retroactively tests
whether the schema holds for a structurally different mechanism (a
discrete open/closed breaker, not a continuous EWMA signal) without
requiring any new runs or Runner changes. See its validity_checks below
for what that surfaced concretely.

Re-running this script is idempotent: it always reconstructs the same
Findings from the same source data and overwrites their finding.json in
place -- safe because none of these are frozen yet (nothing currently
reads finding.json as closed evidence); the same would NOT be true
post-freeze, per the schema's own immutability rule.

Usage:
  python scripts/prototype_findings.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from slimybug.aggregate import load_runs
from slimybug.experiment import load_experiment
from slimybug.finding import Evidence, Finding, Scope, Swept, ValidityCheck


def run_ids_by(runs: list[dict], key_fn) -> dict:
    groups = defaultdict(list)
    for r in runs:
        groups[key_fn(r)].append(r["run_id"])
    return {str(k): sorted(v) for k, v in sorted(groups.items(), key=lambda kv: str(kv[0]))}


def admission_condition(r: dict) -> str:
    if not r.get("admission_control_enabled"):
        return "off"
    if r.get("admission_control_mode") == "ewma":
        return f"ewma_hl{r.get('admission_ewma_half_life_s')}"
    return "instantaneous"


findings: list[Finding] = []
dir_paths: dict[str, Path] = {}  # finding id -> experiment/reference dir, for writing finding.json

# ---------------------------------------------------------------- 011 ----
exp011 = load_experiment("011")
dir_paths["011"] = exp011.dir_path
runs011 = load_runs(exp011.runs_dir)
runs011_rps16 = [r for r in runs011 if r["rps"] == 16]

findings.append(
    Finding(
        id="011",
        claim=(
            "At RPS 16, error rate rises smoothly and continuously as the "
            "EWMA admission signal's half-life increases from 0.06s to "
            "4.0s, with no adjacent pair of half-lives separated by a "
            "disproportionate jump -- information freshness has a graded "
            "effect, not a privileged threshold half-life."
        ),
        scope=Scope(
            experiment_id="011",
            # NOTE: ExperimentMetadata.fixed_params (code-level, whole
            # experiment) omits rps, since 011's matrix() also runs an
            # RPS12 validity check -- but rps=16 IS held constant across
            # every run this specific claim draws on. Scope.fixed_params
            # is per-claim, not an alias of the code's metadata object.
            fixed_params={**exp011.metadata.fixed_params, "rps": 16},
            swept=Swept(variable="admission_ewma_half_life_s", tested_values=[0.06, 0.25, 0.5, 1.0, 2.0, 4.0]),
        ),
        evidence=Evidence(
            grade="research",
            run_ids=run_ids_by(runs011_rps16, admission_condition),
            n_per_condition=1,
            variance_reported=False,
            analysis_ref="experiments/011-half-life-sensitivity/summary.csv",
        ),
        validity_checks=[
            ValidityCheck(
                name="rps12_validity_check",
                passed=True,
                note="~0% admission_rejection_rate at RPS 12 for instantaneous and every half-life",
            ),
        ],
        status="closed",
        narrative_ref="experiments/011-half-life-sensitivity/README.md",
    )
)

# --------------------------------------------------------------- R001 ----
expR001 = load_experiment("R001")
dir_paths["R001"] = expR001.dir_path
runsR001 = load_runs(expR001.runs_dir)

findings.append(
    Finding(
        id="R001",
        claim=(
            "Across 5 replicated runs each at three clean-edge and three "
            "collapse-point cells (pool sizes 10/20/40), clean-edge error "
            "rate was exactly 0.00% every time and collapse-point error "
            "rate clustered near-total (stdev <=0.095pp) at every pool "
            "size -- Experiment 005's single-run observations at these "
            "six cells were not artifacts of one lucky execution."
        ),
        scope=Scope(
            experiment_id="R001",
            fixed_params=dict(expR001.metadata.fixed_params),
            swept=Swept(variable="pool_size", tested_values=[10, 20, 40]),
        ),
        evidence=Evidence(
            grade="reference",
            run_ids=run_ids_by(runsR001, lambda r: f"pool{r['pool_size']}_rps{r['rps']}"),
            n_per_condition=5,
            variance_reported=True,
            analysis_ref="reference/R001-pool-capacity-validation/summary.csv",
        ),
        validity_checks=[
            ValidityCheck(
                name="escalation_rule",
                passed=True,
                note="not triggered -- observed variance smaller than anticipated",
            ),
        ],
        status="closed",
        narrative_ref="reference/R001-pool-capacity-validation/README.md",
        refines="005",
    )
)

# --------------------------------------------------------------- R002 ----
expR002 = load_experiment("R002")
dir_paths["R002"] = expR002.dir_path
runsR002 = load_runs(expR002.runs_dir)

findings.append(
    Finding(
        id="R002",
        claim=(
            "Bounded admission deferral (grace_ms=20) measurably reduces "
            "client-visible error near the collapse boundary (RPS14: "
            "16.81% instantaneous vs 13.10% bounded_grace, -3.71pp, 76.1% "
            "rescue rate), that benefit is statistically gone by RPS18 "
            "(33.33% vs 33.32%), and at RPS16 the mean error rates "
            "converge (23.03% both) while the underlying rescue-decision "
            "mechanism is bimodal, not intermediate -- 8 of 10 runs "
            "cluster at 0-25% rescue, 2 of 10 jump to ~62%, a real "
            "reproducible split invisible to the aggregate error-rate "
            "metric."
        ),
        scope=Scope(
            experiment_id="R002",
            fixed_params=dict(expR002.metadata.fixed_params),
            swept=Swept(variable="admission_control_mode", tested_values=["instantaneous", "bounded_grace"]),
        ),
        evidence=Evidence(
            grade="reference",
            run_ids=run_ids_by(runsR002, lambda r: f"{r.get('admission_control_mode')}_rps{r['rps']}"),
            # Heterogeneous: escalated from N=5 to N=10 at rps16 only --
            # the schema's "N for reference-grade" (implying one number)
            # doesn't fit an escalated design without going to a dict.
            n_per_condition={"rps12": 5, "rps14": 5, "rps16": 10, "rps18": 5},
            variance_reported=True,
            analysis_ref="reference/R002-bounded-deferral-validation/summary.csv",
        ),
        validity_checks=[
            ValidityCheck(
                name="rps12_validity_check",
                passed=True,
                note="~0% error both conditions, confirms below collapse boundary",
            ),
            ValidityCheck(
                name="escalation_rule",
                passed=True,
                note=(
                    "triggered once at bounded_grace_rps16 -- N=5 to N=10, rescue-rate spread "
                    "stayed wide (23.5pp -> 22.5pp stdev), a real bimodal split not a "
                    "small-sample artifact"
                ),
            ),
        ],
        status="closed",
        narrative_ref="reference/R002-bounded-deferral-validation/README.md",
        refines="009",
    )
)

# --------------------------------------------------------------- R003 ----
expR003 = load_experiment("R003")
dir_paths["R003"] = expR003.dir_path
runsR003 = load_runs(expR003.runs_dir)

findings.append(
    Finding(
        id="R003",
        claim=(
            "Across 5 replicated runs at each of Experiment 011's eight "
            "RPS16 cells, the continuous-degradation shape holds -- every "
            "adjacent-cell gap (0.95-32.83pp) is far larger than any "
            "cell's own spread (stdev <=2.63pp), and no cell shows the "
            "bimodal split Experiment 010 found in a different mechanism "
            "at this RPS. One correction to 011: half_life=4.0s does not "
            "fully converge to `off` -- it replicates at 95.11% (stdev "
            "0.04pp) against off's 99.90% (stdev 0.08pp), a small but "
            "real and reproducible residual EWMA benefit, not the exact "
            "match 011's single run measured (99.93% both)."
        ),
        scope=Scope(
            experiment_id="R003",
            fixed_params=dict(expR003.metadata.fixed_params),
            swept=Swept(variable="admission_ewma_half_life_s", tested_values=[0.06, 0.25, 0.5, 1.0, 2.0, 4.0]),
        ),
        evidence=Evidence(
            grade="reference",
            run_ids=run_ids_by(runsR003, admission_condition),
            n_per_condition=5,
            variance_reported=True,
            analysis_ref="reference/R003-half-life-sensitivity-validation/summary.csv",
        ),
        validity_checks=[
            ValidityCheck(
                name="escalation_rule",
                passed=True,
                note="not triggered -- max stdev 2.63pp, no bimodal split observed",
            ),
        ],
        status="closed",
        narrative_ref="reference/R003-half-life-sensitivity-validation/README.md",
        refines="011",
    )
)


# --------------------------------------------------------------- 003 -----
exp003 = load_experiment("003")
dir_paths["003"] = exp003.dir_path
runs003 = load_runs(exp003.runs_dir)

findings.append(
    Finding(
        id="003",
        claim=(
            "A circuit breaker wrapping only the Service-B call (retries "
            "disabled) reduces both downstream load and client-visible "
            "error above Experiment 002's collapse boundary (RPS 14-18): "
            "amplification drops from ~1.0x to 0.57-0.76x, and "
            "client-visible error rate drops by up to 61 percentage "
            "points (RPS16: 99.9% to 58.9%), with zero false-positive "
            "trips at RPS12 (below the boundary). Every half-open probe "
            "succeeded (100% probe success rate at every saturated RPS), "
            "confirming the collapse is a queueing/capacity effect, not "
            "a hard failure -- consistent with Experiment 002's read."
        ),
        scope=Scope(
            experiment_id="003",
            fixed_params=dict(exp003.metadata.fixed_params),
            swept=Swept(variable="breaker_enabled", tested_values=[False, True]),
        ),
        evidence=Evidence(
            grade="research",
            run_ids=run_ids_by(runs003, lambda r: f"breaker{r['breaker_enabled']}_rps{r['rps']}"),
            n_per_condition=1,
            variance_reported=False,
            analysis_ref="experiments/003-circuit-breaker/summary.csv",
        ),
        validity_checks=[
            ValidityCheck(
                name="no_false_positive_at_rps12",
                passed=True,
                note="breaker_open_count=0, error_rate=0.0% at RPS12 with breaker on -- identical to breaker-off",
            ),
            ValidityCheck(
                name="probe_success_rate_100pct",
                passed=True,
                note=(
                    "100% probe success at every above-boundary RPS (14/16/18) -- no "
                    "equivalent check exists in any admission-control Finding, since "
                    "half-open probing is a breaker-specific concept with nothing "
                    "resembling it in EWMA-based admission control"
                ),
            ),
        ],
        status="closed",
        narrative_ref="experiments/003-circuit-breaker/README.md",
    )
)


def main():
    print(f"Constructed {len(findings)} Finding(s):\n")
    for f in findings:
        d = f.to_dict()
        serialized = json.dumps(d, indent=2)  # must round-trip as JSON
        json.loads(serialized)
        total_runs = sum(len(v) for v in d["evidence"]["run_ids"].values())
        cells = len(d["evidence"]["run_ids"])
        refines = f" refines={f.refines}" if f.refines else ""
        print(
            f"  {f.id:5s} grade={f.evidence.grade:9s} "
            f"cells={cells:2d} runs={total_runs:3d} "
            f"validity_checks={len(f.validity_checks)}{refines}"
        )

    print()
    for f in findings:
        dir_path = dir_paths[f.id]
        finding_path = dir_path / "finding.json"
        f.write(finding_path)
        print(f"  wrote {finding_path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
