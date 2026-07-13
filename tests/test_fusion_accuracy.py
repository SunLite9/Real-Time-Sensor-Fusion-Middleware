"""
Fusion accuracy test: compares the fused state estimate (fusion/state) and
a single-sensor baseline (GPS-only, sensors/gps) against ground truth over
a sustained run against the live cluster, reporting RMSE for each.

Run from outside the cluster after port-forwarding the broker:
    kubectl port-forward svc/mosquitto 1883:1883
    python tests/test_fusion_accuracy.py --duration 30
"""
import argparse
import json
import os
import sys
import time

import numpy as np
import paho.mqtt.client as mqtt

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sensors"))
from trajectory import GroundTruthTrajectory  # noqa: E402


def rmse(errors):
    if not errors:
        return None
    arr = np.array(errors)
    return float(np.sqrt(np.mean(arr ** 2)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("MQTT_BROKER_HOST", "localhost"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MQTT_BROKER_PORT", "1883")))
    parser.add_argument("--duration", type=float, default=30.0)
    args = parser.parse_args()

    truth = GroundTruthTrajectory()
    fusion_errors = []
    gps_errors = []
    counts = {"fusion/state": 0, "sensors/gps": 0}

    def on_connect(client, userdata, flags, rc):
        print(f"[fusion_accuracy] connected to {args.host}:{args.port} (rc={rc})")
        client.subscribe("fusion/state")
        client.subscribe("sensors/gps")

    def on_message(client, userdata, msg):
        payload = json.loads(msg.payload.decode())
        t = payload["timestamp"]
        ref = truth.state_at(t)
        err = ((payload["x"] - ref["x"]) ** 2 + (payload["y"] - ref["y"]) ** 2) ** 0.5

        counts[msg.topic] += 1
        if msg.topic == "fusion/state":
            fusion_errors.append(err)
        elif msg.topic == "sensors/gps":
            gps_errors.append(err)

    client = mqtt.Client(client_id="fusion-accuracy-test")
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.host, args.port, keepalive=30)
    client.loop_start()

    print(f"[fusion_accuracy] listening for {args.duration}s...")
    time.sleep(args.duration)
    client.loop_stop()
    client.disconnect()

    fusion_rmse = rmse(fusion_errors)
    gps_rmse = rmse(gps_errors)

    print("\n[fusion_accuracy] results:")
    print(f"  fusion/state samples: {counts['fusion/state']}, RMSE vs ground truth: {fusion_rmse:.3f} m"
          if fusion_rmse is not None else "  fusion/state: no samples received")
    print(f"  sensors/gps  samples: {counts['sensors/gps']}, RMSE vs ground truth: {gps_rmse:.3f} m"
          if gps_rmse is not None else "  sensors/gps: no samples received")

    if fusion_rmse is not None and gps_rmse is not None:
        improvement = (1 - fusion_rmse / gps_rmse) * 100
        print(f"\n  fusion improves on GPS-only baseline by {improvement:.1f}%")

    if fusion_rmse is None or counts["fusion/state"] == 0:
        raise SystemExit("fusion_accuracy test FAILED: no fusion/state messages received")


if __name__ == "__main__":
    main()
