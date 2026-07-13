"""
Smoke test: subscribes to all four sensor topics via the Mosquitto broker
and confirms each is actively publishing.

Run from outside the cluster after port-forwarding the broker:
    kubectl port-forward svc/mosquitto 1883:1883
    python tests/mqtt_smoke_test.py
"""
import argparse
import json
import os
import time
from collections import defaultdict

import paho.mqtt.client as mqtt

TOPICS = ["sensors/lidar", "sensors/camera", "sensors/imu", "sensors/gps"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("MQTT_BROKER_HOST", "localhost"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MQTT_BROKER_PORT", "1883")))
    parser.add_argument("--duration", type=float, default=8.0, help="seconds to listen")
    args = parser.parse_args()

    counts = defaultdict(int)
    samples = {}

    def on_connect(client, userdata, flags, rc):
        print(f"[smoke_test] connected to {args.host}:{args.port} (rc={rc})")
        for topic in TOPICS:
            client.subscribe(topic)

    def on_message(client, userdata, msg):
        counts[msg.topic] += 1
        if msg.topic not in samples:
            samples[msg.topic] = msg.payload.decode()
        print(f"[{msg.topic}] {msg.payload.decode()}")

    client = mqtt.Client(client_id="mqtt-smoke-test")
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.host, args.port, keepalive=30)
    client.loop_start()

    time.sleep(args.duration)
    client.loop_stop()
    client.disconnect()

    print("\n[smoke_test] summary:")
    ok = True
    for topic in TOPICS:
        n = counts.get(topic, 0)
        status = "OK" if n > 0 else "NO MESSAGES"
        if n == 0:
            ok = False
        print(f"  {topic}: {n} messages received - {status}")

    if not ok:
        raise SystemExit("smoke test FAILED: one or more topics received no messages")
    print("\n[smoke_test] PASSED: all four sensor topics are publishing.")


if __name__ == "__main__":
    main()
