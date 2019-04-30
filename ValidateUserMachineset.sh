#!/bin/bash

# Validates that the cluster is healthy.
# Error codes are:
# 0 - success
# 1 - fatal (cluster is unlikely to work)
# 2 - non-fatal (encountered some errors, but cluster should be working correctly)

set -o errexit
set -o nounset
set -o pipefail

function usage {
  echo "usage: $0 <options>"
  echo " -h        this help mesasge"
  echo " -k <file> location of kubeconfig file to use. (default) ${HOME}/.kube/config"
  echo " -t <integer> number of expected nodes in gke on-prem cluster"
  echo " -l <file> location of log file." 
  exit 1
}

while getopts ":h:k:t:l:" opt; do
  case ${opt} in
    k) KUBECONFIG="${OPTARG}"
      ;;
    t) REQUIRED_NUM_NODES="${OPTARG}"
       EXPECTED_NUM_NODES="${OPTARG}"
      ;;
    l) E2E_TEST_LOG="${OPTARG}"
      ;;
    h) usage
      ;;
    *) usage
      ;;
  esac
done

KUBECTL='/usr/bin/kubectl'
CLUSTER_READY_ADDITIONAL_TIME_SECONDS="${CLUSTER_READY_ADDITIONAL_TIME_SECONDS:-30}"

# Make several attempts to deal with slow cluster birth.
return_value=0
attempt=0
# Set the timeout to ~5minutes (10attempts x 30 second).
PAUSE_BETWEEN_ITERATIONS_SECONDS=30
MAX_ATTEMPTS=10
ADDITIONAL_ITERATIONS=$(((CLUSTER_READY_ADDITIONAL_TIME_SECONDS + PAUSE_BETWEEN_ITERATIONS_SECONDS - 1)/PAUSE_BETWEEN_ITERATIONS_SECONDS))
while true; do
  # Pause between iterations of this large outer loop.
  if [[ ${attempt} -gt 0 ]]; then
    sleep 30
  fi
  attempt=$((attempt+1))

  # The "kubectl get nodes -o template" exports node information.
  #
  # Echo the output and gather 2 counts:
  #  - Total number of nodes.
  #  - Number of "ready" nodes.
  #
  # Suppress errors from kubectl output because during cluster bootstrapping
  # for clusters where the master node is registered, the apiserver will become
  # available and then get restarted as the kubelet configures the docker bridge.
  #
  # We are assigning the result of kubectl_retry get nodes operation to the res
  # variable in that way, to prevent stopping the whole script on an error.
  node=$("${KUBECTL}" get nodes --kubeconfig=${KUBECONFIG}) && res="$?" || res="$?"
  if [ "${res}" -ne "0" ]; then
    if [[ "${attempt}" -gt "${last_run:-$MAX_ATTEMPTS}" ]]; then
      echo -e "Failed to get nodes." | sudo tee -a "${E2E_TEST_LOG}"
      exit 1
    else
      continue
    fi
  fi
  found=$(($(echo "${node}" | wc -l) - 1))
  ready=$(($(echo "${node}" | grep -v "NotReady" | wc -l ) - 1))

  if (( "${found}" == "${EXPECTED_NUM_NODES}" )) && (( "${ready}" == "${EXPECTED_NUM_NODES}")); then
    break
  elif (( "${found}" > "${EXPECTED_NUM_NODES}" )); then
    echo -e "Found ${found} nodes, but expected ${EXPECTED_NUM_NODES}. Your cluster may not behave correctly." | sudo tee -a "${E2E_TEST_LOG}"
    break
  elif (( "${ready}" > "${EXPECTED_NUM_NODES}")); then
    echo -e "Found ${ready} ready nodes, but expected ${EXPECTED_NUM_NODES}. Your cluster may not behave correctly." | sudo tee -a "${E2E_TEST_LOG}"
    break
  else
    if [[ "${REQUIRED_NUM_NODES}" -le "${ready}" ]]; then
      echo -e "Found ${REQUIRED_NUM_NODES} Nodes, allowing additional ${ADDITIONAL_ITERATIONS} iterations for other Nodes to join." | sudo tee -a "${E2E_TEST_LOG}"
      last_run="${last_run:-$((attempt + ADDITIONAL_ITERATIONS - 1))}"
    fi
    if [[ "${attempt}" -gt "${last_run:-$MAX_ATTEMPTS}" ]]; then
      echo -e "Detected ${ready} ready nodes, found ${found} nodes out of expected ${EXPECTED_NUM_NODES}. Your cluster may not be fully functional." | sudo tee -a "${E2E_TEST_LOG}"
      "${KUBECTL}" get nodes --kubeconfig=${KUBECONFIG}
      if [[ "${REQUIRED_NUM_NODES}" -gt "${ready}" ]]; then
        exit 1
      else
        return_value=2
        break
      fi
    else
      echo -e "Waiting for ${EXPECTED_NUM_NODES} ready nodes. ${ready} ready nodes, ${found} registered. Retrying." | sudo tee -a "${E2E_TEST_LOG}"
    fi
  fi
done

echo "Running kubectl get nodes..." | sudo tee -a "${E2E_TEST_LOG}"
final_node_list=$("${KUBECTL}" get nodes --kubeconfig=${KUBECONFIG})
echo "${final_node_list}" | sudo tee -a "${E2E_TEST_LOG}"
echo "Found ${found} node(s)." | sudo tee -a "${E2E_TEST_LOG}"

exit "${return_value}"
