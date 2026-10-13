# mkcluster

Create a local [kind](https://kind.sigs.k8s.io/) Kubernetes cluster with an attached registry, configurable host ports, and optional NVIDIA GPU support. CPU clusters are the default.

## Prerequisites

Install Bash 4 or newer, `kind`, `kubectl`, and a running local Docker, Podman, or nerdctl/containerd engine for your host OS and architecture. On macOS, use a current Bash on `PATH`; the bundled Bash 3 is too old. mkcluster checks prerequisites and does not install these tools or alter host settings.

Linux hosts need IPv4 forwarding enabled. `--doctor` reports missing prerequisites and the command needed to fix forwarding. Version discovery needs `curl` and network access; a pinned profile avoids version discovery. Creating a cluster still needs access to uncached container images.

## CPU quickstart

Run these commands from this checkout:

1. Check the example configuration and local prerequisites:

   ```sh
   ./mkcluster --profile examples/local.profile --doctor
   ```

2. Resolve the node image, save a reusable lock, and inspect the generated kind YAML:

   ```sh
   ./mkcluster --profile examples/local.profile --dry-run --write-lock local.lock > local.kind.yaml
   cat local.kind.yaml
   ```

3. Create the cluster from those settings:

   ```sh
   ./mkcluster --profile local.lock
   kubectl --kubeconfig ./local.kubeconfig --context kind-local get nodes
   ```

4. After correcting an interrupted setup, finish configuration of the existing cluster:

   ```sh
   ./mkcluster --profile local.lock --resume
   ```

The example creates one control plane and one worker, binds the API to `127.0.0.1:6443`, publishes HTTP/HTTPS on host ports `8080`/`8443`, and serves the registry at `localhost:5001`. Publishing ports does not install an ingress controller. Use `--provider podman` or `--provider nerdctl` consistently on every command if Docker is not your provider.

`--resume` requires the existing cluster and the original settings. It reuses the nodes, exports kubeconfig, reconnects/configures the registry, and reapplies the selected add-ons. It does not change topology, upgrade node images, or add GPU device mounts to an existing CPU cluster. Keep the same provider, name, worker count, node image, feature gates, GPU mode, and host ports. Resume checks existing node counts, image identities, port bindings, and required GPU mounts before reconfiguring resources; mismatches stop setup. A stopped compatible registry is restarted; an incompatible port binding produces an error. Existing resources are preserved when setup fails.

## Configuration and repeatability

`./mkcluster --help` works offline. Configuration precedence is CLI options, then environment variables, then profile values, then built-in defaults. `examples/local.profile` demonstrates the supported literal format:

```text
MKCLUSTER_NAME=local
MKCLUSTER_API_SERVER_ADDRESS=127.0.0.1
MKCLUSTER_HTTP_PORT=8080
MKCLUSTER_HTTPS_PORT=8443
```

Profiles accept known `MKCLUSTER_*` keys, blank lines, and full-line `#` comments. Values are literal: do not use `export`, shell quotes, `$HOME`, `~`, command substitutions, or inline comments. Use actual paths instead. Unknown and duplicate keys fail validation. See the example and generated lock for the supported settings.

| Setting | CLI option | Profile/environment key |
| --- | --- | --- |
| Runtime | `--provider` | `MKCLUSTER_PROVIDER` |
| Kubernetes node image | `--node-image` | `MKCLUSTER_KIND_NODE_IMAGE` |
| Kubeconfig path | `--kubeconfig` | `MKCLUSTER_KUBECONFIG` |
| HTTP / HTTPS host ports | `--http-port` / `--https-port` | `MKCLUSTER_HTTP_PORT` / `MKCLUSTER_HTTPS_PORT` |
| Registry name / host port | `--registry-name` / `--registry-port` | `MKCLUSTER_REGISTRY_NAME` / `MKCLUSTER_REGISTRY_PORT` |

An explicit provider is passed to both kind and container operations. `--kubeconfig FILE` applies to kind, kubectl, and Helm; forwarding `-- --kubeconfig FILE` is also normalized to this shared setting. Conflicting explicit paths fail. Use mkcluster's own name and image options; forwarded arguments cannot replace its generated kind configuration.

`--doctor` checks local prerequisites without creating resources or changing host settings. `--dry-run` prints the kind configuration without creating resources; it may resolve versions over the network and validate requested GPU support. Adding `--write-lock FILE` explicitly writes a resolved profile, including the selected node image and NVIDIA chart version when GPU mode is enabled. A lock requires an image ending in `@sha256:<64 hex digits>`, a known kind version, and no arbitrary forwarded kind arguments.

Reuse the lock with `--profile FILE`. To select an image manually, pass `--node-image` with an actual digest from the kind release documentation. The lock records the installed kind version; a normal setup or doctor run checks that the installed binary matches the recorded version. For dry-run without kind installed, set `MKCLUSTER_KIND_VERSION` explicitly when writing a lock. A lock does not install tools or pin every transitive image used by Kubernetes, the registry, or a Helm chart. Environment variables and explicit CLI flags can still override a lock.

## NVIDIA GPU clusters

GPU support is opt-in. `--gpu` installs the NVIDIA DRA driver and labels workers `gpu=true`; `--gpu-dra` also enables the experimental `DRAPartitionableDevices` and `DRAConsumableCapacity` feature gates. Choose a compatible Kubernetes release and NVIDIA DRA chart for the features you enable. `--gpu-dra` overrides inherited disabled DRA gates; explicitly disabling those gates on the CLI is an error. Use `--gpu` when selecting custom gates.

Before any NVIDIA configuration or resource creation, mkcluster requires an x86-64 or ARM64 Linux host where `nvidia-smi --query-gpu=uuid --format=csv,noheader` succeeds and returns at least one valid NVIDIA GPU UUID. Empty, invalid, or failed detection stops the operation. CPU mode performs no NVIDIA detection or installation. macOS GPU passthrough and Podman GPU mode are unsupported.

The GPU runtime must be local to the host whose devices and libraries are mounted. In addition to a working NVIDIA host driver, install Helm and the NVIDIA Container Toolkit, and configure these prerequisites yourself:

| Prerequisite | Required configuration |
| --- | --- |
| Host devices and libraries | `nvidia-container-runtime`, `ldconfig`, `readlink`, `/dev/nvidiactl`, and at least one `/dev/nvidia<N>` device |
| NVIDIA runtime config | `/etc/nvidia-container-runtime/config.toml`: root-level `accept-nvidia-visible-devices-as-volume-mounts = true` |
| Docker GPU provider | Reachable local daemon with `nvidia` as its default runtime |
| nerdctl GPU provider | `/etc/containerd/config.toml`: `default_runtime_name = "nvidia"` and `BinaryName = "/usr/bin/nvidia-container-runtime"` |

Validate first, then resolve and create a dedicated GPU cluster:

```sh
./mkcluster --profile examples/local.profile --name gpu-local --gpu --doctor
./mkcluster --profile examples/local.profile --name gpu-local --gpu --dry-run --write-lock gpu-local.lock > gpu-local.kind.yaml
./mkcluster --profile gpu-local.lock
```

The GPU example shares the CPU example's ports and registry; use it instead of the CPU example or choose distinct ports. Replace `--gpu` with `--gpu-dra` in both preparation commands to opt into the experimental gates. Use `MKCLUSTER_NVIDIA_DRA_DRIVER_CHART_VERSION` to select a chart explicitly. Setup waits for cluster nodes, the NVIDIA DRA daemonset, and published GPU devices before reporting success. If an install or readiness check fails, inspect the reported pods and retry with the same lock and `--resume`.

`examples/nvidia-dra-smoke.yaml` supplies a GPU claim and test pod. Review it against your selected DRA release before applying it to the GPU cluster. `--repair-gpu-mounts` refreshes mounted worker NVIDIA libraries and restarts the DRA pods after host driver changes; it also requires successful hardware detection.

## Optional add-on examples

These examples are applied separately from mkcluster:

| Add-on | Files and considerations |
| --- | --- |
| Goldilocks | `examples/goldilocks-values.yaml` and `examples/install-goldilocks-clusterwide.sh` enable management across namespaces. Goldilocks targets top-level controllers, not standalone Pods. Remove or replace overlapping manually managed VPAs first. |
| Envoy Gateway | Apply `examples/envoy-gateway-gatewayclass.yaml` before `examples/envoy-gateway-goldilocks-gateway.yaml` and `examples/envoy-gateway-goldilocks-route.yaml`. Routing resources still need an exposed Envoy proxy service, such as port forwarding or a load balancer. |
| MetalLB | `examples/metallb-values.yaml`, `examples/metallb-ipaddresspool.yaml`, and `examples/metallb-l2advertisement.yaml` illustrate a Layer 2 pool. Replace the example addresses with unused addresses on your clients' LAN, outside your router's DHCP allocation. |

## Development checks

```sh
bash -n mkcluster
shellcheck mkcluster
python3 -m unittest discover -s tests -v
```

The regression suite uses mocked host and cluster commands. It checks argument handling, generated configuration, GPU gating, and recovery paths without provisioning a cluster. Real Docker/Podman/nerdctl integration and GPU workload validation still require suitable hosts; passing the mock suite does not establish those runtime results.
