"""
Extended Kalman Filter for the sensor-fusion state estimate.

Why an EKF and not a plain Kalman Filter
-----------------------------------------
The vehicle state is tracked in a kinematic *unicycle* form -- position,
scalar speed, and heading -- rather than raw (x, y, vx, vy). This is the
natural state for a steered ground vehicle (a real self-driving car's speed
and heading are what its actuators actually control), but it makes both
halves of the filter nonlinear:

1. Prediction is nonlinear: x' = x + v*cos(heading)*dt maps state to next
   state through cos/sin of heading, so the state-transition Jacobian F
   must be recomputed at every step (a plain KF's F is a constant matrix).
2. The camera's measurement is nonlinear: the camera reports (x, y, vx, vy)
   in world-frame Cartesian velocity, but our state only carries speed and
   heading, so vx = v*cos(heading), vy = v*sin(heading) -- again a
   trig function of the state, requiring the measurement Jacobian H to be
   linearized at the current estimate on every update.

LiDAR and GPS measure position directly, which happens to be a linear
function of this state (H is just a row-select matrix) -- but the EKF
machinery (Jacobian-based update) is applied uniformly to every sensor so
the same code path handles it regardless.

State vector: [x, y, v, heading]
  x, y      -- position (m)
  v         -- scalar forward speed (m/s)
  heading   -- heading angle (rad)

IMU as a control input, not a measurement
------------------------------------------
The IMU is folded into the *prediction* step as a control input rather than
treated as a fourth measurement update: its angular rate directly drives
the heading's rate of change, and its raw (ax, ay) acceleration is
projected onto the current heading estimate to drive the speed's rate of
change. This mirrors how a real INS/GPS fusion stack works -- the IMU
propagates state at high rate between low-rate corrections from position
sensors.

Because that projection (ax*cos(heading) + ay*sin(heading)) is itself a
function of the heading state, it happens inside predict() rather than
being precomputed by the caller -- that's what lets F carry
d(v_pred)/d(heading) (see the (2, 3) entry below). Precomputing the
projection outside the filter and passing in a scalar tangential
acceleration would silently drop that term from F, understating the
covariance growth on `v` whenever the heading estimate is off.
"""
import math

import numpy as np


class ExtendedKalmanFilter:
    STATE_DIM = 4  # [x, y, v, heading]

    def __init__(self, initial_state=None, initial_covariance=None,
                 process_noise_std=None):
        self.x = np.array(initial_state, dtype=float) if initial_state is not None \
            else np.zeros(self.STATE_DIM)
        self.P = np.array(initial_covariance, dtype=float) if initial_covariance is not None \
            else np.eye(self.STATE_DIM) * 10.0

        # Per-state process noise std devs: [x, y, v, heading].
        std = process_noise_std or [0.05, 0.05, 0.3, 0.05]
        self._base_Q = np.diag(np.array(std) ** 2)

    # ---- Prediction (nonlinear motion model, IMU as control input) ----

    def predict(self, dt, ax=0.0, ay=0.0, omega=0.0):
        x, y, v, heading = self.x
        cos_h, sin_h = math.cos(heading), math.sin(heading)
        accel_tangential = ax * cos_h + ay * sin_h

        x_pred = x + v * cos_h * dt
        y_pred = y + v * sin_h * dt
        v_pred = v + accel_tangential * dt
        # Wrap to (-pi, pi] so heading stays meaningful over long-running
        # deployments instead of accumulating without bound.
        heading_pred = math.atan2(math.sin(heading + omega * dt), math.cos(heading + omega * dt))

        # d(v_pred)/d(heading): the raw (ax, ay) projection onto heading
        # makes v_pred depend on heading too, not just on v -- see the
        # module docstring for why this has to live here rather than in a
        # precomputed scalar passed in from outside.
        dv_dheading = dt * (-ax * sin_h + ay * cos_h)

        F = np.array([
            [1.0, 0.0, cos_h * dt, -v * sin_h * dt],
            [0.0, 1.0, sin_h * dt,  v * cos_h * dt],
            [0.0, 0.0, 1.0,         dv_dheading],
            [0.0, 0.0, 0.0,         1.0],
        ])

        self.x = np.array([x_pred, y_pred, v_pred, heading_pred])
        Q = self._base_Q * dt
        self.P = F @ self.P @ F.T + Q

    # ---- Measurement updates (each sensor's nonlinear h(x) + Jacobian) ----

    def _apply_update(self, z, h_x, H, R):
        z = np.array(z, dtype=float)
        y = z - h_x  # innovation
        S = H @ self.P @ H.T + R
        K = self.P @ H.T @ np.linalg.inv(S)
        self.x = self.x + K @ y
        I = np.eye(self.STATE_DIM)
        self.P = (I - K @ H) @ self.P

    def update_position(self, z_xy, R):
        """LiDAR/GPS-style update: direct (x, y) position measurement.

        h(x) = [x, y] is linear in this state, so H is a constant
        row-select matrix -- the degenerate case of the general EKF update.
        """
        x, y, v, heading = self.x
        h_x = np.array([x, y])
        H = np.array([
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
        ])
        self._apply_update(z_xy, h_x, H, R)

    def update_camera(self, z_xyvv, R):
        """Camera update: (x, y, vx, vy) where vx/vy are nonlinear in
        (v, heading), requiring the measurement Jacobian to be linearized
        at the current state estimate.
        """
        x, y, v, heading = self.x
        cos_h, sin_h = math.cos(heading), math.sin(heading)
        h_x = np.array([x, y, v * cos_h, v * sin_h])
        H = np.array([
            [1.0, 0.0, 0.0,      0.0],
            [0.0, 1.0, 0.0,      0.0],
            [0.0, 0.0, cos_h,   -v * sin_h],
            [0.0, 0.0, sin_h,    v * cos_h],
        ])
        self._apply_update(z_xyvv, h_x, H, R)

    # ---- Convenience accessors ----

    def position(self):
        return float(self.x[0]), float(self.x[1])

    def velocity(self):
        v, heading = self.x[2], self.x[3]
        return float(v * math.cos(heading)), float(v * math.sin(heading))

    def widen_uncertainty(self, state_indices, factor):
        """Inflate the covariance of the given state components, e.g. when
        a sensor that primarily constrains them has just gone stale. This
        is a one-time bump on top of the steady per-cycle growth process
        noise already contributes each predict step while that sensor's
        updates are skipped -- it makes the "we know less now" acknowledgment
        immediate rather than only gradual."""
        for i in state_indices:
            self.P[i, i] *= factor
