"""
Deadline timing instrumentation for the fusion cycle loop.

Every fusion cycle is timestamped at start and end; TimingTracker classifies
each cycle as on-time or missed against the 100ms deadline, keeps running
statistics (miss rate, average duration, worst-case duration, jitter), and
appends a structured CSV row per cycle to a log file inside the pod.

The CSV is flushed periodically rather than after every single row: at 10
cycles/sec, an fsync-ing flush() on every row is needless syscall overhead
that, under a virtualized/overlay filesystem (as kind pods sit on), can
itself become a meaningful chunk of the pod's CPU budget and contribute to
CFS throttling -- exactly the kind of self-inflicted timing noise this
instrumentation exists to catch, not cause.
"""
import csv
import math
import os
import threading

DEADLINE_S = 0.1
FLUSH_EVERY_N_CYCLES = 20  # ~2s at 100ms/cycle


class TimingTracker:
    def __init__(self, log_path=None, deadline_s=DEADLINE_S):
        self.deadline_s = deadline_s
        self._lock = threading.Lock()

        self.total_cycles = 0
        self.miss_count = 0
        self.sum_duration = 0.0
        self.sum_duration_sq = 0.0
        self.max_duration = 0.0
        self._log_row_count = 0  # never reset, so CSV row numbering stays unique across resets

        self.log_path = log_path or os.environ.get("TIMING_LOG_PATH", "/app/timing_log.csv")
        self._csv_file = open(self.log_path, "w", newline="")
        self._csv_writer = csv.writer(self._csv_file)
        self._csv_writer.writerow(["cycle", "start", "end", "duration_ms", "on_time"])
        self._csv_file.flush()

    def record(self, cycle_start, cycle_end):
        duration = cycle_end - cycle_start
        on_time = duration <= self.deadline_s

        with self._lock:
            self.total_cycles += 1
            if not on_time:
                self.miss_count += 1
            self.sum_duration += duration
            self.sum_duration_sq += duration * duration
            self.max_duration = max(self.max_duration, duration)
            cycle_index = self.total_cycles
            self._log_row_count += 1
            log_row = self._log_row_count

            self._csv_writer.writerow([
                log_row, f"{cycle_start:.6f}", f"{cycle_end:.6f}",
                f"{duration * 1000:.3f}", on_time,
            ])
            if log_row % FLUSH_EVERY_N_CYCLES == 0:
                self._csv_file.flush()

        return {"cycle": cycle_index, "duration_ms": duration * 1000, "on_time": on_time}

    def reset(self):
        """Zero the running stats (but not the CSV log) so a caller -- e.g.
        the breaking-point benchmark -- can measure a clean window at a new
        load level without prior levels' cycles diluting the average."""
        with self._lock:
            self.total_cycles = 0
            self.miss_count = 0
            self.sum_duration = 0.0
            self.sum_duration_sq = 0.0
            self.max_duration = 0.0

    def stats(self):
        with self._lock:
            n = self.total_cycles
            if n == 0:
                return {
                    "total_cycles": 0, "miss_count": 0, "miss_rate_pct": 0.0,
                    "avg_duration_ms": 0.0, "worst_case_ms": 0.0, "jitter_ms": 0.0,
                }
            avg = self.sum_duration / n
            variance = max(0.0, self.sum_duration_sq / n - avg * avg)
            jitter = math.sqrt(variance)
            return {
                "total_cycles": n,
                "miss_count": self.miss_count,
                "miss_rate_pct": (self.miss_count / n) * 100.0,
                "avg_duration_ms": avg * 1000.0,
                "worst_case_ms": self.max_duration * 1000.0,
                "jitter_ms": jitter * 1000.0,
            }

    def close(self):
        with self._lock:
            self._csv_file.flush()
            self._csv_file.close()
