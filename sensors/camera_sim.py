"""
Camera simulator.

Publishes a simulated visual position/velocity estimate at a fast ~30Hz
(typical machine-vision frame rate). Noise characteristics are distinct from
LiDAR: cameras are good at lateral/angular position (rich pixel resolution)
but comparatively poor at estimating distance/depth from a monocular view,
so distance noise is larger than LiDAR's while lateral noise is smaller.
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
TOPIC = "sensors/camera"
PUBLISH_RATE_HZ = float(os.environ.get("PUBLISH_RATE_HZ", "30.0"))

RANGE_NOISE_STD = 0.9
LATERAL_NOISE_STD = 0.05
VELOCITY_NOISE_STD = 0.4


def main():
    start_control_server(SensorControlState(crash_enabled=crash_enabled_from_env()))

    client = mqtt.Client(client_id=f"camera-sim-{os.environ.get('HOSTNAME', os.getpid())}")
    client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, keepalive=30)
    client.loop_start()

    trajectory = GroundTruthTrajectory()
    period = 1.0 / PUBLISH_RATE_HZ

    print(f"[camera_sim] publishing to {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT} "
          f"topic={TOPIC} rate={PUBLISH_RATE_HZ}Hz", flush=True)

    while True:
        cycle_start = time.time()
        truth = trajectory.state_at(cycle_start)
        heading = truth["heading"]

        range_err = random.gauss(0, RANGE_NOISE_STD)
        lateral_err = random.gauss(0, LATERAL_NOISE_STD)
        noisy_x = truth["x"] + range_err * math.cos(heading) - lateral_err * math.sin(heading)
        noisy_y = truth["y"] + range_err * math.sin(heading) + lateral_err * math.cos(heading)

        noisy_vx = truth["vx"] + random.gauss(0, VELOCITY_NOISE_STD)
        noisy_vy = truth["vy"] + random.gauss(0, VELOCITY_NOISE_STD)

        payload = {
            "sensor": "camera",
            "timestamp": cycle_start,
            "x": noisy_x,
            "y": noisy_y,
            "vx": noisy_vx,
            "vy": noisy_vy,
        }
        client.publish(TOPIC, json.dumps(payload))

        elapsed = time.time() - cycle_start
        time.sleep(max(0.0, period - elapsed))


if __name__ == "__main__":
    main()
