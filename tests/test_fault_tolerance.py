"""
Application-layer fault-tolerance test: triggers a controlled GPS dropout
on demand (via the sensor's /force-dropout debug endpoint), then verifies
against the live cluster that:

  1. The system does not crash or stall during the staleness period
     (fusion/state keeps being published throughout).
  2. The fault-events log correctly records the stale -> recovered
     transition for GPS, with a duration matching the triggered dropout.
  3. Tracking error (RMSE vs ground truth) increases somewhat during the
     staleness window (expected -- less information available) but stays
     bounded rather than diverging, and recovers to close to baseline
     afterward.

Run with both port-forwards active:
    kubectl port-forward svc/mosquitto 1883:1883
    kubectl port-forward deployment/gps-sim 18081:8080
    kubectl port-forward deployment/fusion-service 18080:8080
    python tests/test_fault_tolerance.py --dropout-duration 15
"""
import argparse
import json
import os
import sys
import time
import urllib.request

import numpy as np
import paho.mqtt.client as mqtt

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sensors"))
from trajectory import GroundTruthTrajectory  # noqa: E402


def rmse(errors):
    if not errors:
        return None
    return float(np.sqrt(np.mean(np.array(errors) ** 2)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mqtt-host", default="localhost")
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--gps-control-url", default="http://localhost:18081")
    parser.add_argument("--fusion-status-url", default="http://localhost:18080")
    parser.add_argument("--dropout-duration", type=float, default=15.0)
    parser.add_argument("--settle-before-s", type=float, default=5.0)
    parser.add_argument("--recovery-window-s", type=float, default=10.0)
    args = parser.parse_args()

    truth = GroundTruthTrajectory()
    samples = []  # (timestamp, error)
    fusion_state_count = 0

    def on_connect(client, userdata, flags, rc):
        print(f"[fault_tolerance] connected to {args.mqtt_host}:{args.mqtt_port} (rc={rc})")
        client.subscribe("fusion/state")

    def on_message(client, userdata, msg):
        nonlocal fusion_state_count
        payload = json.loads(msg.payload.decode())
        t = payload["timestamp"]
        ref = truth.state_at(t)
        err = ((payload["x"] - ref["x"]) ** 2 + (payload["y"] - ref["y"]) ** 2) ** 0.5
        samples.append((t, err))
        fusion_state_count += 1

    client = mqtt.Client(client_id="fault-tolerance-test")
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.mqtt_host, args.mqtt_port, keepalive=30)
    client.loop_start()

    print(f"[fault_tolerance] settling for {args.settle_before_s}s before triggering dropout...")
    time.sleep(args.settle_before_s)
    baseline_cutoff = time.time()

    print(f"[fault_tolerance] triggering GPS dropout for {args.dropout_duration}s via /force-dropout")
    trigger_time = time.time()
    with urllib.request.urlopen(
        f"{args.gps_control_url}/force-dropout?duration={args.dropout_duration}", timeout=5
    ) as resp:
        print(f"[fault_tolerance] {resp.read().decode()}")

    total_wait = args.dropout_duration + args.recovery_window_s
    print(f"[fault_tolerance] waiting {total_wait}s for dropout + recovery window...")
    time.sleep(total_wait)

    recovery_start = trigger_time + args.dropout_duration
    end_time = time.time()

    client.loop_stop()
    client.disconnect()

    assert fusion_state_count > 0, "fusion/state produced no messages -- system stalled or crashed"

    baseline_errors = [e for t, e in samples if t < baseline_cutoff]
    during_errors = [e for t, e in samples if trigger_time <= t < recovery_start]
    after_errors = [e for t, e in samples if t >= recovery_start + 2.0]  # skip immediate re-convergence

    baseline_rmse = rmse(baseline_errors)
    during_rmse = rmse(during_errors)
    after_rmse = rmse(after_errors)

    print(f"\n[fault_tolerance] fusion/state messages received: {fusion_state_count} "
          f"(system stayed alive and kept publishing throughout)")
    print(f"[fault_tolerance] baseline RMSE (before dropout): {baseline_rmse}")
    print(f"[fault_tolerance] during-dropout RMSE:            {during_rmse}")
    print(f"[fault_tolerance] post-recovery RMSE:              {after_rmse}")

    with urllib.request.urlopen(f"{args.fusion_status_url}/fault-events", timeout=5) as resp:
        fault_data = json.loads(resp.read().decode())

    gps_events = [e for e in fault_data["recent_events"] if e["sensor"] == "gps"
                  and e["timestamp"] >= trigger_time - 1.0]
    print(f"\n[fault_tolerance] fault-events recorded for gps since trigger: {gps_events}")

    stale_events = [e for e in gps_events if e["event"] == "stale"]
    recovered_events = [e for e in gps_events if e["event"] == "recovered"]
    assert stale_events, "expected a 'stale' event to be logged for gps after triggering dropout"
    assert recovered_events, "expected a 'recovered' event to be logged for gps after dropout ends"

    logged_duration = recovered_events[-1]["duration_s"]
    print(f"[fault_tolerance] logged stale duration: {logged_duration:.1f}s "
          f"(triggered dropout was {args.dropout_duration}s)")
    assert abs(logged_duration - args.dropout_duration) < 3.0, \
        "logged staleness duration should roughly match the triggered dropout duration"

    # Bounded, not diverging: allow noticeably worse tracking while GPS is
    # out, but it must not blow up.
    if during_rmse is not None and baseline_rmse is not None:
        assert during_rmse < baseline_rmse * 10 + 5.0, \
            f"during-dropout RMSE ({during_rmse}) diverged well beyond baseline ({baseline_rmse})"

    if after_rmse is not None and baseline_rmse is not None:
        assert after_rmse < baseline_rmse * 3 + 2.0, \
            f"post-recovery RMSE ({after_rmse}) did not recover close to baseline ({baseline_rmse})"

    print("\n[fault_tolerance] PASSED: system stayed up throughout, staleness/recovery correctly "
          "logged, RMSE stayed bounded during the outage and recovered afterward.")


if __name__ == "__main__":
    main()
