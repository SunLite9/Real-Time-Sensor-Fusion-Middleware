"""
Fusion service: subscribes to all four sensor MQTT topics, runs the EKF on
a fixed 100ms cycle, and publishes the fused state estimate.

Cycle structure (every 100ms):
  1. Predict -- advance the state using elapsed time and the latest IMU
     reading as a control input (tangential accel + yaw rate).
  2. Update -- for each of lidar/camera/gps, if a *new* reading (one not
     already consumed) has arrived within its staleness threshold, apply
     that sensor's measurement update.
  3. Publish the resulting state estimate to fusion/state.

Async / out-of-order handling: each sensor's buffer slot only accepts a
new reading if its timestamp is newer than what's already buffered, so a
message that arrives late (delayed in the broker/network) but is older
than what's already been processed is dropped rather than corrupting the
estimate. A per-sensor "last consumed" timestamp additionally prevents the
same reading from being applied twice across cycles.

Application-layer fault tolerance: a sensor with no fresh reading within
its staleness threshold is excluded from that cycle's update (or, for the
IMU, its control-input contribution is zeroed so the filter coasts on the
last known heading/speed) rather than being fed a stale reading. On the
stale transition, FaultEventTracker logs the event and the EKF's
uncertainty for the state components that sensor primarily constrains is
inflated once, on top of the steady per-cycle growth already contributed
by process noise while updates are skipped. When the sensor's data
resumes, the very next fresh update naturally pulls the estimate back
in via the normal Kalman gain -- no separate "resume trust" step is
needed, since a fresh update always trusts the sensor's stated
measurement noise, regardless of what happened while it was stale.
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import paho.mqtt.client as mqtt

from ekf import ExtendedKalmanFilter
from fault_events import FaultEventTracker
from timing import TimingTracker

MQTT_BROKER_HOST = os.environ.get("MQTT_BROKER_HOST", "mosquitto")
MQTT_BROKER_PORT = int(os.environ.get("MQTT_BROKER_PORT", "1883"))
STATE_TOPIC = "fusion/state"
SENSOR_TOPICS = ["sensors/lidar", "sensors/camera", "sensors/imu", "sensors/gps"]

CYCLE_PERIOD_S = 0.1
HTTP_PORT = int(os.environ.get("TIMING_HTTP_PORT", "8080"))
STATUS_LOG_INTERVAL_CYCLES = 50  # ~5s at 100ms/cycle

# Expected publish interval per sensor, used to derive staleness thresholds.
EXPECTED_INTERVAL_S = {
    "lidar": 1.0 / 10.0,
    "camera": 1.0 / 30.0,
    "imu": 1.0 / 100.0,
    "gps": 1.0 / 2.0,
}
STALENESS_MULTIPLIER = 4.0  # a reading older than N x its expected interval is unusable
# Floor on the multiplier-derived threshold: at 100Hz, 4x the IMU's ~10ms
# interval is only 40ms, which real MQTT/network/scheduling jitter on the
# cluster can exceed even when the sensor is perfectly healthy, causing
# false-positive stale/recovery flapping. A 150ms floor absorbs that
# jitter while still catching a genuinely dead sensor within one or two
# fusion cycles.
MIN_STALENESS_THRESHOLD_S = 0.15


def _staleness_threshold(sensor):
    return max(EXPECTED_INTERVAL_S[sensor] * STALENESS_MULTIPLIER, MIN_STALENESS_THRESHOLD_S)

# State-vector indices ([x, y, v, heading]) each sensor primarily
# constrains, used to widen uncertainty there when that sensor goes stale.
STALE_WIDEN_INDICES = {
    "lidar": [0, 1],
    "gps": [0, 1],
    "camera": [0, 1, 2, 3],
    "imu": [2, 3],
}
STALE_WIDEN_FACTOR = 4.0

# Measurement noise covariances, matched to each simulator's injected noise.
R_LIDAR = [[0.35 ** 2, 0.0], [0.0, 0.08 ** 2]]
R_GPS = [[0.5 ** 2, 0.0], [0.0, 0.5 ** 2]]
R_CAMERA = [
    [0.9 ** 2, 0.0, 0.0, 0.0],
    [0.0, 0.9 ** 2, 0.0, 0.0],
    [0.0, 0.0, 0.4 ** 2, 0.0],
    [0.0, 0.0, 0.0, 0.4 ** 2],
]


class SensorBuffer:
    """Thread-safe latest-reading-per-sensor buffer shared between the MQTT
    network thread (writer) and the fusion cycle thread (reader)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._readings = {}       # sensor -> dict payload
        self._consumed_ts = {}    # sensor -> timestamp of last-applied reading

    def offer(self, sensor, payload):
        with self._lock:
            current = self._readings.get(sensor)
            if current is None or payload["timestamp"] > current["timestamp"]:
                self._readings[sensor] = payload

    def take_fresh(self, sensor, now, max_age_s):
        """Return a reading for `sensor` if it exists, is not older than
        max_age_s, and hasn't already been consumed -- else None."""
        with self._lock:
            reading = self._readings.get(sensor)
            if reading is None:
                return None
            if now - reading["timestamp"] > max_age_s:
                return None
            if self._consumed_ts.get(sensor) == reading["timestamp"]:
                return None
            self._consumed_ts[sensor] = reading["timestamp"]
            return reading

    def latest(self, sensor):
        with self._lock:
            return self._readings.get(sensor)


def _make_status_handler(timing_tracker, fault_tracker):
    class StatusHandler(BaseHTTPRequestHandler):
        def _send_json(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/timing-stats":
                self._send_json(timing_tracker.stats())
            elif self.path == "/fault-events":
                self._send_json({
                    "current_status": fault_tracker.current_status(),
                    "recent_events": fault_tracker.recent_events(),
                })
            elif self.path == "/timing-reset":
                timing_tracker.reset()
                self._send_json({"reset": True})
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            pass  # silence default per-request access logging

    return StatusHandler


class FusionService:
    def __init__(self):
        self.buffer = SensorBuffer()
        self.ekf = ExtendedKalmanFilter(initial_state=[0.0, 0.0, 0.0, 0.0])
        self.timing = TimingTracker()
        self.faults = FaultEventTracker()
        self.client = mqtt.Client(client_id="fusion-service")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self._last_cycle_time = None

    def _on_connect(self, client, userdata, flags, rc):
        print(f"[fusion_service] connected to {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT} (rc={rc})", flush=True)
        for topic in SENSOR_TOPICS:
            client.subscribe(topic)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except (ValueError, UnicodeDecodeError):
            return
        sensor = payload.get("sensor")
        if sensor:
            self.buffer.offer(sensor, payload)

    def _run_cycle(self, now):
        cycle_wall_start = time.time()

        dt = CYCLE_PERIOD_S if self._last_cycle_time is None else now - self._last_cycle_time
        self._last_cycle_time = now

        freshness = {}
        latest_readings = {}
        for sensor in ("lidar", "camera", "imu", "gps"):
            reading = self.buffer.latest(sensor)
            latest_readings[sensor] = reading
            max_age = _staleness_threshold(sensor)
            is_fresh = reading is not None and (now - reading["timestamp"]) <= max_age
            freshness[sensor] = is_fresh
            transition = self.faults.check(sensor, now, is_fresh)
            if transition == "stale":
                self.ekf.widen_uncertainty(STALE_WIDEN_INDICES[sensor], STALE_WIDEN_FACTOR)

        imu = latest_readings["imu"]
        ax, ay, omega = 0.0, 0.0, 0.0
        if imu is not None and freshness["imu"]:
            ax, ay, omega = imu["ax"], imu["ay"], imu["omega"]

        self.ekf.predict(dt, ax=ax, ay=ay, omega=omega)

        if freshness["lidar"]:
            lidar = self.buffer.take_fresh("lidar", now, _staleness_threshold("lidar"))
            if lidar is not None:
                self.ekf.update_position([lidar["x"], lidar["y"]], R_LIDAR)

        if freshness["gps"]:
            gps = self.buffer.take_fresh("gps", now, _staleness_threshold("gps"))
            if gps is not None:
                self.ekf.update_position([gps["x"], gps["y"]], R_GPS)

        if freshness["camera"]:
            camera = self.buffer.take_fresh("camera", now, _staleness_threshold("camera"))
            if camera is not None:
                self.ekf.update_camera([camera["x"], camera["y"], camera["vx"], camera["vy"]], R_CAMERA)

        x, y = self.ekf.position()
        vx, vy = self.ekf.velocity()
        state_msg = {
            "timestamp": now,
            "x": x,
            "y": y,
            "vx": vx,
            "vy": vy,
        }
        self.client.publish(STATE_TOPIC, json.dumps(state_msg))

        cycle_wall_end = time.time()
        result = self.timing.record(cycle_wall_start, cycle_wall_end)

        if result["cycle"] % STATUS_LOG_INTERVAL_CYCLES == 0:
            stats = self.timing.stats()
            print(
                f"[fusion_service] timing: cycles={stats['total_cycles']} "
                f"miss_rate={stats['miss_rate_pct']:.2f}% "
                f"worst_case={stats['worst_case_ms']:.1f}ms "
                f"avg={stats['avg_duration_ms']:.1f}ms "
                f"jitter={stats['jitter_ms']:.1f}ms",
                flush=True,
            )

    def _start_http_server(self):
        handler_cls = _make_status_handler(self.timing, self.faults)
        server = HTTPServer(("0.0.0.0", HTTP_PORT), handler_cls)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print(f"[fusion_service] HTTP endpoints on :{HTTP_PORT} -- "
              f"/timing-stats, /timing-reset, /fault-events", flush=True)

    def run(self):
        self._start_http_server()
        self.client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, keepalive=30)
        self.client.loop_start()
        print(f"[fusion_service] running 100ms fusion cycle, publishing to {STATE_TOPIC}", flush=True)

        next_cycle = time.time()
        while True:
            now = time.time()
            self._run_cycle(now)
            next_cycle += CYCLE_PERIOD_S
            sleep_for = next_cycle - time.time()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                next_cycle = time.time()  # cycle overran; resync rather than spin-catch-up


if __name__ == "__main__":
    FusionService().run()
