"""
Shared platform-layer control server for the sensor simulators.

Every sensor exposes this on a small HTTP server, separate from its MQTT
publish loop:

  GET /healthz        -- liveness probe target for Kubernetes. Always
                          returns 200 while the process's main loop is
                          alive; used by each sensor Deployment's
                          livenessProbe so Kubernetes detects a hung or
                          dead sensor and restarts the pod automatically.

  GET /crash           -- deliberately exits the process, simulating a
                          crashed sensor pod for fault-tolerance testing.
                          Gated behind CRASH_ENDPOINT_ENABLED so it can
                          never be reachable unless a deployment
                          explicitly opts in -- this is a test-only
                          feature and must never be enabled outside a
                          local/test cluster.

  GET /force-dropout    -- (LiDAR/GPS only) immediately puts the sensor
                          into a dropout window of ?duration=N seconds
                          (default 5), on demand, without needing to
                          restart the pod -- used to drive the
                          application-layer staleness test on command
                          during a live run.
"""
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer


class SensorControlState:
    def __init__(self, crash_enabled=False):
        self._lock = threading.Lock()
        self.crash_enabled = crash_enabled
        self._manual_dropout_until = 0.0

    def trigger_dropout(self, duration_s):
        with self._lock:
            self._manual_dropout_until = time.time() + duration_s

    def in_manual_dropout(self, now):
        with self._lock:
            return now < self._manual_dropout_until


def start_control_server(state, port=8080):
    class ControlHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)

            if parsed.path == "/healthz":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            elif parsed.path == "/force-dropout":
                qs = urllib.parse.parse_qs(parsed.query)
                duration = float(qs.get("duration", ["5"])[0])
                state.trigger_dropout(duration)
                body = f"dropout triggered for {duration}s".encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            elif parsed.path == "/crash":
                if not state.crash_enabled:
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(b"crash endpoint disabled (test-only feature)")
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"crashing")
                # Exit after the response flushes rather than mid-handler.
                threading.Thread(target=_delayed_exit, daemon=True).start()

            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            pass  # silence default per-request access logging

    server = HTTPServer(("0.0.0.0", port), ControlHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _delayed_exit():
    time.sleep(0.05)
    os._exit(1)


def crash_enabled_from_env():
    return os.environ.get("CRASH_ENDPOINT_ENABLED", "false").lower() == "true"
