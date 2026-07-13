"""
Proves the deadline instrumentation itself is correct before relying on it
for the breaking-point benchmark later: runs a batch of fast cycles plus
one deliberately slow/blocking cycle (a real time.sleep injected to exceed
the 100ms deadline, not a faked duration value) through TimingTracker and
confirms it's classified and counted as a miss.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "fusion"))
from timing import TimingTracker, DEADLINE_S  # noqa: E402


def main():
    log_path = os.path.join(os.path.dirname(__file__), "_timing_stress_test.csv")
    tracker = TimingTracker(log_path=log_path)

    n_fast = 20
    for _ in range(n_fast):
        start = time.time()
        time.sleep(0.01)  # well under the 100ms deadline
        end = time.time()
        result = tracker.record(start, end)
        assert result["on_time"], f"expected fast cycle to be on-time, got {result}"

    # Deliberately stress the loop with a genuine blocking operation that
    # exceeds the deadline.
    start = time.time()
    time.sleep(DEADLINE_S + 0.08)
    end = time.time()
    stressed_result = tracker.record(start, end)
    assert not stressed_result["on_time"], f"expected stressed cycle to miss deadline, got {stressed_result}"

    stats = tracker.stats()
    print(f"[timing_stress] stats after {n_fast} fast cycles + 1 stressed cycle: {stats}")

    assert stats["total_cycles"] == n_fast + 1
    assert stats["miss_count"] == 1, f"expected exactly 1 missed deadline, got {stats['miss_count']}"
    expected_miss_rate = 100.0 / (n_fast + 1)
    assert abs(stats["miss_rate_pct"] - expected_miss_rate) < 1e-6
    assert stats["worst_case_ms"] >= (DEADLINE_S + 0.08) * 1000 * 0.95  # allow small scheduling slack

    tracker.close()

    with open(log_path) as f:
        rows = f.readlines()
    assert len(rows) == n_fast + 2, "expected header + one row per cycle in the CSV log"
    assert rows[-1].strip().endswith("False"), "expected the stressed cycle's CSV row to be logged as a miss"

    os.remove(log_path)
    print("[timing_stress] PASSED: instrumentation correctly detected and logged the missed deadline.")


if __name__ == "__main__":
    main()
