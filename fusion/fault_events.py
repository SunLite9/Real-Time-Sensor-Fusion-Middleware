"""
Application-layer fault-event tracking: detects when a sensor goes stale
(no fresh reading within its staleness threshold) and when it recovers,
logs both transitions to a CSV file inside the pod, and keeps an in-memory
tail of recent events for retrieval over HTTP.
"""
import csv
import os
import threading
import time


class FaultEventTracker:
    def __init__(self, log_path=None, max_recent=200):
        self._lock = threading.Lock()
        self._is_stale = {}      # sensor -> bool
        self._stale_since = {}   # sensor -> timestamp when it went stale
        self._recent = []        # bounded tail of event dicts, most-recent-last
        self._max_recent = max_recent

        self.log_path = log_path or os.environ.get("FAULT_LOG_PATH", "/app/fault_events.csv")
        self._csv_file = open(self.log_path, "w", newline="")
        self._csv_writer = csv.writer(self._csv_file)
        self._csv_writer.writerow(["sensor", "event", "timestamp", "duration_s"])
        self._csv_file.flush()

    def check(self, sensor, now, is_fresh):
        """Update staleness state for `sensor` given whether it currently
        has a fresh reading. Returns 'stale', 'recovered', or None."""
        with self._lock:
            was_stale = self._is_stale.get(sensor, False)

            if was_stale and is_fresh:
                duration = now - self._stale_since.pop(sensor, now)
                self._is_stale[sensor] = False
                self._log(sensor, "recovered", now, duration)
                return "recovered"

            if not was_stale and not is_fresh:
                self._is_stale[sensor] = True
                self._stale_since[sensor] = now
                self._log(sensor, "stale", now, 0.0)
                return "stale"

        return None

    def _log(self, sensor, event, timestamp, duration_s):
        self._csv_writer.writerow([sensor, event, f"{timestamp:.6f}", f"{duration_s:.3f}"])
        self._csv_file.flush()
        entry = {"sensor": sensor, "event": event, "timestamp": timestamp, "duration_s": duration_s}
        self._recent.append(entry)
        if len(self._recent) > self._max_recent:
            self._recent.pop(0)
        print(f"[fault_events] {sensor} {event} at {timestamp:.3f}"
              + (f" (stale for {duration_s:.1f}s)" if event == "recovered" else ""), flush=True)

    def current_status(self):
        with self._lock:
            return dict(self._is_stale)

    def recent_events(self):
        with self._lock:
            return list(self._recent)

    def close(self):
        with self._lock:
            self._csv_file.close()
