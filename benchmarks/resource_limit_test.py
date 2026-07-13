"""
Resource-limit stress test: demonstrates that Kubernetes' CPU limit on the
fusion Deployment isn't just deployment plumbing -- it has a measurable,
provable effect on the system's real-time guarantees.

At a fixed sensor load (matching one of the breaking-point benchmark's
levels), this script measures the fusion service's deadline-miss rate
first under its normal resource limits, then again after deliberately
tightening the CPU limit well below what that load level needs, and
finally restores the original limits.

Usage (with the cluster already up and this repo's manifests applied):
    python benchmarks/resource_limit_test.py

Each limit level is measured over --trials independent windows (default 3)
and reported as mean +/- standard deviation rather than a single sample.

Produces:
    benchmarks/resource_limit_results.csv
    benchmarks/resource_limit_chart.png
and restores the fusion Deployment's original resource limits (from
k8s/fusion.yaml) and the sensors to 1 replica each when done.
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

FUSION_STATUS_URL = "http://localhost:18091"
FIXED_LOAD_MULTIPLIER = 8  # matches the 8x / 1136 Hz level from the breaking-point benchmark
DEPLOYMENTS = ["lidar-sim", "camera-sim", "imu-sim", "gps-sim"]


def run(cmd, **kwargs):
    print(f"$ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, check=True, **kwargs)


def http_get(path, retries=5, retry_delay_s=3.0):
    """A CPU-starved fusion pod may not service its own HTTP endpoint in
    time, so a connection failure here isn't a bug in the harness -- it's
    itself evidence of the resource constraint. Retry a few times before
    giving up and letting the caller treat "unresponsive" as a result."""
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(f"{FUSION_STATUS_URL}{path}", timeout=10) as resp:
                return json.loads(resp.read().decode())
        except (OSError, ValueError) as e:
            last_error = e
            print(f"[resource_limit_test] {path} attempt {attempt}/{retries} failed ({e}); "
                  f"retrying in {retry_delay_s}s...", flush=True)
            time.sleep(retry_delay_s)
    raise last_error


def start_fusion_port_forward():
    proc = subprocess.Popen(
        ["kubectl", "port-forward", "deployment/fusion-service", "18091:8080"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    for _ in range(30):
        line = proc.stdout.readline()
        if "Forwarding from" in line:
            break
    time.sleep(1)
    return proc


def set_fusion_cpu(limit, request):
    run(["kubectl", "set", "resources", "deployment/fusion-service", "-c=fusion-service",
         f"--limits=cpu={limit},memory=128Mi", f"--requests=cpu={request},memory=64Mi"],
        capture_output=True, text=True)
    run(["kubectl", "rollout", "status", "deployment/fusion-service", "--timeout=90s"],
        capture_output=True, text=True)


def set_sensor_replicas(n):
    for name in DEPLOYMENTS:
        run(["kubectl", "scale", f"deployment/{name}", f"--replicas={n}"], capture_output=True, text=True)
    for name in DEPLOYMENTS:
        run(["kubectl", "rollout", "status", f"deployment/{name}", "--timeout=90s"], capture_output=True, text=True)


def kubectl_top_fusion():
    try:
        out = subprocess.run(
            ["kubectl", "top", "pod", "-l", "app=fusion-service", "--no-headers"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        parts = out.split()
        return parts[1] if len(parts) >= 2 else None
    except Exception:
        return None


def _cpu_millicores(cpu_str):
    if not cpu_str or not cpu_str.endswith("m"):
        return None
    try:
        return float(cpu_str[:-1])
    except ValueError:
        return None


def mean_std(values):
    if not values:
        return float("nan"), float("nan")
    if len(values) == 1:
        return values[0], 0.0
    return statistics.mean(values), statistics.stdev(values)


def settle(settle_s):
    # kubectl top is backed by metrics-server, which only refreshes its
    # cache on its own resync period (commonly ~15-60s) -- reading it
    # immediately after changing the CPU limit can return a stale sample
    # from before the new limit actually took effect, which is how an
    # earlier run of this script reported a CPU figure that looked like it
    # exceeded the newly-tightened limit. To avoid that, we poll
    # repeatedly through the settle window and keep only the most recent
    # sample, giving metrics-server time to catch up.
    print(f"[resource_limit_test] settling for {settle_s}s (polling CPU to let metrics-server catch up "
          f"to the new limit)...", flush=True)
    last_sample = None
    deadline = time.time() + settle_s
    while time.time() < deadline:
        sample = kubectl_top_fusion()
        if sample is not None:
            last_sample = sample
        time.sleep(min(5.0, max(0.0, deadline - time.time())) or 0.1)
    return last_sample


def measure_trial(label, trial, trials, duration_s, fallback_cpu):
    try:
        http_get("/timing-reset")
        print(f"[resource_limit_test] '{label}' trial {trial}/{trials}: measuring for {duration_s}s...",
              flush=True)
        time.sleep(duration_s)
        stats = http_get("/timing-stats")
    except OSError as e:
        # The retries inside http_get() themselves burn additional wall
        # time, so re-sample CPU now rather than reusing the settle-window
        # reading -- this is the freshest metrics-server data available
        # before we give up on this pod.
        cpu_after_stall = kubectl_top_fusion() or fallback_cpu
        print(f"[resource_limit_test] '{label}' trial {trial}/{trials}: fusion pod's HTTP endpoint never "
              f"responded under this CPU limit ({e}) -- treating this as a complete stall (effectively "
              f"100% miss rate). Last-known CPU reading: {cpu_after_stall or 'n/a'} (may still lag the "
              f"true throttled value by up to one metrics-server resync period).", flush=True)
        return {
            "total_cycles": 0, "miss_count": 0, "miss_rate_pct": 100.0,
            "avg_duration_ms": float("nan"), "worst_case_ms": float("nan"), "jitter_ms": float("nan"),
            "fusion_cpu": cpu_after_stall,
        }

    cpu = kubectl_top_fusion() or fallback_cpu
    row = {**stats, "fusion_cpu": cpu}
    print(f"[resource_limit_test] '{label}' trial {trial}/{trials} result: "
          f"miss_rate={row['miss_rate_pct']:.2f}% avg={row['avg_duration_ms']:.1f}ms cpu={cpu or 'n/a'}",
          flush=True)
    return row


def measure(label, settle_s, duration_s, trials):
    cpu_during_settle = settle(settle_s)
    trial_rows = [measure_trial(label, t, trials, duration_s, cpu_during_settle)
                  for t in range(1, trials + 1)]

    miss_rates = [t["miss_rate_pct"] for t in trial_rows]
    avg_durations = [t["avg_duration_ms"] for t in trial_rows if t["avg_duration_ms"] == t["avg_duration_ms"]]
    worst_cases = [t["worst_case_ms"] for t in trial_rows if t["worst_case_ms"] == t["worst_case_ms"]]
    jitters = [t["jitter_ms"] for t in trial_rows if t["jitter_ms"] == t["jitter_ms"]]
    cpus = [c for c in (_cpu_millicores(t["fusion_cpu"]) for t in trial_rows) if c is not None]

    miss_mean, miss_std = mean_std(miss_rates)
    avg_mean, avg_std = mean_std(avg_durations)
    worst_mean, worst_std = mean_std(worst_cases)
    jitter_mean, jitter_std = mean_std(jitters)
    cpu_mean, cpu_std = mean_std(cpus)

    row = {
        "label": label,
        "trials": trials,
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
    print(f"[resource_limit_test] '{label}' aggregate over {trials} trials: "
          f"miss_rate={miss_mean:.2f}+/-{miss_std:.2f}%", flush=True)
    return row


def restore(original_limit, original_request):
    print("\n[resource_limit_test] restoring original fusion CPU limits and sensor replicas...", flush=True)
    set_fusion_cpu(original_limit, original_request)
    set_sensor_replicas(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--original-limit", default="750m")
    parser.add_argument("--original-request", default="150m")
    parser.add_argument("--tight-limit", default="100m")
    parser.add_argument("--tight-request", default="50m")
    parser.add_argument("--settle-s", type=float, default=30.0)
    parser.add_argument("--duration-s", type=float, default=25.0)
    parser.add_argument("--trials", type=int, default=3,
                         help="independent measurement windows per limit level, reported as mean +/- stdev")
    args = parser.parse_args()

    fusion_pf = start_fusion_port_forward()
    results = []

    try:
        print(f"\n[resource_limit_test] scaling sensors to {FIXED_LOAD_MULTIPLIER}x replicas "
              f"(fixed load for this comparison)...", flush=True)
        set_sensor_replicas(FIXED_LOAD_MULTIPLIER)

        print(f"\n[resource_limit_test] === original limits (cpu limit={args.original_limit}) ===", flush=True)
        set_fusion_cpu(args.original_limit, args.original_request)
        results.append(measure("original", args.settle_s, args.duration_s, args.trials))

        print(f"\n[resource_limit_test] === tightened limits (cpu limit={args.tight_limit}) ===", flush=True)
        set_fusion_cpu(args.tight_limit, args.tight_request)
        results.append(measure("tightened", args.settle_s, args.duration_s, args.trials))
    finally:
        fusion_pf.terminate()
        restore(args.original_limit, args.original_request)

    write_csv(results)
    write_chart(results, args.original_limit, args.tight_limit)
    print_table(results)


def write_csv(results):
    path = "benchmarks/resource_limit_results.csv"
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        writer.writeheader()
        writer.writerows(results)
    print(f"\n[resource_limit_test] wrote {path}")


def write_chart(results, original_limit, tight_limit):
    labels = [f"original\n(cpu limit {original_limit})", f"tightened\n(cpu limit {tight_limit})"]
    miss_means = [r["miss_rate_pct_mean"] for r in results]
    miss_stds = [r["miss_rate_pct_std"] for r in results]
    worst_means = [r["worst_case_ms_mean"] for r in results]
    worst_stds = [r["worst_case_ms_std"] for r in results]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 5))

    ax1.bar(labels, miss_means, yerr=miss_stds, capsize=6, color=["#2563eb", "#dc2626"])
    ax1.set_ylabel("Deadline-miss rate (%)")
    ax1.set_title(f"Miss rate at fixed {FIXED_LOAD_MULTIPLIER}x sensor load\n"
                  f"(mean +/- stdev, n={results[0]['trials']} trials)")
    ax1.grid(True, axis="y", alpha=0.3)

    plotted_worst_case = [0.0 if (w != w) else w for w in worst_means]  # NaN -> 0 bar, annotated below
    plotted_worst_err = [0.0 if (w != w) else e for w, e in zip(worst_means, worst_stds)]
    bars = ax2.bar(labels, plotted_worst_case, yerr=plotted_worst_err, capsize=6, color=["#2563eb", "#dc2626"])
    ax2.axhline(100.0, color="black", linestyle="--", linewidth=1, label="100ms deadline")
    for bar, original_value in zip(bars, worst_means):
        if original_value != original_value:  # NaN check
            ax2.annotate("no response\n(pod too CPU-starved\nto answer HTTP)",
                         xy=(bar.get_x() + bar.get_width() / 2, 5), ha="center", color="#dc2626")
    ax2.set_ylabel("Worst-case cycle duration (ms)")
    ax2.set_title(f"Worst-case latency at fixed {FIXED_LOAD_MULTIPLIER}x sensor load\n"
                  f"(mean +/- stdev, n={results[0]['trials']} trials)")
    ax2.legend()
    ax2.grid(True, axis="y", alpha=0.3)

    fig.tight_layout()
    path = "benchmarks/resource_limit_chart.png"
    fig.savefig(path, dpi=150)
    print(f"[resource_limit_test] wrote {path}")


def print_table(results):
    print("\n| limits | trials | miss rate % (mean+/-std) | avg ms (mean+/-std) | "
          "worst-case ms (mean+/-std) | fusion CPU millicores (mean+/-std) |")
    print("|---|---|---|---|---|---|")
    for r in results:
        print(f"| {r['label']} | {r['trials']} | "
              f"{r['miss_rate_pct_mean']:.2f}+/-{r['miss_rate_pct_std']:.2f} | "
              f"{r['avg_duration_ms_mean']:.1f}+/-{r['avg_duration_ms_std']:.1f} | "
              f"{r['worst_case_ms_mean']:.1f}+/-{r['worst_case_ms_std']:.1f} | "
              f"{r['fusion_cpu_millicores_mean']:.0f}+/-{r['fusion_cpu_millicores_std']:.0f} |")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        print(f"[resource_limit_test] command failed: {e}", file=sys.stderr)
        sys.exit(1)
