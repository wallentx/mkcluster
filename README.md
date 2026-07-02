# mkcluster
An opinionated setup for a kind cluster

Examples:
- `examples/nvidia-dra-smoke.yaml` is a minimal NVIDIA DRA smoke test pod + claim template.
- `examples/goldilocks-values.yaml` configures Goldilocks to manage all namespaces by default.
- `examples/install-goldilocks-clusterwide.sh` installs Goldilocks and labels all current namespaces for `InPlaceOrRecreate`.
- `examples/envoy-gateway-gatewayclass.yaml` creates the Envoy Gateway `GatewayClass`.
- `examples/envoy-gateway-goldilocks-gateway.yaml` creates a minimal Envoy Gateway `Gateway`.
- `examples/envoy-gateway-goldilocks-route.yaml` exposes the Goldilocks dashboard through an `HTTPRoute`.
- `examples/metallb-values.yaml` is a minimal MetalLB Helm values file.
- `examples/metallb-ipaddresspool.yaml` defines a Layer 2 pool for LoadBalancer services.
- `examples/metallb-l2advertisement.yaml` advertises that pool on the local LAN.

Notes:
- Goldilocks only manages top-level controllers such as Deployments, StatefulSets, and DaemonSets. It will not create VPA objects for standalone Pods.
- If a workload already has a manually managed VPA, remove or replace it before letting Goldilocks manage that same workload, or you can end up with multiple VPA objects targeting one controller.
- The Envoy Gateway examples require applying `examples/envoy-gateway-gatewayclass.yaml` before the `Gateway` and `HTTPRoute`.
- On kind, the `Gateway` and `HTTPRoute` define routing, but they do not by themselves make Envoy reachable from your LAN. You still need a way to expose the generated Envoy proxy service, such as `kubectl port-forward`, a load-balancer implementation, or another host/LAN exposure strategy.
- The MetalLB examples expect a spare IP range on the same LAN as your clients. Do not use addresses that your router may hand out via DHCP.
