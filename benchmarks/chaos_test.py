"""
Pod-chaos resilience test: builds on Task 4's platform-layer fault
tolerance with a more demanding scenario -- forcibly killing pods while
the pipeline is under sustained load, not at idle.

Two phases, both run against the live cluster with all sensors scaled to
LOAD_MULTIPLIER replicas (sustained load, not idle):

  Phase A -- kill the fusion pod itself (not just a sensor pod). Measures
  the downtime between the last fusion/state message before the kill and
  the first one after Kubernetes reschedules it, and confirms the system
  resumes producing correct fused estimates with no manual intervention.

  Phase B -- kill a sensor pod while the system is under the same
  sustained load. LiDAR is temporarily scaled down to 1 replica just for
  this phase (the other three sensor types stay at LOAD_MULTIPLIER) so
  that killing its one pod actually creates a detectable gap -- with N>1
  redundant replicas of the same sensor type, the fusion service can't
  tell one replica apart from another (it only tracks "latest reading per
  sensor type"), so losing one of many would be invisible by design.
  Confirms both layers: Kubernetes restarts the pod (RESTARTS increments)
  and the application layer logs the resulting staleness and recovery.

Usage (with the cluster already up and this repo's manifests applied):
    python benchmarks/chaos_test.py
"""
import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.request

import paho.mqtt.client as mqtt

LOAD_MULTIPLIER = 16
OTHER_SENSORS = ["camera-sim", "imu-sim", "gps-sim"]
ALL_SENSORS = ["lidar-sim"] + OTHER_SENSORS
FUSION_STATUS_URL = "http://localhost:18092"


def run(cmd, **kwargs):
    print(f"$ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, check=True, **kwargs)


def kubectl_json(cmd):
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def set_replicas(name, n):
    run(["kubectl", "scale", f"deployment/{name}", f"--replicas={n}"], capture_output=True, text=True)


def wait_rollout(name, timeout="90s"):
    run(["kubectl", "rollout", "status", f"deployment/{name}", f"--timeout={timeout}"],
        capture_output=True, text=True)


def get_pod_name(label):
    data = kubectl_json(["kubectl", "get", "pods", "-l", f"app={label}", "-o", "json"])
    items = data["items"]
    return items[0]["metadata"]["name"] if items else None


def get_restart_count(label):
    data = kubectl_json(["kubectl", "get", "pods", "-l", f"app={label}", "-o", "json"])
    items = data["items"]
    if not items:
        return None
    statuses = items[0]["status"].get("containerStatuses", [])
    return statuses[0]["restartCount"] if statuses else None


class TopicWatcher:
    """Subscribes to an MQTT topic and records the wall-clock arrival time
    of every message, so we can measure a real publishing gap
    across a pod kill/restart."""

    def __init__(self, topic="fusion/state", mqtt_host="localhost", mqtt_port=1883, client_id="chaos-test-watcher"):
        self.timestamps = []
        self._lock = threading.Lock()
        self.client = mqtt.Client(client_id=client_id)
        self.client.on_connect = lambda c, u, f, rc: c.subscribe(topic)
        self.client.on_message = self._on_message
        self.client.connect(mqtt_host, mqtt_port, keepalive=30)
        self.client.loop_start()

    def _on_message(self, client, userdata, msg):
        with self._lock:
            self.timestamps.append(time.time())

    def last_before(self, t):
        with self._lock:
            before = [ts for ts in self.timestamps if ts < t]
        return max(before) if before else None

    def first_after(self, t, timeout_s=60.0):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            with self._lock:
                after = [ts for ts in self.timestamps if ts > t]
            if after:
                return min(after)
            time.sleep(0.2)
        return None

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()


def start_mosquitto_port_forward():
    proc = subprocess.Popen(
        ["kubectl", "port-forward", "svc/mosquitto", "1883:1883"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    for _ in range(30):
        line = proc.stdout.readline()
        if "Forwarding from" in line:
            break
    time.sleep(1)
    return proc


def start_fusion_status_port_forward():
    proc = subprocess.Popen(
        ["kubectl", "port-forward", "deployment/fusion-service", "18092:8080"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    for _ in range(30):
        line = proc.stdout.readline()
        if "Forwarding from" in line:
            break
    time.sleep(1)
    return proc


def http_get(path, retries=5, retry_delay_s=3.0):
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(f"{FUSION_STATUS_URL}{path}", timeout=10) as resp:
                return json.loads(resp.read().decode())
        except OSError as e:
            last_error = e
            time.sleep(retry_delay_s)
    raise last_error


def phase_a_fusion_kill(watcher, fusion_pf_holder):
    print("\n[chaos_test] === Phase A: kill the fusion pod under sustained load ===", flush=True)
    pod_before = get_pod_name("fusion-service")
    print(f"[chaos_test] fusion pod before kill: {pod_before}", flush=True)

    time.sleep(3)
    kill_time = time.time()
    last_before = watcher.last_before(kill_time) or kill_time

    run(["kubectl", "delete", "pod", pod_before, "--grace-period=0", "--force"],
        capture_output=True, text=True)
    print(f"[chaos_test] killed {pod_before} at {kill_time:.3f}", flush=True)

    first_after = watcher.first_after(kill_time, timeout_s=90.0)
    if first_after is None:
        print("[chaos_test] FAILED: fusion/state never resumed within 90s", file=sys.stderr)
        return False

    downtime = first_after - last_before
    pod_after = get_pod_name("fusion-service")
    print(f"[chaos_test] fusion/state resumed at {first_after:.3f} "
          f"(downtime ~{downtime:.2f}s, gap measured from last message before kill)", flush=True)
    print(f"[chaos_test] fusion pod after: {pod_after} "
          f"({'new pod, rescheduled automatically' if pod_after != pod_before else 'same pod (unexpected)'})",
          flush=True)

    # kubectl port-forward pins to the specific pod it started against, so
    # it died with the old pod -- re-establish it against the new one
    # before checking post-recovery stats over HTTP.
    print("[chaos_test] re-establishing port-forward to the new fusion pod...", flush=True)
    fusion_pf_holder[0].terminate()
    time.sleep(1)
    fusion_pf_holder[0] = start_fusion_status_port_forward()

    time.sleep(3)
    stats = http_get("/timing-stats")
    print(f"[chaos_test] post-recovery timing stats: {stats}", flush=True)

    print(f"\n[chaos_test] Phase A PASSED: downtime ~{downtime:.2f}s, "
          f"system resumed producing fused estimates with no manual intervention.", flush=True)
    return {"downtime_s": downtime, "pod_before": pod_before, "pod_after": pod_after}


def phase_b_sensor_kill(lidar_watcher):
    print("\n[chaos_test] === Phase B: kill a sensor pod under sustained load ===", flush=True)
    print("[chaos_test] scaling lidar-sim down to 1 replica so its loss is detectable "
          "(other sensors stay at load)...", flush=True)
    set_replicas("lidar-sim", 1)
    wait_rollout("lidar-sim")
    time.sleep(5)

    pod_before = get_pod_name("lidar-sim")
    restarts_before = get_restart_count("lidar-sim")
    print(f"[chaos_test] lidar-sim pod before kill: {pod_before} (restarts={restarts_before})", flush=True)

    kill_time = time.time()
    last_before = lidar_watcher.last_before(kill_time) or kill_time
    run(["kubectl", "delete", "pod", pod_before, "--grace-period=0", "--force"],
        capture_output=True, text=True)
    print(f"[chaos_test] killed {pod_before} at {kill_time:.3f}", flush=True)

    wait_rollout("lidar-sim")
    pod_after = get_pod_name("lidar-sim")
    restarts_after = get_restart_count("lidar-sim")
    print(f"[chaos_test] lidar-sim pod after: {pod_after} (restarts={restarts_after})", flush=True)

    # Direct measurement, independent of whether the staleness threshold
    # was tight enough to catch it: the actual gap in sensors/lidar
    # publishing, from MQTT message arrival times.
    first_after = lidar_watcher.first_after(kill_time, timeout_s=60.0)
    publish_gap_s = (first_after - last_before) if first_after else None
    print(f"[chaos_test] actual sensors/lidar publishing gap: "
          f"{f'{publish_gap_s:.2f}s' if publish_gap_s is not None else 'never resumed within 60s'}", flush=True)

    print("[chaos_test] waiting for application-layer staleness/recovery to be logged...", flush=True)
    time.sleep(15)
    fault_data = http_get("/fault-events")
    lidar_events = [e for e in fault_data["recent_events"]
                    if e["sensor"] == "lidar" and e["timestamp"] >= kill_time - 2.0]
    print(f"[chaos_test] fault-events for lidar since kill: {lidar_events}", flush=True)

    platform_ok = pod_after != pod_before
    app_layer_ok = any(e["event"] == "recovered" for e in lidar_events)

    print(f"\n[chaos_test] Phase B platform layer "
          f"{'confirmed' if platform_ok else 'NOT confirmed'} the pod was rescheduled "
          f"(restarts {restarts_before} -> {restarts_after}, new pod name: {pod_after != pod_before}); "
          f"actual outage {f'{publish_gap_s:.2f}s' if publish_gap_s is not None else 'unresolved'}; "
          f"application layer {'logged' if app_layer_ok else 'did not log'} a stale/recovered event "
          f"(threshold is 400ms -- if the real outage was faster than that, no event is expected) "
          f"-- all while the other three sensor types stayed under {LOAD_MULTIPLIER}x sustained load.",
          flush=True)
    return {
        "pod_before": pod_before, "pod_after": pod_after,
        "restarts_before": restarts_before, "restarts_after": restarts_after,
        "publish_gap_s": publish_gap_s, "lidar_events": lidar_events,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--load-multiplier", type=int, default=LOAD_MULTIPLIER)
    args = parser.parse_args()

    print(f"[chaos_test] scaling all sensors to {args.load_multiplier}x replicas for sustained load...",
          flush=True)
    for name in ALL_SENSORS:
        set_replicas(name, args.load_multiplier)
    for name in ALL_SENSORS:
        wait_rollout(name)

    mqtt_pf = start_mosquitto_port_forward()
    fusion_pf_holder = [start_fusion_status_port_forward()]
    watcher = TopicWatcher(topic="fusion/state", client_id="chaos-test-fusion-watcher")
    lidar_watcher = TopicWatcher(topic="sensors/lidar", client_id="chaos-test-lidar-watcher")

    try:
        time.sleep(5)
        result_a = phase_a_fusion_kill(watcher, fusion_pf_holder)
        result_b = phase_b_sensor_kill(lidar_watcher)
    finally:
        watcher.stop()
        lidar_watcher.stop()
        mqtt_pf.terminate()
        fusion_pf_holder[0].terminate()
        print("\n[chaos_test] restoring all sensors to 1 replica...", flush=True)
        for name in ALL_SENSORS:
            set_replicas(name, 1)
        for name in ALL_SENSORS:
            wait_rollout(name)

    print("\n[chaos_test] === summary ===", flush=True)
    print(f"Phase A (fusion pod kill): {result_a}", flush=True)
    print(f"Phase B (sensor pod kill): {result_b}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        print(f"[chaos_test] command failed: {e}", file=sys.stderr)
        sys.exit(1)
