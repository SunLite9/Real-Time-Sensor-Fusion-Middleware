"""
GPS simulator.

Publishes simulated absolute position at a low rate (~1-5Hz), as a real GPS
receiver would (its fix rate is far below the other sensors'). Noise is
lower than LiDAR/camera range noise (GPS gives a genuinely absolute fix,
not a relative one) but GPS realistically loses signal periodically --
this simulator randomly enters a dropout state and skips publishing for a
stretch of time, then resumes, to make later fault-handling testing
realistic.
"""
import json
import os
import random
import time

import paho.mqtt.client as mqtt

from control_server import SensorControlState, crash_enabled_from_env, start_control_server
from trajectory import GroundTruthTrajectory

MQTT_BROKER_HOST = os.environ.get("MQTT_BROKER_HOST", "mosquitto")
MQTT_BROKER_PORT = int(os.environ.get("MQTT_BROKER_PORT", "1883"))
TOPIC = "sensors/gps"
PUBLISH_RATE_HZ = float(os.environ.get("PUBLISH_RATE_HZ", "2.0"))

POSITION_NOISE_STD = 0.5

# Dropout behavior: on average one dropout roughly every DROPOUT_MEAN_INTERVAL_S
# seconds of operation, lasting DROPOUT_MEAN_DURATION_S seconds.
DROPOUT_MEAN_INTERVAL_S = 45.0
DROPOUT_MEAN_DURATION_S = 4.0
DROPOUT_CHECK_PERIOD_S = 1.0


def main():
    control_state = SensorControlState(crash_enabled=crash_enabled_from_env())
    start_control_server(control_state)

    client = mqtt.Client(client_id=f"gps-sim-{os.environ.get('HOSTNAME', os.getpid())}")
    client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, keepalive=30)
    client.loop_start()

    trajectory = GroundTruthTrajectory()
    period = 1.0 / PUBLISH_RATE_HZ

    print(f"[gps_sim] publishing to {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT} "
          f"topic={TOPIC} rate={PUBLISH_RATE_HZ}Hz", flush=True)

    dropout_until = 0.0
    next_dropout_check = time.time() + DROPOUT_CHECK_PERIOD_S

    while True:
        cycle_start = time.time()

        if cycle_start >= next_dropout_check:
            next_dropout_check = cycle_start + DROPOUT_CHECK_PERIOD_S
            # Poisson-ish trigger: probability per check of starting a dropout.
            if cycle_start >= dropout_until and random.random() < (
                DROPOUT_CHECK_PERIOD_S / DROPOUT_MEAN_INTERVAL_S
            ):
                duration = random.expovariate(1.0 / DROPOUT_MEAN_DURATION_S)
                dropout_until = cycle_start + duration
                print(f"[gps_sim] entering dropout for {duration:.1f}s", flush=True)

        if cycle_start < dropout_until or control_state.in_manual_dropout(cycle_start):
            time.sleep(min(period, DROPOUT_CHECK_PERIOD_S))
            continue

        truth = trajectory.state_at(cycle_start)
        payload = {
            "sensor": "gps",
            "timestamp": cycle_start,
            "x": truth["x"] + random.gauss(0, POSITION_NOISE_STD),
            "y": truth["y"] + random.gauss(0, POSITION_NOISE_STD),
        }
        client.publish(TOPIC, json.dumps(payload))

        elapsed = time.time() - cycle_start
        time.sleep(max(0.0, period - elapsed))


if __name__ == "__main__":
    main()
