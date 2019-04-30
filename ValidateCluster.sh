#!/bin/bash

# Validate the cluster by using diagnose cluster command.

set -o nounset
set -o pipefail

function usage {
  echo "usage: $0 <options>"
  echo " -h        this help mesasge"
  echo " -k <file> location of kubeconfig file to use. (default) ${HOME}/.kube/config"
  exit 1
}

while getopts ":h:k:" opt; do
  case ${opt} in
    k) KUBECONFIG="${OPTARG}"
      ;;
    h) usage
      ;;
    *) usage
      ;;
  esac
done

USE_SUDO="${USE_SUDO:-true}"
CLUSTER_NAME="${CLUSTER_NAME:-}"


# Make several attempts to deal with slow cluster birth.
attempt=0
# Set the timeout to ~5minutes (10attempts x 30 second).
PAUSE_BETWEEN_ITERATIONS_SECONDS=30
MAX_ATTEMPTS=10
while true; do
  if [[ ${attempt} -gt 0 ]]; then
    sleep ${PAUSE_BETWEEN_ITERATIONS_SECONDS}
  fi
  attempt=$((attempt+1))

  if [[ "${attempt}" -gt "${MAX_ATTEMPTS}" ]]; then
    echo "Time out validating cluster ${CLUSTER_NAME}"
    exit 1
  fi

  cluster_name_arg=""
  if [ -n "$CLUSTER_NAME" ]; then
    cluster_name_arg="--cluster-name ${CLUSTER_NAME}"
  fi
  echo $cluster_name_arg
  if [ "${USE_SUDO}" == "true" ]; then
    sudo ${HOME}/bin/gkectl diagnose cluster \
        --kubeconfig $KUBECONFIG ${cluster_name_arg}
  else
    ${HOME}/bin/gkectl diagnose cluster \
        --kubeconfig $KUBECONFIG ${cluster_name_arg}
  fi
  exit_code=$?
  if [ "${exit_code}" -eq 0 ]; then
    break
  fi
  echo "Retry ${attempt} in ${PAUSE_BETWEEN_ITERATIONS_SECONDS} seconds..."
done
