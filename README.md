# Real-Time Sensor Fusion Middleware

A Kubernetes-native system that fuses four noisy, asynchronous simulated vehicle sensors (LiDAR, camera, IMU, GPS) into one real-time state estimate with an Extended Kalman Filter, and proves — with numbers measured on a live cluster — where its 100ms deadline actually breaks.

For the full reasoning behind every decision in this project (alternatives considered, bugs found and fixed, every measured result), see [DESIGN.md](DESIGN.md).

## Problem and motivation

Self-driving systems fuse multiple sensors because no single sensor is good enough alone: GPS drops out, cameras can't judge depth, IMUs drift. Combining them under a hard real-time deadline is the actual engineering problem — and most "sensor fusion" demos test that claim in a single local process, where there's no real scheduling, no real network hop, and no real resource contention to violate it.

This project deploys every sensor and the fusion service as its own Kubernetes microservice, with real CPU/memory limits, from the first line of code — so every claim about "real-time" and "fault-tolerant" is a cluster-measured fact, not an assumption. The payoff: the project also empirically finds its own breaking point, instead of just asserting one.

## Key features

- **Extended Kalman Filter fusion core** on a fixed 100ms cycle, combining four sensors with genuinely different noise characteristics and rates.
- **Deadline timing instrumentation** — every fusion cycle is timestamped and classified on-time/missed, queryable live over HTTP.
- **Two-layer fault tolerance**: the EKF handles a sensor going quiet (covariance widening, graceful exclusion); Kubernetes liveness probes handle a sensor pod dying outright — tested independently and together, under load.
- **Automated breaking-point benchmark** that scales real sensor load on the live cluster and measures the deadline-miss rate.
- **Resource-limit and pod-chaos stress tests** that prove Kubernetes' CPU limits and pod scheduling have real, measurable effects on the system's guarantees.

## Architecture

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

**Flow:** each sensor samples a shared ground-truth trajectory, adds its own noise, and publishes JSON over MQTT → the fusion service subscribes to all four topics, runs predict+update every 100ms, and publishes the fused estimate to `fusion/state` → timing and fault-event stats are queryable live over HTTP on the fusion pod.

## Tech stack

- **Language:** Python 3.11
- **Fusion math:** NumPy (Extended Kalman Filter)
- **Messaging:** MQTT (Eclipse Mosquitto broker)
- **Orchestration:** Kubernetes (`kind`), Docker
- **Observability:** `metrics-server`, bespoke HTTP/JSON status endpoints, CSV logs
- **Benchmarking/plotting:** matplotlib, `kubectl` scripting (Python `subprocess`)

## Installation and setup

Requires Docker Desktop, `kubectl`, and `kind` installed locally.

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
#    visibility during the benchmarks
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
kubectl patch deployment metrics-server -n kube-system --type='json' \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
```

Local Python dependencies for running test/benchmark scripts from your host:

```bash
pip install -r requirements.txt
```

## Usage examples

Query the fusion service's live status (after `kubectl port-forward deployment/fusion-service 18080:8080`):

```bash
curl localhost:18080/timing-stats
# {"total_cycles": 1400, "miss_count": 0, "miss_rate_pct": 0.0,
#  "avg_duration_ms": 31.98, "worst_case_ms": 83.66, "jitter_ms": 31.73}

curl localhost:18080/fault-events
# {"current_status": {"lidar": false, "camera": false, "imu": false, "gps": false},
#  "recent_events": [{"sensor": "gps", "event": "recovered", "timestamp": ..., "duration_s": 14.0}]}
```

Run the breaking-point benchmark against the live cluster:

```bash
python benchmarks/breaking_point.py --multipliers 1,2,4,8,16,20,28
```

Trigger a controlled sensor dropout for fault-tolerance testing:

```bash
kubectl port-forward deployment/gps-sim 18081:8080
curl "localhost:18081/force-dropout?duration=15"
```

## Results and metrics

All numbers below were measured by running the system on a live `kind` cluster — see [DESIGN.md](DESIGN.md) for full methodology.

| Metric | Result |
|---|---|
| Fusion accuracy vs. GPS-only baseline | 0.443m RMSE vs. 0.755m RMSE — **41.4% improvement** |
| Baseline deadline-miss rate (normal load) | **0%** over 1400 cycles, avg 32ms, worst-case 83.7ms (100ms budget) |
| Deadline-miss rate under scaled load | **0.00% +/- 0.00%** (mean +/- stdev over 3 trials) at every load level the cluster could schedule, up to 2272 Hz aggregate (16x baseline) |
| Practical breaking point found | Kubernetes' own 110-pod node capacity (32x replicas / 128 pods requested) — not the fusion algorithm |
| Resource-limit sensitivity | Cutting fusion's CPU limit 750m → 100m took it from 0% miss rate to a complete stall in all 3 trials |
| Pod-kill recovery time (fusion pod, under load) | **~0.10s** downtime, fully automatic |
| Pod-kill recovery time (sensor pod, under load) | **~0.03s** outage, fully automatic |
| Application-layer fault recovery | 15s GPS dropout absorbed with RMSE barely moving (0.59m → 0.56m), correctly logged |

Both benchmarks now run 3 independent trials per level and report mean +/- standard deviation rather than a single sample — see [DESIGN.md](DESIGN.md) §15.4 for why (an earlier single-trial version produced a non-monotonic latency curve that was impossible to distinguish from measurement noise with n=1).

## Testing

| Script | What it validates |
|---|---|
| `tests/mqtt_smoke_test.py` | All four sensors are actually publishing through the cluster |
| `tests/test_fusion_accuracy.py` | Fused estimate beats a single-sensor baseline |
| `tests/test_timing_stress.py` | The deadline instrumentation itself correctly detects a missed deadline (unit-level, no cluster needed) |
| `tests/test_fault_tolerance.py` | A controlled sensor outage is detected, bounded, and recovered from |

```bash
python tests/test_timing_stress.py                    # no cluster needed
kubectl port-forward svc/mosquitto 1883:1883           # for the rest
python tests/mqtt_smoke_test.py --duration 6
python tests/test_fusion_accuracy.py --duration 45
python tests/test_fault_tolerance.py --dropout-duration 15
```

Benchmarks (`benchmarks/breaking_point.py`, `resource_limit_test.py`, `chaos_test.py`) double as end-to-end / load / chaos tests — each manages its own cluster state and restores the baseline when done.

### CI

`.github/workflows/ci.yml` runs on every push/PR: a `unit-tests` job runs the standalone timing self-test, then a `cluster-smoke-test` job stands up a real `kind` cluster, builds and loads all five images, applies the manifests, and runs the MQTT smoke test and the fusion-accuracy check against it — so "all four sensors publish" and "fusion beats single-sensor baseline" are verified automatically on every change, not just asserted in this README.

## Project structure

```
sensors/    Sensor simulators (LiDAR, camera, IMU, GPS) + shared trajectory + control server
fusion/     EKF core, fusion service, timing instrumentation, fault-event tracking
k8s/        Kubernetes manifests, one per Deployment
tests/      Smoke, accuracy, timing-instrumentation, and fault-tolerance tests
benchmarks/ Breaking-point, resource-limit, and pod-chaos test harnesses
```

## Design decisions and tradeoffs

- **`kind` over minikube/a cloud cluster** — images load straight into the node with no registry round-trip, which dominated iteration speed across six build phases. Tradeoff accepted: a single node's default 110-pod capacity became the system's actual measured breaking point.
- **MQTT over gRPC/Kafka/ROS2** — lightweight pub/sub matches the "many independent sensors, one broker" topology without point-to-point wiring or a heavyweight durable-log system.
- **EKF state is `[x, y, v, heading]`, not `[x, y, vx, vy]`** — the more obvious Cartesian-velocity state would make every sensor's measurement model linear, at which point a plain Kalman Filter (not an EKF) would be the honest choice. The unicycle state makes both the prediction step and the camera's measurement genuinely nonlinear, which is the actual, defensible reason this needs an EKF.
- **IMU as a control input, not a fourth measurement** — the standard INS/GPS pattern: high-rate IMU propagation between lower-rate position corrections.
- **Sensor load is scaled via Kubernetes replica count, not a rate parameter** — raising a sensor's configured publish rate doesn't raise real throughput past ~400-500Hz (a single Python process's own overhead ceiling); replica scaling is what actually stresses the pipeline, and is more true to the project's Kubernetes-native thesis.

Full alternatives-considered reasoning for every decision above, plus every bug found along the way, is in [DESIGN.md](DESIGN.md).

## Limitations and future improvements

- Single-node cluster only — a multi-node setup might surface a genuine algorithm-level breaking point instead of the node's pod-capacity limit.
- No persistent storage for timing/fault logs (ephemeral pod filesystem only); no Prometheus/Grafana/OpenTelemetry integration.
- EKF noise covariances are set from known simulator ground truth, not tuned or estimated online.
- The message broker itself has never been chaos-tested (only sensor and fusion pods are killed in testing).
- Both benchmarks (`breaking_point.py`, `resource_limit_test.py`) run each load/limit level over 3 independent trials and report mean +/- standard deviation, not a single sample — but 3 trials is still a small sample; more trials would tighten the confidence further.
- The `/crash` debug endpoint has no authentication, is disabled by default (`CRASH_ENDPOINT_ENABLED=false` in every `k8s/*.yaml` manifest), and must never be enabled outside a local test cluster.
- CI covers the MQTT smoke test and fusion-accuracy check on every push, but not the full benchmark suite (breaking-point/resource-limit/chaos tests still need to be run manually against a live cluster — see Testing above).

Full list, with the reasoning behind each, in [DESIGN.md](DESIGN.md).

## Deployment

This project targets a local `kind` cluster only (see Installation and setup above) — there is no cloud deployment or hosted demo. `.github/workflows/ci.yml` (see Testing above) verifies the system stands up and works correctly on every push, but doesn't deploy anywhere persistent. Deploying a code change locally: rebuild the affected image, `kind load docker-image` it back into the cluster, then `kubectl rollout restart deployment/<name>` (a plain re-`apply` won't pick up new image contents under an unchanged tag).

## License

MIT — see [LICENSE](LICENSE).
