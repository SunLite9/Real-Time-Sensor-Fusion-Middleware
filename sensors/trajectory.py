"""
Ground-truth vehicle trajectory shared by every sensor simulator.

All four sensors observe the same underlying vehicle motion so that fusion
accuracy can later be measured against a common reference. The path is a
slow racetrack-style loop: straight, curve, straight, curve, composed from
piecewise-constant curvature segments so that both position and velocity
are continuous and differentiable almost everywhere.
"""
import math
import time

SPEED_MPS = 8.0
STRAIGHT_LENGTH_M = 60.0
TURN_RADIUS_M = 20.0


class GroundTruthTrajectory:
    """Deterministic, time-parameterized closed-loop vehicle path.

    Call `state_at(t)` with a wall-clock or simulation timestamp to get the
    true (x, y, vx, vy) of the vehicle at that instant. The path is periodic
    so it can run indefinitely.

    `start_time` anchors the phase of the loop and defaults to epoch 0 (not
    "now") so that every sensor pod -- each started at a slightly different
    wall-clock moment -- computes the exact same ground truth for a given
    absolute timestamp. Anchoring to per-pod startup time would desync the
    "shared" ground truth across sensors, which defeats the point of having
    one.
    """

    def __init__(self, start_time=0.0):
        self.start_time = start_time

        half_turn_len = math.pi * TURN_RADIUS_M
        self._segments = [
            ("straight", STRAIGHT_LENGTH_M),
            ("turn", half_turn_len),
            ("straight", STRAIGHT_LENGTH_M),
            ("turn", half_turn_len),
        ]
        self._segment_time = [seg[1] / SPEED_MPS for seg in self._segments]
        self.period_s = sum(self._segment_time)

    def state_at(self, t=None):
        if t is None:
            t = time.time()
        elapsed = (t - self.start_time) % self.period_s

        # Both turns curve the same way (left), which is what makes this a
        # closed stadium/racetrack shape -- alternating turn direction would
        # trace an S-curve that never returns to the start point.
        x, y, heading = 0.0, 0.0, 0.0
        remaining = elapsed

        for (kind, length), seg_dur in zip(self._segments, self._segment_time):
            if remaining <= seg_dur:
                if kind == "straight":
                    dist = remaining * SPEED_MPS
                    x += dist * math.cos(heading)
                    y += dist * math.sin(heading)
                else:
                    angle = (remaining * SPEED_MPS) / TURN_RADIUS_M
                    cx = x - TURN_RADIUS_M * math.sin(heading)
                    cy = y + TURN_RADIUS_M * math.cos(heading)
                    new_heading = heading + angle
                    x = cx + TURN_RADIUS_M * math.sin(new_heading)
                    y = cy - TURN_RADIUS_M * math.cos(new_heading)
                    heading = new_heading
                vx = SPEED_MPS * math.cos(heading)
                vy = SPEED_MPS * math.sin(heading)
                return {"x": x, "y": y, "vx": vx, "vy": vy, "heading": heading}

            remaining -= seg_dur
            if kind == "straight":
                x += length * math.cos(heading)
                y += length * math.sin(heading)
            else:
                angle = length / TURN_RADIUS_M
                cx = x - TURN_RADIUS_M * math.sin(heading)
                cy = y + TURN_RADIUS_M * math.cos(heading)
                new_heading = heading + angle
                x = cx + TURN_RADIUS_M * math.sin(new_heading)
                y = cy - TURN_RADIUS_M * math.cos(new_heading)
                heading = new_heading

        vx = SPEED_MPS * math.cos(heading)
        vy = SPEED_MPS * math.sin(heading)
        return {"x": x, "y": y, "vx": vx, "vy": vy, "heading": heading}
