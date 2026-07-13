"""
LiDAR simulator.

Publishes a simplified point-cloud-derived position estimate (we skip full
point-cloud synthesis and instead emit the (x, y) of the nearest obstacle
return, which for this simulation is just the vehicle's own position) at a
realistic ~10Hz. LiDAR here is modeled as fairly accurate but with a bit
more noise on the distance axis than on lateral position, and it publishes
comparatively infrequently (a scan-rate limitation of a real spinning LiDAR).
"""
import json
import math
import os
import random
import time

import paho.mqtt.client as mqtt

from control_server import SensorControlState, crash_enabled_from_env, start_control_server
from trajectory import GroundTruthTrajectory

MQTT_BROKER_HOST = os.environ.get("MQTT_BROKER_HOST", "mosquitto")
MQTT_BROKER_PORT = int(os.environ.get("MQTT_BROKER_PORT", "1883"))
TOPIC = "sensors/lidar"
PUBLISH_RATE_HZ = float(os.environ.get("PUBLISH_RATE_HZ", "10.0"))

# Noise: LiDAR is precise laterally, noisier on range/distance.
RANGE_NOISE_STD = 0.35
LATERAL_NOISE_STD = 0.08


def main():
    control_state = SensorControlState(crash_enabled=crash_enabled_from_env())
    start_control_server(control_state)

    # HOSTNAME (the pod name, set automatically by Kubernetes) keeps the
    # client ID unique when this Deployment is scaled to multiple replicas
    # -- MQTT brokers disconnect any earlier client using the same ID.
    client = mqtt.Client(client_id=f"lidar-sim-{os.environ.get('HOSTNAME', os.getpid())}")
    client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, keepalive=30)
    client.loop_start()

    trajectory = GroundTruthTrajectory()
    period = 1.0 / PUBLISH_RATE_HZ

    print(f"[lidar_sim] publishing to {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT} "
          f"topic={TOPIC} rate={PUBLISH_RATE_HZ}Hz", flush=True)

    while True:
        cycle_start = time.time()

        if control_state.in_manual_dropout(cycle_start):
            time.sleep(min(period, 1.0))
            continue

        truth = trajectory.state_at(cycle_start)

        # Decompose noise into along-heading (range-like) and
        # perpendicular-to-heading (lateral) components.
        heading = truth["heading"]
        range_err = random.gauss(0, RANGE_NOISE_STD)
        lateral_err = random.gauss(0, LATERAL_NOISE_STD)
        noisy_x = truth["x"] + range_err * math.cos(heading) - lateral_err * math.sin(heading)
        noisy_y = truth["y"] + range_err * math.sin(heading) + lateral_err * math.cos(heading)

        payload = {
            "sensor": "lidar",
            "timestamp": cycle_start,
            "x": noisy_x,
            "y": noisy_y,
        }
        client.publish(TOPIC, json.dumps(payload))

        elapsed = time.time() - cycle_start
        time.sleep(max(0.0, period - elapsed))


if __name__ == "__main__":
    main()
