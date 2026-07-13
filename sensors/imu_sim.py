"""
IMU simulator.

Publishes simulated linear acceleration and angular velocity at a high rate
(~100Hz+), as a real MEMS IMU would. IMU readings are derivative
quantities (acceleration/angular rate, not position), are comparatively
noisy, and accumulate a small slowly-varying bias/drift over time -- a
realistic MEMS characteristic that downstream fusion needs to account for.
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
TOPIC = "sensors/imu"
PUBLISH_RATE_HZ = float(os.environ.get("PUBLISH_RATE_HZ", "100.0"))

ACCEL_NOISE_STD = 0.25
GYRO_NOISE_STD = 0.03
BIAS_DRIFT_STD_PER_S = 0.002  # random-walk bias drift rate


def main():
    start_control_server(SensorControlState(crash_enabled=crash_enabled_from_env()))

    client = mqtt.Client(client_id=f"imu-sim-{os.environ.get('HOSTNAME', os.getpid())}")
    client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, keepalive=30)
    client.loop_start()

    trajectory = GroundTruthTrajectory()
    period = 1.0 / PUBLISH_RATE_HZ

    print(f"[imu_sim] publishing to {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT} "
          f"topic={TOPIC} rate={PUBLISH_RATE_HZ}Hz", flush=True)

    accel_bias_x = 0.0
    accel_bias_y = 0.0
    prev_state = trajectory.state_at(time.time())
    prev_t = time.time()

    while True:
        cycle_start = time.time()
        dt = max(1e-3, cycle_start - prev_t)
        truth = trajectory.state_at(cycle_start)

        # Approximate true acceleration via finite difference of velocity.
        true_ax = (truth["vx"] - prev_state["vx"]) / dt
        true_ay = (truth["vy"] - prev_state["vy"]) / dt
        true_omega = (truth["heading"] - prev_state["heading"]) / dt

        # Bias random-walks slowly over time.
        accel_bias_x += random.gauss(0, BIAS_DRIFT_STD_PER_S * dt)
        accel_bias_y += random.gauss(0, BIAS_DRIFT_STD_PER_S * dt)

        payload = {
            "sensor": "imu",
            "timestamp": cycle_start,
            "ax": true_ax + accel_bias_x + random.gauss(0, ACCEL_NOISE_STD),
            "ay": true_ay + accel_bias_y + random.gauss(0, ACCEL_NOISE_STD),
            "omega": true_omega + random.gauss(0, GYRO_NOISE_STD),
        }
        client.publish(TOPIC, json.dumps(payload))

        prev_state = truth
        prev_t = cycle_start

        elapsed = time.time() - cycle_start
        time.sleep(max(0.0, period - elapsed))


if __name__ == "__main__":
    main()
