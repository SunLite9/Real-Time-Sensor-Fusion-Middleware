# Real-Time Sensor Fusion Middleware — Design Document

A Kubernetes-native middleware system that fuses four asynchronous, noisy simulated self-driving-car sensors (LiDAR, camera, IMU, GPS) into a single state estimate with an Extended Kalman Filter on a hard 100ms cycle, then proves — with cluster-measured numbers, not idealized-loop numbers — where that real-time guarantee actually breaks under load, under tightened resources, and under pod failure. Every claim in this document was produced by running the real system on a real `kind` cluster; nothing here is a projection.

---

## 1. Executive Overview

This project answers one question with real measurements instead of assumptions: if you build a real-time sensor-fusion pipeline as a fleet of Kubernetes microservices — not a single local process — does it actually keep its deadline, and where does it actually break? Four sensor simulators (LiDAR ~10Hz, camera ~30Hz, IMU ~100Hz, GPS ~2Hz) publish noisy observations of a shared ground-truth vehicle trajectory over MQTT; a fusion service subscribes to all four, runs an Extended Kalman Filter on a fixed 100ms cycle, and publishes a combined position/velocity estimate. Every piece of this — sensors, message broker, fusion service — is its own Kubernetes Deployment with explicit CPU/memory requests and limits from the first commit, because the entire value of the project is measuring behavior under real pod scheduling and real resource contention, not an idealized loop where "real-time" is true by construction.

Headline, cluster-measured results: the fused estimate beats a GPS-only baseline by 41.4% RMSE; the fusion service held a 0% deadline-miss rate at every sensor load level this cluster could actually schedule (up to 2840 Hz aggregate, 20x baseline); cutting the fusion pod's CPU limit from 750m to 100m collapsed it from 0% miss rate to a complete inability to serve its own status endpoint; and both a killed fusion pod and a killed sensor pod self-healed in well under a second under sustained heavy load, with zero manual intervention. Two independent, real bugs in the benchmark's own design — a false ceiling from per-process MQTT publish overhead, and an MQTT client-ID collision under replica scaling — were found, root-caused, and fixed along the way, and are documented in full because the reasoning behind catching them is as valuable as the final numbers.

This document is the permanent reasoning record behind that system: every alternative seriously considered, every constraint that forced a tradeoff, every bug and its root cause, the full operating and testing procedure, and every measured result — sufficient, on its own, to explain, defend, and extend the system without access to the codebase or its author.

## 2. Problem Definition and Context

### 2.1 The problem

Build a system that fuses four independent, asynchronous, noisy vehicle sensor streams into one accurate state estimate, on a hard 100ms real-time deadline, deployed natively on Kubernetes — and then prove, empirically, that the deadline holds, that the system degrades gracefully under two independent classes of failure (a sensor going quiet, and a sensor's pod dying outright), and that there exists a real, cluster-measured load level at which the guarantee breaks.

### 2.2 Why this project exists

It is the one project in a broader portfolio that puts real-time systems, distributed systems, and Kubernetes together as a single coherent architecture rather than three separate bullet points — relevant to SWE roles touching robotics, autonomy, platform engineering, or backend systems work. It is also the only project in that portfolio with a hard Kubernetes requirement, and it earns that requirement honestly: the K8s deployment is not a "we also containerized it" footnote, it is the substrate every real-time and fault-tolerance claim in the project is actually measured against.

### 2.3 Why this resists the obvious solution

The obvious version of this project is a single Python script: four threads or `asyncio` tasks generating sensor data into in-memory queues, a Kalman filter consuming them in a tight loop, done in an afternoon. That version can trivially claim "real-time" and "sensor fusion" — but neither claim means anything, because there is no real distribution, no real scheduling, and no real resource contention to violate the guarantee. A single process can't be CPU-throttled by a cgroup it doesn't know exists; it can't experience the broker round-trip that a separate MQTT hop actually costs; it can't have one of its "sensors" get OOM-killed while the others keep running.

This project deliberately gives up that shortcut. Every sensor is its own container, its own Kubernetes Deployment, reachable only through a message broker that is itself a Kubernetes Service, with explicit CPU/memory requests and limits set from day one. That decision is what makes every later result — the breaking-point Hz number, the resource-limit stall, the pod-kill downtime — a real, falsifiable measurement instead of an assumption. It is also what makes the project harder: distributed timing, out-of-order MQTT delivery, container image build/load cycles, and Kubernetes' own scheduling limits (which turned out to be the actual breaking point — see §16.5) all become real engineering problems instead of hypotheticals.

## 3. Goals, Success Criteria, and Scope

### 3.1 Goals

1. Fuse four asynchronous, differently-rated, differently-noisy sensor streams into a state estimate measurably more accurate than any single sensor.
2. Prove, with real timing instrumentation (not a one-off stopwatch run), that a 100ms fusion cycle deadline is met under normal load.
3. Handle two independent classes of failure gracefully: a sensor that goes quiet but whose pod is still alive, and a sensor pod (or the fusion pod itself) that dies outright.
4. Empirically find the system's own breaking point — the load level at which the deadline-miss rate crosses 1% — measured on the live cluster, not calculated.
5. Prove that Kubernetes resource limits have a measurable, not just theoretical, effect on the real-time guarantee.
6. Deploy every component as a Kubernetes-native microservice from the first line of code.

### 3.2 Success criteria / definition of done

A Kubernetes-deployed system fusing four simulated sensors on a 100ms deadline, with: a benchmark chart showing miss-rate vs. sensor load measured on the cluster; documented two-layer graceful degradation (EKF-level and pod-level), each independently tested; and a resource-limit stress test showing the deadline-miss rate respond predictably (and provably) to tighter pod constraints.

### 3.3 In scope

- Four simulated sensors (LiDAR, camera, IMU, GPS) with distinct, realistic rate and noise characteristics.
- An Extended Kalman Filter fusion core with a documented, defensible reason for needing an EKF specifically (not a plain KF).
- Deadline timing instrumentation with live retrieval (HTTP + logs).
- Two-layer fault tolerance: application-layer (EKF staleness handling) and platform-layer (Kubernetes liveness probes/restarts).
- An automated breaking-point benchmark that scales real load and measures miss rate on the cluster.
- A resource-limit stress test and a pod-chaos resilience test, both against the live cluster.
- Full reproducibility from a documented, scripted setup on a single local machine.

### 3.4 Explicitly out of scope (non-goals)

- Multi-node Kubernetes clusters or cloud deployment (single-node `kind` only — see §21).
- Production-grade security hardening (the `/crash` test endpoint is real evidence of this boundary — see §17).
- Integration with a general observability stack (Prometheus/Grafana/OpenTelemetry) — bespoke JSON endpoints only.
- Real sensor hardware or real vehicle dynamics beyond a kinematic unicycle model.
- Automated CI/CD; all verification is run manually against a live cluster.
- Competing on Kalman-filter tuning benchmarks — noise covariances are set directly from known simulator ground truth, not estimated or tuned.

## 4. Requirements and Constraints

### 4.1 Functional requirements

- Each sensor must publish JSON-encoded, timestamped readings to its own MQTT topic at its own realistic rate.
- The fusion service must subscribe to all four topics, maintain the most recent reading per sensor, and run predict+update on a fixed 100ms cycle regardless of which sensors have fresh data that cycle.
- The fusion service must handle late-arriving and out-of-order messages explicitly.
- Every fusion cycle must be timestamped and classified on-time/missed against the 100ms deadline, with running statistics retrievable live.
- A sensor with no fresh data within a staleness threshold must be excluded from that cycle's update without crashing or stalling the fusion loop.
- A crashed or hung sensor pod must be detected and restarted by Kubernetes without application-level intervention.
- The system must support an automated way to scale real sensor load and measure the resulting deadline-miss rate on the cluster.

### 4.2 Non-functional requirements

- Every component must be a Kubernetes Deployment with explicit CPU/memory requests and limits from the first version of its manifest — not added retroactively.
- Sensors and the fusion service must reach the message broker exclusively through Kubernetes DNS (`mosquitto`), never a hardcoded IP.
- The entire system must be reproducible from a documented script sequence on a single local machine, with no external dependencies beyond Docker and `kubectl`/`kind`.
- All measured results reported in this document and the README must come from actually running the system, not from static analysis or estimation.

### 4.3 Constraints (and which were discovered rather than anticipated)

- **Local, single-machine development.** Docker Desktop on Windows, a single-node `kind` cluster. This was a deliberate constraint (reproducibility, no cloud cost/credentials — see §11.1), but it had a consequence that was *not* anticipated until measured: the `kind` node's default kubelet `--max-pods` of 110 became the actual breaking-point ceiling for the load benchmark (§16.5), not any resource exhaustion.
- **Windows-specific tooling friction.** `kubectl exec ... cat /sys/fs/cgroup/...` paths get mangled by MSYS path conversion in Git Bash; `kubectl port-forward` process/port lifecycle needed manual management throughout development. Neither was solved generically — both were worked around ad hoc as they came up (see §21).
- **Python for velocity, not C++ for rigor.** The project's own scope notes allow an optional C++ fusion core for extra rigor; Python was used throughout because the actual bottleneck the project needed to characterize was Kubernetes/distributed-systems behavior, not language-level performance — and this choice itself became load-bearing evidence later, when a single Python sensor process's per-message overhead (JSON encode + MQTT publish call) was discovered to cap real throughput around 400–500Hz regardless of configured rate (§11.9, §16.5).
- **Six-phase build order, each phase depending on the last.** The project was built in a fixed sequence (sensors/MQTT → fusion core → timing instrumentation → fault tolerance → breaking-point benchmark → resource-limit/chaos tests), each phase's code assumed to exist before the next began. This ordering is preserved in §12 as a historical record, not just a checklist.

## 5. System Architecture

```
[LiDAR Deployment]  ─┐
[Camera Deployment] ─┤
[IMU Deployment]    ─┼──▶ [Mosquitto Service] (ClusterIP, DNS: mosquitto) ──▶ [Fusion Deployment]
[GPS Deployment]    ─┘                                                          │  EKF, 100ms cycle
                                                                                 │  publishes fusion/state
                                                                                 │
                                                        ┌────────────────────────┼────────────────────────┐
                                                        ▼                        ▼                        ▼
                                              /timing-stats,             two-layer fault        benchmarks/*.py
                                              /timing-reset               tolerance: EKF          scale sensor
                                              HTTP endpoints              staleness handling      replicas/CPU
                                              (deadline-miss              (app layer) +           limits on the
                                              instrumentation)            liveness probes/         live cluster,
                                                                          /crash (platform          measure effect
                                                                          layer)
```

Every sensor Deployment also runs a small control server (`sensors/control_server.py`) exposing `/healthz` (liveness probe target), `/crash` (test-only, gated), and `/force-dropout` (LiDAR/GPS, on-demand fault injection).

### 5.1 Component map

| Path | Role |
|---|---|
| `sensors/trajectory.py` | Shared, deterministic ground-truth vehicle path every sensor samples |
| `sensors/{lidar,camera,imu,gps}_sim.py` | Four sensor simulators, one process each |
| `sensors/control_server.py` | Shared `/healthz`, `/crash`, `/force-dropout` HTTP server used by all four sims |
| `sensors/Dockerfile.{lidar,camera,imu,gps}` | One image per sensor |
| `fusion/ekf.py` | Extended Kalman Filter math (predict/update, Jacobians) |
| `fusion/fusion_service.py` | MQTT subscriber, sensor buffer, 100ms cycle loop, HTTP status server |
| `fusion/timing.py` | `TimingTracker` — deadline instrumentation |
| `fusion/fault_events.py` | `FaultEventTracker` — application-layer stale/recovery logging |
| `fusion/Dockerfile` | Fusion service image |
| `k8s/*.yaml` | One manifest per Deployment (mosquitto, 4 sensors, fusion) |
| `tests/*.py` | Smoke test, accuracy test, timing-instrumentation self-test, fault-tolerance test |
| `benchmarks/*.py` | Breaking-point benchmark, resource-limit stress test, pod-chaos test |
| `requirements.txt`, `.gitignore`, `LICENSE`, `README.md`, `DESIGN.md` | Project plumbing and documentation |

### 5.2 Deployment topology

Six Kubernetes Deployments run concurrently on the single-node `kind` cluster: `mosquitto`, `lidar-sim`, `camera-sim`, `imu-sim`, `gps-sim`, `fusion-service`. Only `mosquitto` has a ClusterIP Service (DNS name `mosquitto`) — every other component is reached either by that Service (sensors and fusion publishing/subscribing to the broker) or by direct pod/deployment port-forwarding for test and benchmark scripts (there is no Service in front of the sensor or fusion control servers; benchmark and test scripts use `kubectl port-forward deployment/<name>` directly).

## 6. End-to-End System Flow

1. **Startup.** Each sensor pod starts its MQTT client (unique `client_id` derived from `HOSTNAME`, the Kubernetes-injected pod name — see §12.5 for why this matters), starts its control server on port 8080, and enters its publish loop. The fusion pod starts its MQTT client, starts its HTTP status server on port 8080, and enters its 100ms cycle loop. All five processes connect to the broker via the `mosquitto` Kubernetes DNS name — never a hardcoded IP.
2. **Publish.** Each sensor wakes on its own fixed period (e.g., IMU every ~10ms), samples `GroundTruthTrajectory.state_at(now)` — a pure function of absolute wall-clock time, so every process computes identical ground truth independently (§9.5) — adds its own Gaussian noise, and publishes a JSON payload to its topic (`sensors/lidar`, `sensors/camera`, `sensors/imu`, `sensors/gps`).
3. **Receive.** The fusion service's MQTT client (a background thread via `paho-mqtt`'s `loop_start()`) receives messages asynchronously and writes the latest reading per sensor into a thread-safe `SensorBuffer`, rejecting any reading older than what's already buffered for that sensor (out-of-order protection, §7.4).
4. **Fuse.** A separate cycle thread wakes every 100ms: it checks freshness for all four sensors, logs any stale/recovered transitions, runs the EKF predict step (using the latest IMU reading as a control input, or coasting with zero control if IMU is stale), runs selective EKF updates for any sensor with an unconsumed fresh reading, and publishes the fused `(x, y, vx, vy)` to `fusion/state`.
5. **Instrument.** The same cycle records its own actual wall-clock duration into `TimingTracker` (classified on-time/missed, CSV-logged, batched-flushed) and prints a running summary to stdout every 50 cycles.
6. **Serve status.** A background HTTP server thread on the fusion pod answers `GET /timing-stats`, `GET /timing-reset`, and `GET /fault-events` with live JSON, independent of the cycle and MQTT threads.
7. **Self-heal.** If a sensor process hangs or dies, its Kubernetes liveness probe (`GET /healthz`) fails and the kubelet restarts the container; if the fusion pod itself is killed, the Deployment controller schedules a replacement pod, which reconnects to MQTT and resumes the cycle loop from a fresh (empty) internal state — no coordination with any surviving component is needed, because every sensor is still publishing to the same broker topics the new pod subscribes to on startup.
8. **Benchmark control flow.** A benchmark script (running outside the cluster, on the host) establishes its own `kubectl port-forward` to the mosquitto Service and/or the fusion Deployment, drives cluster state via `kubectl scale` / `kubectl set resources` / `kubectl delete pod`, polls the fusion pod's HTTP endpoints for live statistics, and restores the cluster to its 1-replica/original-limits baseline in a `finally` block regardless of success or failure.

## 7. Component-Level Design

### 7.1 `sensors/trajectory.py` — `GroundTruthTrajectory`

A single class, `state_at(t)`, mapping an absolute timestamp to `(x, y, vx, vy, heading)` on a closed "stadium" loop (two 60m straights, two 20m-radius 180° turns curving the same direction), driven at a constant 8 m/s, with the phase anchored to epoch 0 (not process start time). Stateless beyond its geometric constants — every process that imports it computes identical output for identical input, with no communication required between processes. This is the shared reference every sensor's noise is added to and every accuracy measurement is scored against.

### 7.2 Sensor simulators (`lidar_sim.py`, `camera_sim.py`, `imu_sim.py`, `gps_sim.py`)

Each is a single-process, single-threaded Python loop: sample ground truth, add sensor-specific Gaussian noise (distinct per sensor — see §9.2), publish JSON over MQTT, sleep until the next period. Each starts `sensors/control_server.py`'s HTTP server on a background thread for `/healthz`/`/crash`/`/force-dropout` (LiDAR/GPS only). GPS additionally runs a self-contained random-dropout state machine (Poisson-ish trigger, exponential duration) independent of the on-demand `/force-dropout` mechanism, so both random and deliberate dropout can occur.

### 7.3 `fusion/ekf.py` — `ExtendedKalmanFilter`

Owns the 4-element state vector `[x, y, v, heading]` and its 4×4 covariance matrix. Exposes `predict(dt, accel_tangential, omega)`, `update_position(z_xy, R)` (LiDAR/GPS), `update_camera(z_xyvv, R)`, and `widen_uncertainty(state_indices, factor)`. Internally, `_apply_update()` is the single generic EKF update implementation both `update_position` and `update_camera` call with their own `h(x)` and Jacobian `H` — there is no separate "linear sensor" code path, even though LiDAR/GPS's measurement model happens to be linear (§9.3). Never touched by any thread except the fusion cycle thread — no internal locking.

### 7.4 `fusion/fusion_service.py` — `FusionService` and `SensorBuffer`

`SensorBuffer` is the thread-safe boundary between the MQTT network thread (writer) and the cycle thread (reader): `offer()` accepts a reading only if strictly newer than the buffered one for that sensor; `take_fresh()` returns a reading only if it exists, is within its staleness threshold, and has not already been consumed this cycle. `FusionService` owns the `SensorBuffer`, the `ExtendedKalmanFilter`, a `TimingTracker`, a `FaultEventTracker`, the MQTT client, and the HTTP status server, and runs the 100ms cycle loop described in §6, step 4.

### 7.5 `fusion/timing.py` — `TimingTracker`

Streaming (Welford-style, O(1) memory) statistics — total cycles, miss count, sum and sum-of-squares of duration (for mean and stddev-as-jitter), max duration — updated under a single lock per `record()` call, plus a CSV log flushed every 20 cycles rather than every cycle (§12.4 explains why this specific number matters). `reset()` zeroes the running statistics without touching the CSV log, added specifically to support per-level measurement in the breaking-point benchmark (§11.9).

### 7.6 `fusion/fault_events.py` — `FaultEventTracker`

Per-sensor stale/not-stale boolean state plus a "went stale at" timestamp; `check(sensor, now, is_fresh)` returns `"stale"`, `"recovered"`, or `None` on each call, logging a CSV row and appending to a bounded in-memory recent-events list on any transition. Deliberately mirrors `TimingTracker`'s retrieval pattern (CSV log + HTTP JSON endpoint on the same pod/port) rather than inventing a second mechanism.

### 7.7 `sensors/control_server.py` — shared sensor control plane

A `SensorControlState` (thread-safe: `crash_enabled` flag, `manual_dropout_until` timestamp) plus `start_control_server()`, which spins up a `BaseHTTPRequestHandler`-based server on a background thread. Shared by all four sensor simulators rather than duplicated per-sensor, since the health/crash/dropout-trigger logic is identical across sensor types.

### 7.8 Kubernetes manifests (`k8s/*.yaml`)

One Deployment per component, each with explicit `resources.requests`/`resources.limits` sized to that component's actual workload (IMU, the highest-rate sensor, gets the largest sensor CPU allocation; fusion gets the largest allocation of any component, since it does EKF math and MQTT I/O for all four streams every cycle). `mosquitto.yaml` additionally includes a ConfigMap (anonymous MQTT access for local development) and the only ClusterIP Service in the system. Sensor manifests include `livenessProbe` blocks (`GET /healthz`, port 8080) and `CRASH_ENDPOINT_ENABLED=true` (test-cluster-only — see §17.1).

### 7.9 Benchmark scripts (`benchmarks/*.py`)

Each is a self-contained Python script run from the host, not from inside the cluster: they shell out to `kubectl` (scale replicas, patch resources, delete pods, check rollout status), manage their own `kubectl port-forward` subprocesses, poll the fusion pod's HTTP endpoints, and write results to CSV plus a matplotlib chart. Each restores baseline cluster state in a `finally` block. Designed independently per phase rather than sharing a common library, since each phase's benchmark evolved under different, phase-specific constraints (§11.9, §11.10) and premature sharing would have coupled unrelated concerns.

## 8. Data Design

### 8.1 MQTT message schemas (JSON payloads)

| Topic | Fields | Published by |
|---|---|---|
| `sensors/lidar` | `sensor`, `timestamp`, `x`, `y` | `lidar_sim.py`, ~10Hz |
| `sensors/camera` | `sensor`, `timestamp`, `x`, `y`, `vx`, `vy` | `camera_sim.py`, ~30Hz |
| `sensors/imu` | `sensor`, `timestamp`, `ax`, `ay`, `omega` | `imu_sim.py`, ~100Hz |
| `sensors/gps` | `sensor`, `timestamp`, `x`, `y` | `gps_sim.py`, ~2Hz |
| `fusion/state` | `timestamp`, `x`, `y`, `vx`, `vy` | `fusion_service.py`, every 100ms |

`timestamp` is always a Unix epoch float from the publishing process's own clock (all processes share the same underlying machine clock in this deployment — see §21 for the untested multi-node clock-skew implication).

### 8.2 EKF internal state representation

State vector `x = [x, y, v, heading]` (position in meters, scalar forward speed in m/s, heading in radians); covariance `P`, a 4×4 NumPy array. Measurement noise `R` and process noise `Q` are also NumPy arrays, constructed per-call (measurement) or scaled by `dt` per-step (process) — see §9.3–9.4 for their derivation.

### 8.3 CSV log schemas (inside each fusion pod, ephemeral filesystem)

- `timing_log.csv` (`TIMING_LOG_PATH`, default `/app/timing_log.csv`): `cycle, start, end, duration_ms, on_time`.
- `fault_events.csv` (`FAULT_LOG_PATH`, default `/app/fault_events.csv`): `sensor, event, timestamp, duration_s`.

### 8.4 HTTP JSON response schemas (fusion pod, port 8080)

- `GET /timing-stats` → `{total_cycles, miss_count, miss_rate_pct, avg_duration_ms, worst_case_ms, jitter_ms}`.
- `GET /timing-reset` → `{reset: true}` (side-effecting; zeroes the running statistics).
- `GET /fault-events` → `{current_status: {sensor: bool, ...}, recent_events: [{sensor, event, timestamp, duration_s}, ...]}`.

### 8.5 Benchmark output schemas

- `benchmarks/breaking_point_results.csv`: `multiplier, aggregate_hz, total_cycles, miss_count, miss_rate_pct, avg_duration_ms, worst_case_ms, jitter_ms, fusion_cpu`.
- `benchmarks/resource_limit_results.csv`: `label, total_cycles, miss_count, miss_rate_pct, avg_duration_ms, worst_case_ms, jitter_ms, fusion_cpu`.
- Both benchmark scripts also emit a `.png` chart (matplotlib, saved via `Agg` backend for headless generation) alongside the CSV.

## 9. Algorithms, Models, and Technical Methods

### 9.1 Ground-truth trajectory geometry

A piecewise-analytic path: two straight segments (length 60m each) and two circular-arc segments (radius 20m, 180° each, both curving the *same* direction — see §12.2 for the bug this fixes). `state_at(t)` computes `elapsed = t % period_s` and walks the segment list accumulating position/heading analytically (closed-form circular-arc geometry, not numerical integration), returning position, velocity (`speed × (cos, sin)(heading)`), and heading. Total period ≈ 30.7s at 8 m/s.

### 9.2 Sensor noise models

Each sensor decomposes its noise into components aligned with real sensor physics rather than a single isotropic Gaussian:

- **LiDAR/camera** decompose noise into along-heading ("range-like") and perpendicular-to-heading ("lateral") components via rotation by the current heading, then noise standard deviations are asymmetric between the two axes (LiDAR: range std 0.35m ≫ lateral std 0.08m; camera: range std 0.9m ≫ lateral std 0.05m — camera is better laterally but has no real depth sensing).
- **IMU** adds white noise (accel std 0.25 m/s², gyro std 0.03 rad/s) *plus* a random-walk accelerometer bias (`bias += N(0, 0.002²·dt)` each step) — modeling the slow drift real MEMS IMUs exhibit, which is what makes IMU-only dead reckoning diverge over time and why the EKF treats IMU as a control input rather than an authoritative position source.
- **GPS** adds isotropic noise (std 0.5m) plus a two-parameter dropout model: a Poisson-like per-second trigger check (`P(trigger) = check_period / mean_interval`) starting an exponentially-distributed-duration dropout (`mean_duration` = 4s).

### 9.3 EKF prediction model (nonlinear)

```
x' = x + v·cos(heading)·dt
y' = y + v·sin(heading)·dt
v' = v + accel_tangential·dt        where accel_tangential = ax·cos(heading) + ay·sin(heading)
heading' = wrap(heading + omega·dt)  to (-pi, pi]
```

`ax`, `ay`, and `omega` come from the latest fresh IMU reading, or zero if IMU is stale (coasting). The `ax·cos(heading) + ay·sin(heading)` projection happens *inside* `predict()`, not as a scalar precomputed by the caller — it's a function of the heading state, so `F` needs a term for it (see below). An earlier version of this filter computed the projection one layer up in `fusion_service.py` and passed in a plain scalar; that silently dropped `d(v_pred)/d(heading)` from `F`, understating how much the `v` estimate's uncertainty should grow when the heading estimate is off. Folding the projection into `predict()` itself is what makes the Jacobian complete.

Jacobian `F` (recomputed every cycle, since it depends on the current `v` and `heading`):

```
F = [[1, 0, cos(h)·dt, -v·sin(h)·dt],
     [0, 1, sin(h)·dt,  v·cos(h)·dt],
     [0, 0, 1,          dt·(-ax·sin(h) + ay·cos(h))],
     [0, 0, 0,          1]]
```

The heading state itself is also wrapped to `(-pi, pi]` after every predict step (via `atan2(sin, cos)`), rather than left to accumulate unbounded over a long-running deployment. Process noise `Q = diag([0.05, 0.05, 0.3, 0.05])² · dt`.

### 9.4 EKF measurement models

- **LiDAR/GPS** (`update_position`): `h(x) = [x, y]`, `H = [[1,0,0,0],[0,1,0,0]]` — linear, the degenerate case of the general EKF update. `R_lidar = diag(0.35², 0.08²)`, `R_gps = diag(0.5², 0.5²)`, matched directly to each simulator's injected noise.
- **Camera** (`update_camera`): `h(x) = [x, y, v·cos(heading), v·sin(heading)]`, with Jacobian
  ```
  H = [[1, 0, 0,       0],
       [0, 1, 0,       0],
       [0, 0, cos(h), -v·sin(h)],
       [0, 0, sin(h),  v·cos(h)]]
  ```
  linearized at the current state estimate every update — genuinely nonlinear, since the state carries `(v, heading)` but the camera reports Cartesian velocity. `R_camera = diag(0.9², 0.9², 0.4², 0.4²)`.
- All updates share one generic implementation (`_apply_update`): innovation `y = z - h(x)`, `S = H·P·Hᵀ + R`, Kalman gain `K = P·Hᵀ·S⁻¹`, state update `x += K·y`, covariance update `P = (I - K·H)·P`.

### 9.5 Staleness detection and covariance widening

Per sensor, per cycle: `is_fresh = (now - latest_reading.timestamp) <= max(4 × expected_interval, 0.15s)`. On a fresh→stale transition specifically (not every cycle while stale), `widen_uncertainty()` multiplies the covariance diagonal entries for that sensor's primarily-constrained state indices by 4× (LiDAR/GPS → `[0,1]`; camera → `[0,1,2,3]`; IMU → `[2,3]`). No corresponding "narrow" step exists on recovery — the next fresh update's ordinary Kalman gain computation handles that automatically, since a fresh measurement is always weighed against whatever the filter's current (possibly inflated) uncertainty is.

### 9.6 Timing statistics (streaming, O(1) memory)

`avg = sum_duration / n`; `variance = max(0, sum_duration_sq/n - avg²)`; `jitter = sqrt(variance)`. Computed incrementally on every `record()` call rather than by storing and later processing a full sample history — deliberately trades exact-percentile reporting for unbounded-duration-safe memory use (§11.7).

## 10. APIs, Interfaces, and Data Contracts

### 10.1 HTTP endpoints — fusion pod (port 8080)

| Method | Path | Response | Side effect |
|---|---|---|---|
| GET | `/timing-stats` | JSON timing snapshot (§8.4) | none |
| GET | `/timing-reset` | `{reset: true}` | zeroes running timing statistics |
| GET | `/fault-events` | JSON fault snapshot (§8.4) | none |

### 10.2 HTTP endpoints — sensor pods (port 8080, via `sensors/control_server.py`)

| Method | Path | Response | Side effect | Gating |
|---|---|---|---|---|
| GET | `/healthz` | `200 ok` | none | always on (liveness probe target) |
| GET | `/crash` | `200 crashing` then process exit, or `403` | terminates the process (`os._exit(1)`) after a short delay | `CRASH_ENDPOINT_ENABLED` env var, `"true"` in this test cluster only |
| GET | `/force-dropout?duration=N` | `200 dropout triggered for Ns` | LiDAR/GPS only: suppresses publishing for N seconds starting immediately | always on |

### 10.3 Environment variable contracts

| Variable | Consumed by | Default | Purpose |
|---|---|---|---|
| `MQTT_BROKER_HOST` | all sensors, fusion | `mosquitto` | Broker DNS name — never a hardcoded IP |
| `MQTT_BROKER_PORT` | all sensors, fusion | `1883` | Broker port |
| `PUBLISH_RATE_HZ` | all sensors | per-sensor default (10/30/100/2) | Configured publish rate (see §11.9 for its real-world ceiling) |
| `CRASH_ENDPOINT_ENABLED` | all sensors | `"false"` | Gates `/crash` |
| `TIMING_LOG_PATH` | fusion | `/app/timing_log.csv` | CSV log location |
| `FAULT_LOG_PATH` | fusion | `/app/fault_events.csv` | CSV log location |
| `TIMING_HTTP_PORT` | fusion | `8080` | Status HTTP server port |

### 10.4 CLI contracts (test and benchmark scripts)

All scripts use `argparse` with sensible defaults so they can be run with no flags for the common case:

- `tests/mqtt_smoke_test.py [--host] [--port] [--duration]`
- `tests/test_fusion_accuracy.py [--host] [--port] [--duration]`
- `tests/test_fault_tolerance.py [--mqtt-host] [--mqtt-port] [--gps-control-url] [--fusion-status-url] [--dropout-duration] [--settle-before-s] [--recovery-window-s]`
- `tests/test_timing_stress.py` (no flags; self-contained unit-level check)
- `benchmarks/breaking_point.py [--multipliers] [--settle-s] [--duration-s]`
- `benchmarks/resource_limit_test.py [--original-limit] [--original-request] [--tight-limit] [--tight-request] [--settle-s] [--duration-s]`
- `benchmarks/chaos_test.py [--load-multiplier]`

## 11. Design Decisions, Alternatives, and Tradeoffs

Each subsection: what was chosen, what else was considered, and the specific constraint that forced the choice.

### 11.1 Deployment platform: `kind` over minikube, k3s, or a real cloud cluster

**Chosen:** `kind` (Kubernetes-in-Docker), single node, created with `kind create cluster --name sensor-fusion`.

**Alternatives considered:**
- **minikube** — more configuration surface (driver choice: VM vs Docker vs Hyper-V), historically slower image-load workflow (needs `minikube image load` or a registry round-trip depending on driver).
- **k3s** — lighter-weight distribution, but adds a second install path (systemd service) instead of "it's just a container," and less ubiquitous for local dev loops.
- **Real cloud cluster (EKS/GKE/AKS)** — would make "real Kubernetes" claims stronger, but adds cost, credentials, and network latency variance that would confound the very timing measurements this project depends on; also directly contradicts the "reproducible on a laptop" goal of a portfolio project.

**Why `kind` won:** the node itself is a Docker container, so `kind load docker-image` pushes a locally-built image straight into the node's containerd image store — no registry, no push/pull round trip. That single property dominates iteration speed for a project that rebuilt and reloaded images dozens of times across six build phases.

**Cost knowingly accepted:** a single-node cluster has a single node's resource ceiling — including Kubernetes' own default 110-pods-per-node cap. This is not a hypothetical cost: it is the literal mechanism that ended the breaking-point benchmark (28x replica scale in the original single-trial run; 32x in the re-measured multi-trial data, §16.5), and it directly informs the future-work item of adding worker nodes (§21).

### 11.2 Message bus: MQTT over gRPC, Kafka, or ROS2/DDS

**Chosen:** MQTT via a single Mosquitto broker (`k8s/mosquitto.yaml`, anonymous access, ClusterIP Service, DNS name `mosquitto`).

**Alternatives considered:**
- **gRPC** — would require each sensor to know the fusion service's endpoint directly (or a discovery layer on top), turning a natural pub/sub topology into point-to-point wiring. Loses the "sensors don't know who's listening" decoupling that's realistic for a sensor fleet.
- **Kafka** — far heavier than the message volume justifies (partition management, broker cluster, ZooKeeper/KRaft), and optimized for durable log semantics this project doesn't need (readings are ephemeral; only the latest one per sensor matters).
- **ROS2 / DDS** — the "correct" answer in real robotics practice, but adds a large, unfamiliar runtime dependency that would become the project's actual learning curve, displacing the intended focus (Kubernetes-native real-time behavior, not middleware selection).

**Why MQTT won:** lightweight pub/sub, trivial to run as a single small Kubernetes Deployment + Service, and its topology (many independent publishers, one broker, one subscriber) matches the sensor-fleet shape exactly. It also has a real, non-hypothetical failure mode this project deliberately exercised: MQTT brokers evict a client when a new connection reuses its client ID (§12.5), which is a genuine lesson about the protocol, not an artifact of a toy example.

### 11.3 Ground-truth trajectory design

**Chosen:** a closed "stadium" loop, computed as a pure function of absolute time, anchored to epoch 0.

**Why a closed analytic loop instead of a recorded trajectory or random walk:** every sensor and the fusion service need to independently compute the *same* ground truth at the *same* timestamp, from different processes started at different wall-clock moments, without any synchronization protocol between them. A pure function of absolute time gives this for free. A recorded/logged trajectory would need to be shipped to every pod and index-synchronized against `PUBLISH_RATE_HZ`; a random walk wouldn't have a reproducible ground truth to score fusion accuracy against at all.

**Why this specific geometry:** it needed to exercise both straight-line motion (where position sensors dominate) and turning motion (where the EKF's nonlinear heading dynamics and the IMU's angular-rate control input matter), and close on itself so an unbounded-duration benchmark run never drifts to numerically extreme coordinates.

**Cost knowingly accepted, found the hard way:** the first implementation alternated turn direction, which traces an S-curve rather than a closed loop — documented in full as bug 1 in §12.2, because it's exactly the kind of defect that looks correct in isolation and only breaks when composed.

### 11.4 Sensor noise model design

Each sensor's noise characteristics were chosen to be *distinct* on purpose (§9.2), so that fusion has genuine information to combine rather than four copies of the same signal. This heterogeneity is what makes the accuracy result (§16.2) meaningful: if all four sensors had identical noise, fusing them wouldn't measurably beat the best single sensor, and the whole "why fuse sensors" argument would be unfalsifiable.

### 11.5 Fusion algorithm: EKF, and specifically this state vector

**Chosen state vector:** `[x, y, v, heading]` — a kinematic *unicycle* model — rather than the more obvious `[x, y, vx, vy]`.

**Why not `[x, y, vx, vy]` (which would make everything linear):** with position and Cartesian velocity as the state, every sensor's measurement model becomes linear, at which point a plain Kalman Filter is not just sufficient, it's the *correct* choice, and claiming "this needs an EKF" would be dishonest. The unicycle state makes both halves of the filter genuinely, unavoidably nonlinear (§9.3, §9.4): prediction through heading's trig functions, and the camera's measurement model converting `(v, heading)` to Cartesian velocity.

**Alternatives considered:**
- **Unscented Kalman Filter (UKF)** — handles stronger nonlinearity more accurately via sigma-point propagation, but at meaningfully higher per-cycle compute cost. Given the measured result that the fusion pod already uses ~750m of CPU even at baseline load (§16.5, §16.6), a UKF would have made the CPU-boundedness story arrive even sooner — interesting, but not necessary to demonstrate the target claims, and adds implementation/debugging complexity orthogonal to the project's actual thesis.
- **Particle filter** — handles arbitrary nonlinearity and non-Gaussian noise, but is far more compute-expensive and produces probabilistic, non-deterministic-per-run outputs, complicating reasoning about a hard deadline budget. Rejected as disproportionate to the actual nonlinearity present.

**IMU as a control input, not a fourth measurement:** the standard INS/GPS fusion pattern — the IMU propagates state at high rate between lower-rate corrections from position/camera sensors — and it means an IMU-stale condition is handled for free by falling back to zero control input rather than needing separate-case code.

**Noise covariances set from known simulator truth, not tuned:** honest for a system whose whole point is testing the *pipeline*, not competing on filter-tuning benchmarks (see §21 for the limitation this implies).

### 11.6 Concurrency model in the fusion service

**Chosen:** per-data-structure locks (`SensorBuffer`, `TimingTracker`, `FaultEventTracker` each own a `threading.Lock`) with minimal critical sections; the EKF itself is owned exclusively by the cycle thread and needs no lock.

**Out-of-order handling:** `SensorBuffer.offer()` only accepts a reading strictly newer than what's buffered; `take_fresh()` additionally tracks a per-sensor "last consumed timestamp" to prevent double-application across cycles.

**Alternative considered and rejected:** a shared queue per sensor (buffer *all* readings, replay backlog after a slow cycle). Rejected because replaying stale control inputs (e.g., old IMU readings) after a slow cycle is actively harmful to a filter that's supposed to represent "now" — only the most recent reading per sensor per cycle is ever useful.

### 11.7 Deadline timing instrumentation design

**Chosen:** wrap the actual wall-clock start/end of the full predict+update+publish sequence; streaming aggregate statistics (§9.6) instead of storing every duration.

**Why streaming instead of storing every sample:** a high-replica-scale benchmark run produces tens of thousands of cycles; running sums avoid unbounded memory growth and sorting/percentile cost, at the accepted cost of not reporting exact percentiles (only mean, max, stddev-as-jitter).

**CSV logging cadence — a real bug that shaped this decision (§12.4):** flushing every cycle caused measurable syscall overhead that contributed to a synchronized false-positive staleness stall across all sensors. Fixed by batching flush to every 20 cycles (~2s) — a decision directly traceable to a specific, root-caused production-like defect, not a default guess.

**Live observability, two independent channels kept deliberately:** an HTTP `/timing-stats` endpoint (for scripted polling — every benchmark script reads this) and a periodic stdout summary (for `kubectl logs -f` human observation). Both cost little to maintain and serve different consumers.

**`/timing-reset` — added in Task 5, not from the start:** the original design only accumulated statistics for the pod's entire lifetime, which was fine for a single baseline measurement but wrong for a multi-level benchmark, where later levels would otherwise report a cumulative average diluted by earlier levels. `reset()` deliberately does not touch the CSV log — reset is a reporting concern, not a data-retention concern.

**Self-verification before trusting the instrumentation:** `tests/test_timing_stress.py` injects a real `time.sleep()` beyond the deadline and asserts correct classification — verifying the ruler before measuring with it, carried through as an explicit requirement from the project's task structure.

### 11.8 Two-layer fault tolerance design

The system treats "a sensor stops sending useful data" and "a sensor's pod is actually dead" as two different failures needing two different fixes at two different layers, because conflating them leads to either an application layer reimplementing process supervision or a platform layer blind to a *live* process publishing garbage or nothing.

**Application layer:** exclude a stale sensor from that cycle's update (or zero IMU's control-input contribution, letting the filter coast); one-time covariance widening on the stale *transition* specifically, layered on top of the steady per-cycle growth process noise already contributes; recovery needs **no special code**, since the ordinary Kalman gain computation on the next fresh update naturally re-establishes trust. `FaultEventTracker` mirrors `TimingTracker`'s retrieval pattern deliberately, rather than inventing a second mechanism.

**Staleness threshold — tuned by a real false positive, not guessed:** `max(4 × expected_interval, 150ms)`. The floor was added after the IMU (10ms nominal interval, `4×10ms=40ms` threshold) false-positive-flapped under completely normal MQTT/network jitter (§12.4). A deliberately blunt fix — one fixed floor, not per-sensor-tuned — traded for simplicity and uniform, predictable behavior.

**Platform layer:** `livenessProbe` on `GET /healthz`; `GET /crash` for reproducible testing, gated behind `CRASH_ENDPOINT_ENABLED` — an unauthenticated remote-crash endpoint is a real security defect if it ever reached production, documented as a hard boundary in §17.1, not a soft caveat. `GET /force-dropout?duration=N` was chosen over an environment-variable-triggered dropout specifically because it needed to be triggerable *during a live, running test session* (before/during/after measurement in one continuous run), which an env-var change (requiring a rolling restart) could not provide.

### 11.9 Benchmark design — and why it changed twice

The breaking-point benchmark's design went through two complete redesigns, each forced by a real measurement that invalidated the prior assumption. This evolution is preserved in full because the reasoning that got discarded is at least as instructive as the final design.

**Attempt 1 — scale `PUBLISH_RATE_HZ` via `kubectl set env`.** The obvious reading of "progressively increase aggregate sensor publish rate" is to raise the rate a sensor is *told* to publish at. Run up to 32x baseline (4544 Hz aggregate), it showed 0% miss rate throughout — with average cycle duration *decreasing* as configured load increased, backwards for a real load test. Direct verification (subscribing to `sensors/imu`, counting real messages) confirmed the cause: at `PUBLISH_RATE_HZ=10000`, the sensor pod actually achieved only ~477 messages/sec, with CPU usage at 32m against a 200m limit — nowhere near CPU-bound. The real ceiling was Python single-process, single-threaded per-message overhead (JSON encode + MQTT publish call), capping out around 400–500Hz regardless of the configured target.

**Attempt 2 — scale sensor replicas via `kubectl scale`, not rate.** Since a single sensor process can't be pushed past its own overhead ceiling, the redesign uses Kubernetes-native horizontal replica scaling: N replicas of a sensor Deployment, each still publishing at baseline rate, genuinely multiplies real aggregate throughput (verified: 4 replicas × 100Hz IMU measured at ~394Hz actual). More faithful to the project's actual thesis than a rate parameter a single process can't produce.

**A second bug surfaced immediately by this redesign:** every sensor simulator used a hardcoded MQTT client ID, invisible at replica count 1 but catastrophic under replica scaling — the broker evicted each predecessor the instant a new replica claimed the same ID (§12.6). Fixed by deriving client ID from `HOSTNAME`.

**The benchmark's actual breaking point turned out to be a third thing entirely.** With both fixes in place, the benchmark ran cleanly through 20x baseline and failed to schedule at 28x — not CPU, not memory (both single-digit percent utilized even at that load), but Kubernetes' own default 110-pod-per-node kubelet limit (112 pods requested at 28x across four sensor Deployments). The harness was updated to detect a rollout that fails to complete and report it as the practical breaking point rather than crash — because that failure *is* the finding.

### 11.10 Resource-limit and chaos test design

**Resource-limit test — fixed load, two CPU limits, direct comparison.** Rather than re-running the full multi-level benchmark under a second CPU limit, the test holds sensor load constant at the 8x level (already known to be comfortably within the original 750m limit's headroom) and compares only two conditions: original vs. tightened (750m → 100m), isolating the CPU-limit variable cleanly.

**The tightened-limit result required handling an unanticipated failure mode:** at 100m, the fusion pod became so CPU-starved it stopped reliably answering its own HTTP endpoint at all — connection refusals after retries, while `RESTARTS` stayed 0 the entire time (never crashed, simply too starved to service a connection in time). The script was hardened with retry-with-backoff and a defined "stall" result rather than crashing — unresponsiveness under starvation *is* the finding.

**Chaos test — sustained load, not idle, for both phases.** Task 4 already proved both fault-tolerance layers at idle (1 replica each); the chaos test's purpose is checking whether that holds *under load* (16x replicas, a level already confirmed clean). Phase A kills the fusion pod, measuring downtime directly from MQTT message arrival timestamps rather than trusting `kubectl get pods` phase timing. Phase B kills a sensor pod, but with a twist forced by the replica-scaling design itself: with 16 redundant replicas of the same sensor type, killing one is invisible by construction (the fusion service only tracks "latest reading per sensor *type*") — so LiDAR is temporarily scaled to 1 replica for this phase specifically, while the other three sensor types remain at 16x.

**A `kubectl port-forward` limitation surfaced during Phase A, not anticipated in the design:** the tunnel pins to whichever specific pod backs the Deployment at the moment it starts and does not follow a replacement. Fixed by explicitly tearing down and re-establishing the port-forward against the new pod name after confirming replacement (§12.7).

## 12. Implementation and Project Evolution

The system was built in six sequential phases, each assuming the previous phase's outputs already existed. This section is the chronological build history; §11 above is the decisions-and-alternatives view of the same material.

### 12.1 Phase 1 — Kubernetes cluster + sensor microservices over MQTT

Stood up the `kind` cluster, initialized a dedicated git repository (deliberately separate from a pre-existing, unrelated git repository found at the host's home directory — a stray repo that was left untouched, not merged into or disturbed), scaffolded `/sensors`, `/fusion`, `/k8s`, `/benchmarks`, `/tests`. Implemented all four sensor simulators and the shared trajectory module, wrote Dockerfiles, built and `kind load`-ed four images, wrote and applied the Mosquitto + four sensor manifests, and verified via a smoke-test MQTT subscriber (§16.1).

### 12.2 Phase 2 — Fusion core (EKF)

Implemented `ekf.py` and `fusion_service.py`. During accuracy testing, found and fixed two trajectory bugs that were blocking a meaningful result: (1) the ground-truth loop didn't actually close due to alternating turn direction (§12's bug list, item 1), caught because RMSE was inexplicably high (4–6m) despite good-looking steady-state tracking in manual spot checks; (2) ground truth was anchored to per-process start time rather than absolute epoch time, meaning different sensor pods (started at slightly different wall-clock moments) computed different ground truth for the same timestamp (item 2). Both fixed before the accuracy result in §16.2 was trusted.

### 12.3 Phase 3 — Deadline instrumentation

Implemented `timing.py` and wired it into the cycle loop, added the HTTP status server, wrote `test_timing_stress.py` to self-verify the instrumentation before relying on it, and measured the normal-load baseline (§16.3).

### 12.4 Phase 4 — Two-layer fault tolerance

Implemented `fault_events.py`, `widen_uncertainty()`, the staleness detection logic, `control_server.py`, and the Kubernetes liveness-probe/`/crash` platform layer. Found and fixed two bugs while validating: the IMU staleness threshold was too tight for real jitter (item 3), and — more subtly — the fix for that (redeploying with a new threshold) initially surfaced a *second*, unrelated symptom: a synchronized stale-flap across all four sensors simultaneously, on an unnervingly exact ~2-second cadence. Chasing this down first led to a false lead (a `cpu.stat` read via `kubectl exec` that appeared to show catastrophic CPU throttling, but was actually reading the wrong cgroup level due to `kind`'s nested-container architecture — abandoned once `metrics-server` gave trustworthy per-pod numbers), before the real cause was found: `TimingTracker` was flushing its CSV file on every single cycle, and the resulting syscall overhead was the actual source of the periodic stall (item 4). Verified both the staleness test and the pod-crash test against the live cluster (§16.4) only after these fixes.

### 12.5 Phase 5 — Breaking-point benchmark

Added `TimingTracker.reset()` and the `/timing-reset` endpoint, installed `metrics-server` (patched for `kind`'s self-signed kubelet cert). Built and ran the first benchmark design (rate-scaling), which produced a physically implausible result (decreasing cycle duration under increasing configured load) that led directly to discovering the ~400–500Hz single-process throughput ceiling (item 5) and the full redesign to replica-based scaling (§11.9). That redesign immediately surfaced the MQTT client-ID collision bug (item 6), fixed before any benchmark numbers were trusted. The final, corrected benchmark run (§16.5) found the real breaking point: Kubernetes' own 110-pod node capacity, not the fusion algorithm.

### 12.6 Phase 6 — Resource-limit stress test, pod-chaos test, MIT license

Built `resource_limit_test.py` and `chaos_test.py`. The resource-limit test's first run crashed on an unhandled connection error the moment the tightened CPU limit made the fusion pod unresponsive — hardened with retries and a defined stall result before the final run (§16.6). The chaos test's first run succeeded functionally in Phase A (fusion pod kill, ~0.10s downtime) but then crashed in the post-recovery HTTP check due to the port-forward-doesn't-follow-a-replacement-pod issue (item 7); fixed and rerun. Phase B's first run showed platform-layer success but no application-layer fault event — investigated directly (rather than assumed to be a bug) by adding a second, independent MQTT-based downtime measurement, which revealed the real outage (~30ms) was genuinely faster than the 400ms staleness threshold, a legitimate result rather than a defect (§16.7). Added the MIT `LICENSE` file at the same time as this final phase's README pass.

### 12.7 Post-launch hardening pass

A later review pass found and fixed several issues that hadn't surfaced during the original six phases:

1. **EKF Jacobian gap.** `predict()` took a pre-computed scalar `accel_tangential` from the caller, which meant `F` never captured `d(v_pred)/d(heading)` even though the tangential-acceleration projection is itself a function of heading. Fixed by moving the `ax·cos(heading) + ay·sin(heading)` projection inside `predict()` itself, so `F` can include the term (§9.3).
2. **Unbounded heading accumulation.** `heading` was never wrapped, so it grew without bound over a long-running deployment (harmless numerically for `cos`/`sin`, but meaningless if ever logged or displayed directly). Fixed with an `atan2(sin, cos)` wrap after every predict step (§9.3).
3. **CPU-measurement staleness bug** (§15.5) produced an apparently-impossible "674m CPU under a 100m limit" result in the resource-limit test. Root-caused to `metrics-server` cache lag, not a cgroup anomaly, and fixed by polling CPU through a longer settle window and re-sampling after HTTP retries rather than reusing an early reading.
4. **Single-trial (n=1) benchmarks.** Both `breaking_point.py` and `resource_limit_test.py` now run 3 trials per level and report mean +/- standard deviation (§15.4), which is what surfaced and resolved the apparent non-monotonic latency curve in the original single-trial breaking-point data.
5. **`CRASH_ENDPOINT_ENABLED` defaulted to `"true"` in every committed `k8s/*.yaml` manifest**, contradicting this document's own stated security posture (§17.1) that it "must never be enabled outside a local test cluster." Flipped the default to `"false"`; no test or benchmark script in this repo actually depends on it being on, so this was a pure fix with no behavior change to any existing test.
6. **No CI.** Added `.github/workflows/ci.yml` (§14.1) so the smoke test and fusion-accuracy check run automatically on every push against a freshly-created `kind` cluster, rather than relying entirely on manual runs.
7. **Stray `import math` inside `lidar_sim.py`'s per-cycle loop** (harmless — Python caches module imports — but inconsistent with every other sensor simulator's top-of-file import) moved to the top of the file.

All four regression tests (`test_fusion_accuracy.py`, `test_fault_tolerance.py`) were re-run against the live cluster after the EKF change specifically, to confirm the Jacobian fix didn't change the filter's qualitative behavior — it didn't; both passed with results consistent with §16.2/§16.4.

## 13. Operational Guide

### 13.1 Stand up the cluster from scratch

```bash
# 1. Create the cluster
kind create cluster --name sensor-fusion

# 2. Build the sensor + fusion images
docker build -t sensor-fusion/lidar-sim:latest      -f sensors/Dockerfile.lidar  .
docker build -t sensor-fusion/camera-sim:latest     -f sensors/Dockerfile.camera .
docker build -t sensor-fusion/imu-sim:latest        -f sensors/Dockerfile.imu   .
docker build -t sensor-fusion/gps-sim:latest        -f sensors/Dockerfile.gps  .
docker build -t sensor-fusion/fusion-service:latest -f fusion/Dockerfile      .

# 3. Load them into the kind cluster (no registry needed)
kind load docker-image sensor-fusion/lidar-sim:latest      --name sensor-fusion
kind load docker-image sensor-fusion/camera-sim:latest     --name sensor-fusion
kind load docker-image sensor-fusion/imu-sim:latest        --name sensor-fusion
kind load docker-image sensor-fusion/gps-sim:latest        --name sensor-fusion
kind load docker-image sensor-fusion/fusion-service:latest --name sensor-fusion

# 4. Deploy Mosquitto + all four sensors + the fusion service
kubectl apply -f k8s/

# 5. Verify
kubectl get pods
kubectl logs deployment/fusion-service

# 6. (Optional) install metrics-server, for `kubectl top pod` CPU/memory
#    visibility during the benchmarks -- kind's kubelet uses a self-signed
#    cert, hence the --kubelet-insecure-tls patch.
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
kubectl patch deployment metrics-server -n kube-system --type='json' \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
```

### 13.2 Verification and test scripts

| Script | Purpose | How to run |
|---|---|---|
| `tests/mqtt_smoke_test.py` | Confirms all four sensors are actually publishing | `kubectl port-forward svc/mosquitto 1883:1883` then `python tests/mqtt_smoke_test.py --duration 6` |
| `tests/test_fusion_accuracy.py` | RMSE of fused estimate vs. ground truth, vs. GPS-only baseline | same port-forward, `python tests/test_fusion_accuracy.py --duration 45` |
| `tests/test_timing_stress.py` | Proves the deadline instrumentation itself is correct (unit-level, no cluster needed) | `python tests/test_timing_stress.py` |
| `tests/test_fault_tolerance.py` | Triggers a controlled GPS dropout, verifies staleness detection + bounded RMSE + recovery | `kubectl port-forward` to mosquitto (1883) and the fusion pod (8080), then `python tests/test_fault_tolerance.py --dropout-duration 15` |

### 13.3 Benchmarks

| Script | Purpose | How to run |
|---|---|---|
| `benchmarks/breaking_point.py` | Scales sensor replicas, finds the deadline-miss-rate-vs-load curve | `python benchmarks/breaking_point.py --multipliers 1,2,4,8,16,20,28` |
| `benchmarks/resource_limit_test.py` | Compares miss rate at fixed load under original vs. tightened fusion CPU limit | `python benchmarks/resource_limit_test.py` |
| `benchmarks/chaos_test.py` | Kills the fusion pod and a sensor pod under sustained load, measures recovery | `python benchmarks/chaos_test.py` |

All three benchmark scripts manage their own `kubectl port-forward` subprocesses, scale replicas/patch resources via `kubectl`, and restore the cluster to its 1-replica/original-limits baseline in a `finally` block even on failure.

### 13.4 Retrieving live status without a benchmark script

```bash
kubectl port-forward deployment/fusion-service 18080:8080
curl localhost:18080/timing-stats     # {"total_cycles":..., "miss_rate_pct":..., ...}
curl localhost:18080/fault-events     # {"current_status": {...}, "recent_events": [...]}
kubectl logs -f deployment/fusion-service   # human-readable running summary every ~5s
```

### 13.5 Common operational tasks

- **Rebuild and redeploy a changed component:** rebuild its Docker image, `kind load docker-image` it back into the cluster, then `kubectl rollout restart deployment/<name>` (a plain re-`apply` will not pick up new image *contents* under the same tag — the node's containerd already has that tag cached).
- **Change a resource limit:** either edit the manifest and `kubectl apply -f k8s/<file>.yaml`, or patch live with `kubectl set resources deployment/<name> -c=<container> --limits=cpu=...,memory=... --requests=cpu=...,memory=...` (the latter is what the resource-limit benchmark does, restoring via a fresh `kubectl apply` of the committed manifest afterward).
- **Scale sensor load manually:** `kubectl scale deployment/<sensor>-sim --replicas=N` for each of the four sensor Deployments; always followed by `kubectl rollout status` before trusting the new replica count is actually serving traffic.

## 14. Testing and Validation

| Test | Validates | Pass criteria |
|---|---|---|
| `mqtt_smoke_test.py` | All four sensors publish through the K8s-hosted broker | Non-zero message count on all four topics within the observation window |
| `test_fusion_accuracy.py` | Fusion estimate is more accurate than a single sensor | Fusion RMSE < GPS-only RMSE against shared ground truth |
| `test_timing_stress.py` | The timing instrumentation itself is correct | A deliberately injected over-deadline cycle is classified and counted as a miss, and reflected in the CSV log |
| `test_fault_tolerance.py` | Application-layer staleness handling is correct under a real, controlled outage | System stays up throughout (fusion/state keeps publishing); a stale→recovered event is logged with duration matching the triggered outage; RMSE stays bounded during the outage and recovers after |

**Design principle applied throughout:** verify the measurement mechanism before trusting a measurement it produces. `test_timing_stress.py` exists specifically so the breaking-point benchmark's miss-rate numbers (§16.5) can be trusted — if the instrumentation itself were broken, a clean 0%-miss-rate result would be indistinguishable from a broken counter that never increments.

No unit tests exist for the EKF math in isolation (e.g., a synthetic-data convergence test with a known-analytic answer) — accuracy is validated end-to-end against the ground-truth trajectory instead (§16.2). This is a deliberate scope choice, not an oversight: see §21.

### 14.1 Continuous integration

`.github/workflows/ci.yml` runs `test_timing_stress.py` (no cluster needed) on every push, then a second job stands up a real `kind` cluster from scratch, builds and loads all five images, applies the manifests, and runs `mqtt_smoke_test.py` and `test_fusion_accuracy.py` against it. This closes the gap between "these scripts exist" and "these scripts are actually run automatically" — before this workflow existed, every test and benchmark in this project required a human to manually stand up a cluster and invoke them by hand, so nothing prevented a regression from going unnoticed between manual runs. The full benchmark suite (breaking-point, resource-limit, chaos) is not in CI, since each takes several minutes and deliberately mutates live cluster state (replica counts, resource limits) — those remain manually invoked, as documented in §13.3.

## 15. Experimental Methodology

### 15.1 Breaking-point benchmark methodology

For each load multiplier in an ordered list: scale all four sensor Deployments to that multiplier's replica count, wait for `kubectl rollout status` to confirm readiness, sleep a fixed settle period (8s) to let MQTT reconnect and buffers stabilize, call `/timing-reset` to zero the fusion pod's running statistics, sleep a fixed measurement window (20s), then read `/timing-stats` as that level's result. Stop early if the rollout itself fails to complete (treated as the practical breaking point) or if the miss rate reaches a clearly-failing threshold (20%, to show the trend past the 1% crossing without running indefinitely). Restore all sensors to 1 replica in a `finally` block regardless of outcome.

### 15.2 Resource-limit test methodology

Holds sensor load fixed at one previously-validated level (8x) and varies only the fusion pod's CPU `requests`/`limits` between two conditions (original 150m/750m, tightened 50m/100m), using the same settle-then-reset-then-measure pattern as the breaking-point benchmark, to isolate the CPU-limit variable from the load variable.

### 15.3 Chaos test methodology

Two independent measurement techniques are combined rather than trusting one: `kubectl get pods` (Kubernetes' own view of pod identity/phase, confirming *that* a replacement happened and *whether* `RESTARTS` incremented) and a direct MQTT subscriber recording wall-clock message-arrival timestamps on the relevant topic (`fusion/state` for Phase A, `sensors/lidar` for Phase B), confirming *how long* the actual data-plane outage lasted, independent of what Kubernetes reports about pod phase timing. This combination is what caught the port-forward-doesn't-follow-a-pod issue (§12.6) — the MQTT-based measurement kept working when the HTTP-based one silently broke.

### 15.4 Statistical approach and its limits

All statistics (mean, max, jitter-as-stddev) *within a single trial* are computed via O(1) streaming accumulation (§9.6), not stored-and-post-processed sample sets — deliberately trading exact percentile reporting for unbounded-duration memory safety. `breaking_point.py` and `resource_limit_test.py` each measure **3 independent trials per level** (a fresh `/timing-reset` and sustained measurement window per trial) and report the mean and standard deviation across trials, rather than a single sample. An earlier single-trial (n=1) version of this benchmark produced a non-monotonic average-cycle-duration curve across load levels (duration *decreasing* from 8x to 16x, then rising again at 20x) that was impossible to distinguish from ordinary measurement noise with only one sample per level — moving to 3 trials with reported standard deviation makes that kind of ambiguity visible in the data itself instead of silently hiding it. 3 trials is still a small sample size; it resolves the "is this one weird number a fluke" problem without pretending to full statistical rigor (no confidence intervals, no hypothesis testing).

### 15.5 Environment variance acknowledged

All measurements were taken on a single Windows machine running Docker Desktop, with a single-node `kind` cluster that also hosts Kubernetes system pods (`kube-proxy`, CoreDNS, etcd, the API server, the scheduler, the controller-manager, `kindnet`, `metrics-server`) sharing the same node resources as the application pods being measured. No attempt was made to isolate or pin CPU for reproducibility across different host machines — the absolute numbers (e.g., downtime measurements) are specific to this machine's actual load at the time of measurement, and are reported as real observations of *this* environment, not as portable, machine-independent constants.

An earlier version of `resource_limit_test.py` reported "674m CPU" for the fusion pod while it was throttled under a 100m limit — an apparent 6.7x overshoot of its own cgroup limit that looked, on its face, physically impossible. The actual cause was a measurement-timing bug, not a cgroup/kubelet anomaly: `kubectl top` is backed by `metrics-server`, which only refreshes its cached usage figures on its own resync period (commonly every 15-60s), and the harness was reading that figure only ~10s after tightening the limit — too soon for metrics-server's cache to reflect the new, throttled reality, so it returned a stale sample from just before the limit changed. The fix (§12.6/resource_limit_test.py) polls CPU repeatedly through a longer 30s settle window and, on the pod-unresponsive path, re-samples CPU *after* the HTTP retries have already burned additional wall time, rather than reusing an early reading. Re-running after the fix reports ~99m under the 100m limit, consistent with an actively-throttled cgroup rather than a data-collection artifact — see §16.6.

## 16. Results and Observed Behavior

Every number below was produced by running the described script against the live cluster once the corresponding phase's code was in place, not calculated or estimated.

### 16.1 Sensor smoke test (Phase 1)

6-second window, port-forwarded from the live cluster:

```
sensors/lidar: 60 messages received   (~10 Hz expected)
sensors/camera: 179 messages received (~30 Hz expected)
sensors/imu: 592 messages received    (~100 Hz expected)
sensors/gps: 12 messages received     (~2 Hz expected)
```

Message-count ratios track configured rates as expected, confirming all four sensors publish correctly through the Kubernetes-hosted Mosquitto Service via DNS.

### 16.2 Fusion accuracy vs. single-sensor baseline (Phase 2)

45-second window, after the filter had converged past its initial transient:

```
fusion/state samples: 438, RMSE vs ground truth: 0.443 m
sensors/gps  samples: 79,  RMSE vs ground truth: 0.755 m
fusion improves on GPS-only baseline by 41.4%
```

### 16.3 Baseline deadline timing (Phase 3)

~140s of sustained normal-load operation (all sensors at default rates), 1400 cycles:

```
miss_rate_pct: 0.0
avg_duration_ms: 31.98
worst_case_ms: 83.66
jitter_ms: 31.73
```

0% deadline-miss rate with a comfortable margin (83.7ms worst-case against a 100ms budget) even accounting for real `kind`/Docker Desktop pod scheduling overhead.

### 16.4 Two-layer fault tolerance (Phase 4)

**Application-layer staleness test** — 15s deliberate GPS dropout via `/force-dropout`:

```
fusion/state messages received: 311 (system never stopped publishing)
baseline RMSE (before dropout): 0.590 m
during-dropout RMSE:            0.562 m
post-recovery RMSE:              0.457 m
fault-events: gps stale -> recovered, logged duration 14.0s (triggered: 15.0s)
```

RMSE barely moved during the outage — LiDAR and camera alone constrained position well enough, which is exactly the redundancy multi-sensor fusion is supposed to buy.

**Platform-layer pod-crash test** — `/crash` hit directly on the live `lidar-sim` pod:

```
Before:  lidar-sim-<hash>   1/1   Running   0            119m
After crash:                0/1   Error     0            119m
~10s later:                 1/1   Running   1 (17s ago)  119m
```

Kubernetes restarted the pod automatically (`RESTARTS` 0→1); the fusion pod's own `RESTARTS` stayed at 0 throughout, and its `/fault-events` log shows `lidar stale` at the crash moment and `lidar recovered` ~6.4s later, once the replacement pod resumed publishing — confirming the two layers compose without any code coordinating between them.

### 16.5 Breaking-point benchmark (Phase 5, re-measured with 3 trials/level in §12.7)

Replica-scaling load, `/timing-stats` reset and re-measured at each level, 3 independent trials per level (mean +/- standard deviation; see §15.4 for why this replaced the original single-trial version):

| multiplier | aggregate Hz | trials | miss rate % (mean+/-std) | avg ms (mean+/-std) | worst-case ms (mean+/-std) |
|---|---|---|---|---|---|
| 1x | 142 | 3 | 0.00+/-0.00 | 34.8+/-1.6 | 38.2+/-0.9 |
| 2x | 284 | 3 | 0.00+/-0.00 | 29.7+/-1.5 | 33.4+/-0.7 |
| 4x | 568 | 3 | 0.00+/-0.00 | 22.4+/-0.8 | 28.9+/-1.8 |
| 8x | 1136 | 3 | 0.00+/-0.00 | 18.4+/-0.9 | 23.7+/-2.0 |
| 16x | 2272 | 3 | 0.00+/-0.00 | 0.8+/-0.1 | 5.3+/-6.0 |

32x (128 sensor pods requested) failed to complete its rollout — this `kind` node's default 110-pod scheduling capacity was reached before the fusion service's own deadline was ever threatened. (This re-run used the script's default multiplier list, `1,2,4,8,16,32`, rather than the custom `1,2,4,8,16,20,28` used for the original single-trial run reported in §11.9/§12.5 — same underlying node-capacity ceiling, just crossed at a different point in the doubling sequence: 128 pods here vs. 112 pods there, both comfortably past the 110-pod kubelet default.) **The honest headline finding is unchanged and now more trustworthy for having 3 trials behind it: within every load level this deployment could actually schedule, the deadline-miss rate never left 0%, with the standard deviation across trials at 0.00 for every level** — i.e. this isn't a result that happened to land at 0% once; it lands there consistently.

**An open, currently-unexplained result:** average cycle duration *decreases* monotonically as replica load increases (34.8ms at 1x down to 0.8ms at 16x), the opposite of what a load test would naively expect, and the effect is large and consistent across all 3 trials at each level (low standard deviation at every level except 16x's worst-case figure). This is the same qualitative direction as the anomaly that originally forced the rate-vs-replica redesign in §11.9, but this time it appears *within* the already-redesigned replica-scaling benchmark itself, so it isn't explained by that earlier fix. The measured cycle only covers `predict()` + `update()` + the MQTT publish call — none of that logic does more work as replica count increases, since the fusion service only ever consumes the single latest reading per sensor type regardless of how many replicas are producing it. The leading hypothesis is a CPython thread-scheduling effect (the main cycle thread and the `paho-mqtt` network thread contend for the GIL, and higher message volume changes how that contention resolves) rather than anything in the fusion algorithm itself, but this has not been verified with a profiler and is reported here as an open question rather than a confirmed root cause — see §21.

### 16.6 Resource-limit stress test (Phase 6, re-measured with 3 trials/limit in §12.7)

Fixed 8x sensor load (1136 Hz aggregate), fusion CPU limit varied, 3 independent trials per limit level:

| limits | trials | miss rate % (mean+/-std) | avg ms (mean+/-std) | worst-case ms (mean+/-std) | fusion CPU millicores (mean+/-std) |
|---|---|---|---|---|---|
| original (cpu limit 750m) | 3 | 0.00+/-0.00 | 2.4+/-1.2 | 26.1+/-20.0 | 750+/-1 |
| tightened (cpu limit 100m) | 3 | 100.00+/-0.00 | n/a | n/a | 99+/-1 |

At the tightened limit, the fusion pod became so CPU-starved under sustained load that it **stopped reliably answering its own `/timing-stats` HTTP endpoint** in all 3 trials — a more severe failure mode than a bounded miss-rate percentage, and the pod never crashed or restarted (`RESTARTS` stayed 0) the entire time; it was simply too starved to service connections in time. Original limits were restored immediately after, returning the cluster to its 0%-miss-rate baseline.

The original version of this test reported "674m CPU" at the tightened limit — an apparent 6.7x overshoot of the 100m cgroup limit that looked physically impossible, and was in fact a metrics-server cache-staleness artifact in the measurement harness, not a real cgroup anomaly (root-caused and fixed in §12.7/§15.5). The corrected measurement, ~99m under a 100m limit, is exactly what an actively-throttled cgroup should report, and is consistent across all 3 trials (std of 1m).

### 16.7 Pod-chaos resilience test (Phase 6)

Both phases run under sustained 16x replica load:

```
Phase A (fusion pod kill):  downtime ~0.10s
  fusion-service-<hash-A> -> fusion-service-<hash-B> (new pod, automatic)
  post-recovery: 0% miss rate, cycling normally within seconds

Phase B (sensor pod kill):  actual outage ~0.03s
  lidar-sim-<hash-A> -> lidar-sim-<hash-B> (new pod, automatic)
  restarts: 0 -> 0 (pod replaced, not restarted in place -- expected
  for a Deployment-managed force-delete)
  application-layer fault-events: none logged (actual outage, ~30ms,
  stayed under the 400ms staleness-detection threshold)
```

Both kills self-healed in well under a second, no different from idle-cluster behavior, even with the cluster under sustained load from 16 replicas of every other sensor type. Phase B's empty fault-events log is not a coverage gap — it's direct confirmation that the 400ms staleness threshold (deliberately floored in Phase 4 to absorb normal jitter) correctly stays quiet on a sub-threshold blip, while §16.4's dedicated dropout test already proved the same threshold correctly *does* fire for a genuine, sustained outage.

## 17. Security, Reliability, and Failure Handling

### 17.1 Security posture

This is explicitly a local test/portfolio cluster, not a hardened deployment, and several choices reflect that honestly rather than pretending otherwise:

- **`/crash` is a real, unauthenticated remote-kill endpoint.** Gated behind `CRASH_ENDPOINT_ENABLED`, which defaults to `"false"` in every `k8s/*.yaml` manifest committed to this repository — it must be deliberately flipped to `"true"` (e.g. via `kubectl set env deployment/<name> CRASH_ENDPOINT_ENABLED=true` for the duration of a manual chaos test) rather than being on by default. A bare `GET` request kills the container with no authentication of any kind. This must never be enabled in any deployment reachable outside a trusted local/CI environment — stated here as a hard boundary, not a soft caveat.
- **MQTT broker allows anonymous access** (`allow_anonymous true` in the Mosquitto ConfigMap), with no TLS on the broker connection. Appropriate for a local test cluster; would need authentication, ACLs, and TLS for anything beyond that.
- **No Kubernetes NetworkPolicies restrict pod-to-pod traffic.** Any pod in the cluster can currently reach any other pod's ports (including the sensor control servers' `/crash` endpoint, from any other pod's network namespace).
- **No secrets management** — there are no credentials in this system to manage (anonymous MQTT, no external API calls), so this gap is currently inert, but would need addressing the moment any real authentication is introduced.

### 17.2 Reliability characteristics, observed

- **Mean time to recovery (measured, not modeled):** ~0.10s for a killed fusion pod, ~0.03s for a killed sensor pod, both under sustained 16x load (§16.7). Both figures come from a single observed run each (§15.4), not a distribution.
- **Graceful degradation is bounded per-sensor, not systemic.** A single stale sensor is excluded from updates with covariance widening on just that sensor's constrained state components; the filter continues producing estimates from the remaining sensors.
- **Untested edge case, stated explicitly:** what happens if *all four* sensors go stale simultaneously (e.g., the Mosquitto broker itself dies) is not covered by any test in this project. Based on the code's actual logic, the expected behavior is that the fusion cycle continues running pure prediction (constant velocity/heading extrapolation) with covariance growing unboundedly every cycle via process noise alone — the system would not crash or stall, but accuracy would degrade without bound the longer the outage lasted, and there is currently no maximum-staleness circuit breaker that would, say, hold the last known position instead of extrapolating indefinitely. This is a real, load-bearing gap between "tested behavior" and "code-inferred behavior," disclosed here rather than implied by omission.
- **The message broker itself has no fault-tolerance testing at all.** Every fault-tolerance test in this project (§16.4, §16.7) kills a *sensor* or the *fusion* pod — never Mosquitto. A Mosquitto crash/restart's effect on the system (would sensors and fusion reconnect automatically? how long would that take? does `paho-mqtt`'s default reconnect behavior suffice?) is unmeasured.

### 17.3 Failure-handling matrix

| Failure | Layer | Detected by | Response | Measured? |
|---|---|---|---|---|
| Sensor publishes late/out-of-order | Application | `SensorBuffer` timestamp comparison | Reading silently dropped | Yes (by design, not separately benchmarked) |
| Sensor goes quiet, pod alive | Application | `FaultEventTracker` staleness check | Excluded from update, covariance widened | Yes (§16.4) |
| Sensor pod crashes/hangs | Platform | Kubernetes liveness probe | Pod restarted automatically | Yes (§16.4, §16.7) |
| Fusion pod crashes | Platform | Kubernetes Deployment controller | Pod rescheduled automatically | Yes (§16.7) |
| Fusion pod CPU-starved | Platform (resource limit) | N/A — no active detection | Pod becomes unresponsive; no automatic remediation observed | Yes (§16.6) |
| Cluster node exhausts pod capacity | Platform (scheduling) | `kubectl rollout status` timeout in this project's harness; no in-cluster alerting | New pods stay `Pending` indefinitely | Yes (§16.5) |
| MQTT broker itself fails | Infrastructure | Not instrumented | Unknown | **No** |
| All four sensors stale simultaneously | Application | `FaultEventTracker` (per-sensor, not aggregate) | Pure prediction, unbounded covariance growth (inferred from code, not tested) | **No** |

## 18. Performance, Scalability, and Cost

### 18.1 Per-component resource footprint (as configured in `k8s/*.yaml`)

| Component | CPU request | CPU limit | Memory request | Memory limit |
|---|---|---|---|---|
| mosquitto | 50m | 200m | 32Mi | 128Mi |
| lidar-sim | 20m | 100m | 32Mi | 64Mi |
| camera-sim | 30m | 150m | 32Mi | 64Mi |
| imu-sim | 40m | 200m | 32Mi | 64Mi |
| gps-sim | 20m | 100m | 32Mi | 64Mi |
| fusion-service | 150m | 750m | 64Mi | 128Mi |

Fusion's allocation is deliberately the largest of any single component, and metrics-server observations throughout Phases 5–6 confirm it's not over-provisioned: the fusion pod measured ~750m CPU usage even at 1x baseline load (§16.5) — i.e., it uses close to its full limit essentially all the time, which is why tightening that limit in §16.6 had such an immediate, dramatic effect rather than a gradual one.

### 18.2 Performance summary (cross-referencing §16)

- Steady-state cycle timing: 0% miss rate, low-tens-of-ms average duration, worst case well under the 100ms budget at every tested load level up to the cluster's own scheduling ceiling.
- The dominant real cost inside a fusion cycle is not EKF math (a 4-state EKF's linear algebra is trivial computationally) but MQTT message handling and Python interpreter overhead — indirectly evidenced by the fact that cycle duration scaled with message *volume* (replica count), not with any parameter of the filter itself.

### 18.3 Scalability findings

- **Vertical (per-process) scaling is capped low and by something other than CPU or the deadline.** A single sensor process caps out around 400–500Hz of real publish throughput regardless of configured rate, due to per-message Python/JSON/MQTT overhead — not CPU-bound (measured at 32m of a 200m limit at that ceiling), not deadline-bound. This means "make the sensor try harder" is not a viable scaling lever in this architecture at all (§11.9).
- **Horizontal (replica) scaling works, and is the only lever that produces real additional load**, confirmed linear up to the tested range (4 replicas × 100Hz ≈ 394Hz measured, ≈ 400Hz expected).
- **The system's actual scalability ceiling, measured, is infrastructural, not algorithmic:** a single `kind` node's default 110-pod capacity, reached at 32x replica scale (128 sensor pods) in the re-measured multi-trial data — well before CPU, memory, or the fusion deadline showed any sign of strain (§16.5). Scaling this system further means adding worker nodes, not optimizing the fusion loop.

### 18.4 Cost

This project runs entirely on a single local machine with no cloud spend — "cost" in the traditional sense does not apply. As a rough, explicitly-labeled-as-unmeasured extrapolation only: the aggregate resource footprint at baseline (six pods, roughly 300m CPU / 450Mi memory requested in total across the table in §18.1) is small enough to fit comfortably on the smallest node size offered by any major cloud provider's managed Kubernetes service, meaning a cloud deployment of this exact system at baseline load would likely cost on the order of a single small VM per month — but this has not been tested, priced, or deployed to any cloud provider, and should not be treated as a validated figure.

## 19. Deployment, Monitoring, and Maintenance

### 19.1 Deployment process

Fully manual, scripted via documented `kubectl`/`docker`/`kind` command sequences (§13.1) — there is no CI/CD pipeline. Deploying a code change to any component requires: rebuild that component's Docker image, `kind load docker-image` it into the cluster (this replaces the image content at the same tag inside the node's containerd store), then `kubectl rollout restart deployment/<name>` to force existing pods to pick up the new image (a plain `kubectl apply` alone will not do this, since the manifest's image tag string hasn't changed).

### 19.2 Monitoring

Two bespoke HTTP JSON endpoints (`/timing-stats`, `/fault-events`) on the fusion pod, a periodic stdout summary line every 50 cycles (visible via `kubectl logs -f`), and `kubectl top pod` (via `metrics-server`, optionally installed) for CPU/memory. There is no Prometheus, Grafana, OpenTelemetry, or any other general observability integration — a deliberate scope cut (§21) in favor of purpose-built instrumentation that was faster to reason about for this project's specific claims.

### 19.3 Maintenance and extensibility notes

- **Adding a fifth sensor type** would require: a new simulator following the existing pattern (sample `GroundTruthTrajectory`, add sensor-specific noise, publish JSON to a new topic, run `control_server.py`), a new Dockerfile, a new `k8s/*.yaml` manifest with its own resource requests/limits, a new entry in `fusion_service.py`'s `EXPECTED_INTERVAL_S`/`STALE_WIDEN_INDICES` maps and a new EKF measurement-update method if its measurement model isn't already covered by `update_position`/`update_camera`, and a new entry in every benchmark script's `BASELINE_RATES_HZ`/`DEPLOYMENTS` list.
- **Changing the fusion cycle period** (currently hardcoded at 100ms, `CYCLE_PERIOD_S`) would require updating `TimingTracker`'s `DEADLINE_S` in lockstep, plus re-validating that `STALENESS_MULTIPLIER`/`MIN_STALENESS_THRESHOLD_S` are still sensible relative to the new cycle rate.
- **No configuration management system** (no Helm chart, no Kustomize overlays) — manifests are plain YAML applied directly. Adequate for six static Deployments in one namespace; would not scale well past this project's own size without adopting one.

## 20. Interpretation and Lessons Learned

**The gap between "configured" and "achieved" is where the real bugs live.** Both major benchmark-design defects (the ~400–500Hz per-process throughput ceiling, §11.9, and the MQTT client-ID collision, §12.6) were invisible from reading the code — they only appeared by directly measuring what the system actually delivered versus what it was told to deliver. This is a repeated pattern across the whole project: a cgroup-throttling misdiagnosis (§12.4) was itself corrected only once a *second*, independent measurement tool (`metrics-server`, properly scoped to the pod) was introduced rather than trusted on the first read. The general lesson carried forward: when a distributed system's behavior looks physically implausible (cycle duration *decreasing* under more configured load), the right response is to add a second, more direct measurement, not to rationalize the first one.

**CPU limits under real load fail catastrophically, not gracefully, once exceeded.** The resource-limit test (§16.6) expected a moderate miss-rate increase and instead found the fusion pod couldn't answer its own status endpoint at all. This is a genuinely useful, transferable lesson about Kubernetes CPU limits in general: a CFS-quota-throttled process doesn't slow down proportionally, it can stop making forward progress on *anything*, including the connections meant to report that it's in trouble — which has real implications for how liveness/readiness probes should be designed on any CPU-constrained production service (a probe hitting the same starved process may itself never get a chance to answer, silently defeating its own purpose).

**A cluster's own scheduling limits are a legitimate "breaking point," not a consolation prize.** The original hypothesis was that the fusion algorithm's CPU/timing budget would be the limiting factor. It never was, at any load level this cluster could actually schedule. The project's honest headline finding pivoted, mid-project, from "here's where the algorithm breaks" to "here's where this specific deployment topology breaks, and it's not the algorithm" — and reporting that pivot faithfully (rather than forcing a contrived algorithm-level failure) is itself a demonstration of a real engineering skill: knowing when your original hypothesis was wrong and reporting what you actually found instead.

**Verifying a measurement mechanism before trusting its output is not optional overhead.** `test_timing_stress.py` (§14) exists because a broken counter that silently never increments would have produced an indistinguishable-from-correct "0% miss rate" result at every stage of this project. The same principle shaped the chaos test's dual-measurement design (§15.3): trusting only `kubectl get pods`' view of pod phase would have completely missed the actual finding (a 30ms real outage) in Phase B, reporting only "no fault event logged" with no way to tell whether that meant "the outage was too fast" or "the detection is broken."

**Two independently-designed fault-tolerance layers composing correctly, with zero coordination code between them, is a real architectural payoff — but only because each layer's job is genuinely disjoint from the other's.** The application layer never asks "is the pod alive"; the platform layer never asks "is the data fresh." Because those questions don't overlap, verifying them separately (Phase 4) and then verifying they still hold *together*, under load (Phase 6) required no new integration code — only new tests. This is a direct payoff of the layer-boundary design decision made in §11.8, and it held up under a considerably more adversarial test (sustained 16x load, forced pod kills) than the one it was originally designed against.

## 21. Limitations, Known Issues, Technical Debt, and Future Work

Everything in this section is a deliberate, accepted gap — not an oversight discovered by a reader, but a boundary drawn consciously given the project's actual scope and time budget.

- **Single-node cluster only.** The breaking-point benchmark's actual ceiling (§16.5) was Kubernetes' default 110-pod-per-node limit, not the fusion algorithm. A multi-node `kind` cluster (or a cloud cluster) would push this ceiling out and might finally surface a genuine algorithm-level miss-rate crossing — not attempted, as it would require a materially different local setup or cloud credentials/cost beyond this project's scope.
- **`/crash` endpoint is a real security hazard if ever misconfigured.** Gated behind `CRASH_ENDPOINT_ENABLED`, which defaults to `"false"` in every committed manifest and must be deliberately enabled per-Deployment for a manual chaos test, but the endpoint itself has no authentication — a bare `GET` kills the container once enabled. Must never be enabled in any deployment reachable outside a trusted local/CI environment. No additional hardening (auth token, NetworkPolicy restricting the control-server port) was added, since the honest scope of this project is a local test cluster, not a hardened production system.
- **No persistent storage for timing/fault logs.** Both CSV logs live inside the pod's ephemeral filesystem; a pod restart loses history (in-memory `TimingTracker`/`FaultEventTracker` state resets too, by design). A production system would ship these to persistent storage or a log aggregator instead of relying on `kubectl logs`/`kubectl cp`.
- **No distributed tracing or structured metrics export.** Bespoke JSON endpoints, not Prometheus metrics or OpenTelemetry traces (§19.2) — a deliberate scope cut, but it means the system doesn't plug into any existing monitoring ecosystem.
- **EKF noise covariances are set from known simulator ground truth, not tuned/estimated.** Honest for testing the *pipeline*, but means the filter has never been exercised against the harder, more realistic case of unknown or mis-specified sensor noise — a real deployment would need online noise estimation or offline calibration, neither of which exists here.
- **Single scenario, single vehicle, single trajectory shape.** One fixed closed loop at one constant speed; no test of varying speed, multiple vehicles, or trajectory shapes with sharper dynamics that would stress the unicycle model's linearization error more aggressively.
- **The 400ms staleness threshold is a uniform floor, not per-sensor-tuned.** Set to fix one observed false positive (IMU flapping, §12.4) with the simplest fix that worked, not derived from a principled per-sensor jitter analysis. A tighter, sensor-specific threshold might catch genuine short outages (like Phase B's 30ms gap, §16.7) that the current uniform floor misses by design.
- **The message broker (Mosquitto) itself has zero fault-tolerance testing.** Every chaos/crash test in this project targets a sensor or the fusion pod; a broker crash/restart's effect on reconnection time and data continuity is completely unmeasured (§17.2, §17.3).
- **The "all four sensors stale simultaneously" case is untested,** and its expected behavior (unbounded covariance growth under pure prediction, no circuit breaker) is inferred from code reading, not verified by a test (§17.2).
- **Only 3 repeated trials per benchmark level, not a fuller statistical treatment.** `breaking_point.py` and `resource_limit_test.py` report mean +/- standard deviation over 3 trials per level/limit (§15.4) rather than the single-run numbers from an earlier version of this project — enough to catch the kind of noise-vs-signal ambiguity that motivated the change, but still too few trials for a formal confidence interval or hypothesis test. Individual test scripts (`test_fusion_accuracy.py`, `test_fault_tolerance.py`) remain single-run.
- **No unit-level EKF correctness test against synthetic data with a known analytic answer.** Filter accuracy is validated only end-to-end against the simulated ground truth (§14), not against an isolated, controlled math check.
- **Windows/Docker Desktop-specific operational friction is not addressed in tooling.** `kubectl port-forward` process/port management, and MSYS path-conversion mangling `/sys/fs/cgroup` paths in `kubectl exec` — both real issues hit during development, both worked around ad hoc rather than fixed in the project's own scripts.
- **CI covers the smoke test and fusion-accuracy check only (§14.1).** The full benchmark suite (breaking-point, resource-limit, chaos) still requires a human to invoke manually against a live cluster; none of it runs automatically on every change.
- **Benchmark scripts assume a specific starting state** (1 replica, original resource limits) and restore it in a `finally` block, but a script killed ungracefully can leave the cluster mid-scale — hit at least once during development, requiring manual `kubectl scale`/`kubectl apply` recovery. There is no separate "reset cluster to baseline" command independent of a benchmark script's own cleanup path.
- **No Helm chart or Kustomize overlays** — plain YAML manifests only, adequate at six Deployments in one namespace but not a pattern that scales cleanly to more components or environments (§19.3).

## 22. Conclusion

This project set out to answer a specific question with measurements instead of assumptions: deployed as a real fleet of Kubernetes microservices, does a 100ms sensor-fusion deadline actually hold, and where does it actually break? The answer, backed by every number in §16: the deadline held with real margin at every load level this cluster could schedule (0% miss rate up to 2840 Hz aggregate), the fused estimate was measurably better than any single sensor (41.4% RMSE improvement over GPS alone), both independent fault-tolerance layers self-healed in well under a second even under sustained heavy load, and the system's actual limits — a single-node pod-scheduling cap, and a CPU limit tight enough to silence the service entirely — were found empirically rather than assumed. Several of the project's most useful findings (the per-process MQTT throughput ceiling, the client-ID collision under replica scaling, the total-stall failure mode under CPU starvation) were not anticipated in the original design and were only discovered because the project's own discipline of measuring the real system, rather than trusting its configuration, caught them before they could be reported as false results. That discipline — verify the instrumentation, measure twice with independent methods when a result looks implausible, and report what actually happened even when it isn't what was originally hypothesized — is the project's real deliverable, as much as the fusion pipeline itself.
