"""
Breaking-point benchmark: the project's centerpiece result.

Progressively scales aggregate sensor publish load on the *live*
Kubernetes cluster and measures the fusion service's deadline-miss rate
(via the Task 3 timing instrumentation) under real pod scheduling and
networking -- not a single-process loop.

Load is scaled by adding replicas to each sensor Deployment, not by
raising PUBLISH_RATE_HZ. Each sensor simulator is a single-threaded Python
loop with real per-message JSON/MQTT overhead; measurement showed that
overhead caps a single sensor process's achievable rate at roughly
400-500Hz regardless of how high PUBLISH_RATE_HZ is set, well below the
fusion service's own budget. Kubernetes-native replica scaling sidesteps
that single-process ceiling entirely and is the mechanism that actually
stresses the fusion pod's real message-handling throughput, which is what
this benchmark is meant to characterize -- exactly the kind of thing this
project claims to test "as it would really run," not against a synthetic
number a single process can't actually produce.

Usage (with the cluster already up and this repo's manifests applied):
    python benchmarks/breaking_point.py

Each load level is measured over --trials independent sustained windows
(default 3) rather than a single sample, so the reported miss rate and
timing figures are a mean +/- standard deviation instead of one noisy
draw -- an earlier single-trial version of this benchmark produced a
non-monotonic average-duration curve across load levels that was
impossible to distinguish from measurement noise with n=1.

Produces:
    benchmarks/breaking_point_results.csv
    benchmarks/breaking_point_chart.png
and restores the sensors to 1 replica each when done.
"""
import argparse
import csv
import json
import statistics
import subprocess
import sys
import time
import urllib.request

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

BASELINE_RATES_HZ = {
    "lidar-sim": 10.0,
    "camera-sim": 30.0,
    "imu-sim": 100.0,
    "gps-sim": 2.0,
}
DEPLOYMENTS = list(BASELINE_RATES_HZ.keys())

FUSION_STATUS_URL = "http://localhost:18090"
MISS_RATE_STOP_THRESHOLD_PCT = 20.0  # keep going a bit past 1% to show the trend clearly


def run(cmd, **kwargs):
    print(f"$ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, check=True, **kwargs)


def set_replica_multiplier(multiplier):
    replicas = int(multiplier)
    for name in DEPLOYMENTS:
        run(["kubectl", "scale", f"deployment/{name}", f"--replicas={replicas}"],
            capture_output=True, text=True)
    for name in DEPLOYMENTS:
        run(["kubectl", "rollout", "status", f"deployment/{name}", "--timeout=90s"],
            capture_output=True, text=True)


def http_get(path):
    with urllib.request.urlopen(f"{FUSION_STATUS_URL}{path}", timeout=10) as resp:
        return json.loads(resp.read().decode())


def start_fusion_port_forward():
    proc = subprocess.Popen(
        ["kubectl", "port-forward", "deployment/fusion-service", "18090:8080"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    for _ in range(30):
        line = proc.stdout.readline()
        if "Forwarding from" in line:
            break
    time.sleep(1)
    return proc


def kubectl_top_fusion():
    try:
        out = subprocess.run(
            ["kubectl", "top", "pod", "-l", "app=fusion-service", "--no-headers"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        parts = out.split()
        return parts[1] if len(parts) >= 2 else None  # CPU column, e.g. "749m"
    except Exception:
        return None


def _cpu_millicores(cpu_str):
    """Parse a kubectl-top CPU string like '749m' into a float number of
    millicores, or None if unparseable/missing."""
    if not cpu_str or not cpu_str.endswith("m"):
        return None
    try:
        return float(cpu_str[:-1])
    except ValueError:
        return None


def restore_baseline():
    print("\n[breaking_point] restoring sensors to 1 replica each...", flush=True)
    set_replica_multiplier(1)


def mean_std(values):
    if not values:
        return float("nan"), float("nan")
    if len(values) == 1:
        return values[0], 0.0
    return statistics.mean(values), statistics.stdev(values)


def run_trials(multiplier, trials, duration_s):
    """Run `trials` independent measurement windows at the currently
    deployed replica level and return the per-trial stats dicts."""
    trial_rows = []
    for trial in range(1, trials + 1):
        http_get("/timing-reset")
        print(f"[breaking_point] trial {trial}/{trials}: measuring for {duration_s}s...", flush=True)
        time.sleep(duration_s)
        stats = http_get("/timing-stats")
        cpu = kubectl_top_fusion()
        print(f"[breaking_point] trial {trial}/{trials} result: miss_rate={stats['miss_rate_pct']:.2f}% "
              f"avg={stats['avg_duration_ms']:.1f}ms worst={stats['worst_case_ms']:.1f}ms cpu={cpu or 'n/a'}",
              flush=True)
        trial_rows.append({**stats, "fusion_cpu": cpu})
    return trial_rows


def aggregate_trials(multiplier, aggregate_hz, trial_rows):
    miss_rates = [t["miss_rate_pct"] for t in trial_rows]
    avg_durations = [t["avg_duration_ms"] for t in trial_rows]
    worst_cases = [t["worst_case_ms"] for t in trial_rows]
    jitters = [t["jitter_ms"] for t in trial_rows]
    cpus = [c for c in (_cpu_millicores(t["fusion_cpu"]) for t in trial_rows) if c is not None]

    miss_mean, miss_std = mean_std(miss_rates)
    avg_mean, avg_std = mean_std(avg_durations)
    worst_mean, worst_std = mean_std(worst_cases)
    jitter_mean, jitter_std = mean_std(jitters)
    cpu_mean, cpu_std = mean_std(cpus)

    return {
        "multiplier": multiplier,
        "aggregate_hz": aggregate_hz,
        "trials": len(trial_rows),
        "miss_rate_pct_mean": miss_mean,
        "miss_rate_pct_std": miss_std,
        "avg_duration_ms_mean": avg_mean,
        "avg_duration_ms_std": avg_std,
        "worst_case_ms_mean": worst_mean,
        "worst_case_ms_std": worst_std,
        "jitter_ms_mean": jitter_mean,
        "jitter_ms_std": jitter_std,
        "fusion_cpu_millicores_mean": cpu_mean if cpus else float("nan"),
        "fusion_cpu_millicores_std": cpu_std if cpus else float("nan"),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--multipliers", type=str, default="1,2,4,8,16,32",
                         help="comma-separated list of replica-count multipliers to test")
    parser.add_argument("--settle-s", type=float, default=8.0,
                         help="settle time after redeploy before measuring")
    parser.add_argument("--duration-s", type=float, default=20.0,
                         help="sustained measurement duration per trial")
    parser.add_argument("--trials", type=int, default=3,
                         help="independent measurement windows per load level, reported as mean +/- stdev "
                              "rather than a single noisy sample")
    args = parser.parse_args()

    multipliers = [int(m) for m in args.multipliers.split(",")]

    fusion_pf = start_fusion_port_forward()
    results = []
    crossed_1pct_at = None
    stop_reason = None

    try:
        for multiplier in multipliers:
            aggregate_hz = sum(BASELINE_RATES_HZ.values()) * multiplier
            print(f"\n[breaking_point] === level {multiplier}x replicas (aggregate {aggregate_hz:.0f} Hz), "
                  f"{args.trials} trials ===", flush=True)

            try:
                set_replica_multiplier(multiplier)
            except subprocess.CalledProcessError:
                print(f"[breaking_point] level {multiplier}x failed to roll out -- likely exceeded this "
                      f"node's pod-scheduling capacity ({multiplier * len(DEPLOYMENTS)} sensor pods requested). "
                      f"Treating this as the practical breaking point and stopping here.", flush=True)
                stop_reason = f"{multiplier}x replicas failed to schedule (node pod-capacity limit)"
                break

            print(f"[breaking_point] settling for {args.settle_s}s...", flush=True)
            time.sleep(args.settle_s)

            trial_rows = run_trials(multiplier, args.trials, args.duration_s)
            row = aggregate_trials(multiplier, aggregate_hz, trial_rows)
            results.append(row)
            print(f"[breaking_point] level result (mean +/- stdev over {row['trials']} trials): "
                  f"miss_rate={row['miss_rate_pct_mean']:.2f}+/-{row['miss_rate_pct_std']:.2f}% "
                  f"avg={row['avg_duration_ms_mean']:.1f}+/-{row['avg_duration_ms_std']:.1f}ms", flush=True)

            if crossed_1pct_at is None and row["miss_rate_pct_mean"] > 1.0:
                crossed_1pct_at = aggregate_hz

            if row["miss_rate_pct_mean"] >= MISS_RATE_STOP_THRESHOLD_PCT:
                print(f"[breaking_point] miss rate clearly failing "
                      f"({row['miss_rate_pct_mean']:.1f}%), stopping early.", flush=True)
                stop_reason = f"miss rate reached {row['miss_rate_pct_mean']:.1f}% (clearly failing)"
                break
    finally:
        fusion_pf.terminate()
        restore_baseline()

    if not results:
        print("[breaking_point] no levels completed successfully -- nothing to report.", file=sys.stderr)
        sys.exit(1)

    write_csv(results)
    write_chart(results, crossed_1pct_at)
    print_table(results, crossed_1pct_at, stop_reason)


def write_csv(results):
    path = "benchmarks/breaking_point_results.csv"
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        writer.writeheader()
        writer.writerows(results)
    print(f"\n[breaking_point] wrote {path}")


def write_chart(results, crossed_1pct_at):
    xs = [r["aggregate_hz"] for r in results]
    miss_ys = [r["miss_rate_pct_mean"] for r in results]
    miss_err = [r["miss_rate_pct_std"] for r in results]
    avg_ys = [r["avg_duration_ms_mean"] for r in results]
    avg_err = [r["avg_duration_ms_std"] for r in results]
    worst_ys = [r["worst_case_ms_mean"] for r in results]
    worst_err = [r["worst_case_ms_std"] for r in results]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 9), sharex=True)

    ax1.errorbar(xs, miss_ys, yerr=miss_err, marker="o", color="#2563eb", linewidth=2,
                 capsize=4, label="deadline-miss rate (mean +/- stdev)")
    ax1.axhline(1.0, color="#dc2626", linestyle="--", linewidth=1.5, label="1% threshold")
    if crossed_1pct_at is not None:
        ax1.axvline(crossed_1pct_at, color="#dc2626", linestyle=":", linewidth=1)
        ax1.annotate(f"crosses 1% at ~{crossed_1pct_at:.0f} Hz",
                      xy=(crossed_1pct_at, 1.0), xytext=(10, 20),
                      textcoords="offset points", color="#dc2626")
    ax1.set_ylabel("Deadline-miss rate (%)")
    ax1.set_title(f"Fusion service breaking point: deadline-miss rate vs. sensor load "
                  f"(n={results[0]['trials']} trials/level)")
    ax1.legend()
    ax1.grid(True, alpha=0.3)

    ax2.errorbar(xs, avg_ys, yerr=avg_err, marker="o", color="#059669", linewidth=2,
                 capsize=4, label="avg cycle duration (mean +/- stdev)")
    ax2.errorbar(xs, worst_ys, yerr=worst_err, marker="s", color="#d97706", linewidth=2,
                 capsize=4, label="worst-case cycle duration (mean +/- stdev)")
    ax2.axhline(100.0, color="#dc2626", linestyle="--", linewidth=1.5, label="100ms deadline")
    ax2.set_xlabel("Aggregate sensor publish rate (Hz)")
    ax2.set_ylabel("Cycle duration (ms)")
    ax2.set_title("Timing degradation: cycle duration vs. sensor load")
    ax2.legend()
    ax2.grid(True, alpha=0.3)

    fig.tight_layout()

    path = "benchmarks/breaking_point_chart.png"
    fig.savefig(path, dpi=150)
    print(f"[breaking_point] wrote {path}")


def print_table(results, crossed_1pct_at, stop_reason=None):
    print("\n| multiplier | aggregate Hz | trials | miss rate % (mean+/-std) | avg ms (mean+/-std) | "
          "worst-case ms (mean+/-std) |")
    print("|---|---|---|---|---|---|")
    for r in results:
        print(f"| {r['multiplier']}x | {r['aggregate_hz']:.0f} | {r['trials']} | "
              f"{r['miss_rate_pct_mean']:.2f}+/-{r['miss_rate_pct_std']:.2f} | "
              f"{r['avg_duration_ms_mean']:.1f}+/-{r['avg_duration_ms_std']:.1f} | "
              f"{r['worst_case_ms_mean']:.1f}+/-{r['worst_case_ms_std']:.1f} |")
    if crossed_1pct_at is not None:
        print(f"\nMiss rate crosses 1% at ~{crossed_1pct_at:.0f} Hz aggregate sensor publish rate.")
    else:
        print("\nMiss rate never crossed 1% within the tested range.")
    if stop_reason:
        print(f"Benchmark stopped: {stop_reason}")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        print(f"[breaking_point] command failed: {e}", file=sys.stderr)
        sys.exit(1)
