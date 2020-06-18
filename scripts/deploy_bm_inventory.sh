#!/usr/bin/env bash
set -euo pipefail

source scripts/utils.sh


export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/config}
export NO_EXTERNAL_PORT=${NO_EXTERNAL_PORT:-n}
export SERVICE_NAME=bm-inventory
export INVENTORY_URL=$(get_main_ip)
export INVENTORY_PORT=${INVENTORY_PORT:-6000}

mkdir -p build

print_log "Updating bm_inventory params"
skipper run discovery-infra/update_bm_inventory_cm.py
skipper run	"make -C bm-inventory/ deploy-all" ${SKIPPER_PARAMS} DEPLOY_TAG=${DEPLOY_TAG}

print_log "Wait till ${SERVICE_NAME} api is ready"
wait_for_url_and_run "$(minikube service ${SERVICE_NAME} --url -n assisted-installer)" "echo \"waiting for ${SERVICE_NAME}\""

print_log "Starting port forwarding for deployment/${SERVICE_NAME}"
wait_for_url_and_run "http://${INVENTORY_URL}:${INVENTORY_PORT}" "spawn_port_forwarding_command ${SERVICE_NAME} ${INVENTORY_PORT}"
print_log "${SERVICE_NAME} can be reached at http://${INVENTORY_URL}:${INVENTORY_PORT} "
print_log "Done"
