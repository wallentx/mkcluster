#!/usr/bin/env bash
set -euo pipefail

GOLDILOCKS_NAMESPACE="${GOLDILOCKS_NAMESPACE:-goldilocks}"
GOLDILOCKS_RELEASE="${GOLDILOCKS_RELEASE:-goldilocks}"
GOLDILOCKS_VALUES_FILE="${GOLDILOCKS_VALUES_FILE:-examples/goldilocks-values.yaml}"
GOLDILOCKS_UPDATE_MODE="${GOLDILOCKS_UPDATE_MODE:-InPlaceOrRecreate}"

if ! command -v helm >/dev/null 2>&1; then
  echo "helm is required" >&2
  exit 1
fi

if ! command -v kubectl >/dev/null 2>&1; then
  echo "kubectl is required" >&2
  exit 1
fi

if [[ ! -f "${GOLDILOCKS_VALUES_FILE}" ]]; then
  echo "values file not found: ${GOLDILOCKS_VALUES_FILE}" >&2
  exit 1
fi

cat <<EOF
This will install Goldilocks cluster-wide and label all current namespaces with:
  goldilocks.fairwinds.com/enabled=true
  goldilocks.fairwinds.com/vpa-update-mode=${GOLDILOCKS_UPDATE_MODE}

If you already have a manually managed VPA targeting a workload, remove it first
or Goldilocks may create a second VPA for the same controller.
EOF

helm repo add fairwinds-stable https://charts.fairwinds.com/stable >/dev/null 2>&1 || true
helm repo update

helm upgrade -i "${GOLDILOCKS_RELEASE}" fairwinds-stable/goldilocks \
  --namespace "${GOLDILOCKS_NAMESPACE}" \
  --create-namespace \
  -f "${GOLDILOCKS_VALUES_FILE}"

mapfile -t namespaces < <(kubectl get ns -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')

for ns in "${namespaces[@]}"; do
  kubectl label namespace "${ns}" goldilocks.fairwinds.com/enabled=true --overwrite
  kubectl label namespace "${ns}" "goldilocks.fairwinds.com/vpa-update-mode=${GOLDILOCKS_UPDATE_MODE}" --overwrite
done

cat <<EOF
Goldilocks installation complete.

Check:
  kubectl get pods -n ${GOLDILOCKS_NAMESPACE}
  kubectl get vpa -A
EOF
