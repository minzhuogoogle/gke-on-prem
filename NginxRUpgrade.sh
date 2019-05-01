#!/bin/bash

# nginx:1.15-alpine
# nginx:1.7.9
# nginx:1.10.1

KUBECONFIG="${KUBECONFIG:-${HOME}/.kube/config}"
KUBECTL="${KUBECTL:-/usr/bin/kubectl}"
LOAD_BALANCER_IP="${LOAD_BALANCER_IP:-}"
TEST_YAML="${TEST_YAML:-nginx-scale-test.yaml}"
CLEANUP_ON_EXIT="true"

set -e

function usage {
  echo "usage: $0 <options>"
  echo " -h        this help mesasge"
  echo " -k <file> location of kubeconfig file to use. (default) ${HOME}/.kube/config"
  echo " -l <ip>   ip address to configure on the load balancer"
  echo " -n        don't cleanup service on exit"
  exit 1
}

while getopts ":hk:l:nc:" opt; do
  case ${opt} in
    k) KUBECONFIG="${OPTARG}"
      ;;
    l) LOAD_BALANCER_IP="${OPTARG}"
      ;;
    n) CLEANUP_ON_EXIT="false"
      ;;
    h) usage
      ;;
    \?) usage
      ;;
  esac
done

function log {
  echo "`date +'%b %d %T.000'`: INFO: $@"
}

function Check_Response {
  GOT=${1}
  EXPECTED=${2}
  if [[ "${GOT}" != "${EXPECTED}" ]]; then
    log "Expected ${EXPECTED} but got ${GOT}."
    return 1
  else
    log "Got ${GOT} as expected."
    return 0
  fi
}

function Cleanup {
  # we remove the services after the test
  "${KUBECTL}" --kubeconfig ${KUBECONFIG} delete -f ${TEST_YAML} \
  | while read line; do log ${line}; done
  rm ${TEST_YAML}

}

if [ "${CLEANUP_ON_EXIT}" = "true" ]; then
  trap Cleanup EXIT
fi

function WaitforPODS {
  NUM_PODS=0
  EXPECTED_PODS=${1}
  WAITED=0
  while [[ ${NUM_PODS} -lt ${EXPECTED_PODS} &&  ${WAITED} -lt 30 ]]
  do
    sleep 3
    NUM_PODS=`"${KUBECTL}" --kubeconfig ${KUBECONFIG} \
      get pods \
      | grep 'nginx-test' \
      | grep Running \
      |  wc -l`
      log "Waiting for pods, ${NUM_PODS} of ${EXPECTED_PODS} created."
    WAITED=$((WAITED+10))
  done
  if (( "${NUM_PODS}" == "${EXPECTED_PODS}" )) ; then
    log "Found 10 Pods for NGNIX-TEST"
    return 0
  fi
  log "Fail to have 10 Pods for NGNIX-TEST"
  return 1
}



function NumberOfPods {
  NUM_PODS=`"${KUBECTL}" --kubeconfig ${KUBECONFIG} \
      get pods \
      | grep 'nginx-test' \
      | grep Running \
      |  wc -l`
  return $NUM_PODS
}

function WaitforContainerVER {
  EXPECTED_VERSION=${1}
  EXPECTED_PODS=${2}
  WAITED=0
  while [[ ${WAITED} -lt 30 ]]
  do
    sleep 3
    NUM_PODS=`"${KUBECTL}" --kubeconfig ${KUBECONFIG} \
      get pods \
      | grep 'nginx-test' \
      | grep Running \
      |  wc -l`
      log "Waiting for pods, ${NUM_PODS} of ${EXPECTED_PODS} created."
    WAITED=$((WAITED+10))
  done
  if (( "${NUM_PODS}" == "${EXPECTED_PODS}" )) ; then
    log "Found 10 Pods for NGNIX-TEST"
    return 0
  fi
  log "Fail to have 10 Pods for NGNIX-TEST"
  return 1
}

 
function RollingUpgrade {
  VERSION=${1}
  LOAD_BALANCER_IP=${2}
  log "Start Rolling upgrade for NGINX"
  "${KUBECTL}" --kubeconfig ${KUBECONFIG} set  image deployments/nginx-test nginx-test=nginx:${VERSION}
  WAITED=0
  UPGRADE_DONE=0
  while [[ ${WAITED} -lt 10 && ${UPGRADE_DONE} -eq 0 ]]
  do
    REPEAT=0
    NUM_PODS=0
    while [[ ${REPEAT} -lt 300  && ${NUM_PODS} -ne 3 ]] 
    do 
      RESP1=`curl -s http://${LOAD_BALANCER_IP}  | head -4 | tail -1`
      log "http response:$RESP1"
      Check_Response "${RESP1}", "<title>Welcome to nginx!</title>,"
      LB_CHECK_RESULT=$?
      if [ ${LB_CHECK_RESULT} -ne 0 ]; then
        log "Service is interruptted during upgrade to ${VERSION}"
        ping -c 1 ${LOAD_BALANCER_IP} 2>&1 \
           | while read line; do log ${line}; done 
        RESULT_PING=$?
        if (( ${RESULT_PING} -ne 0  )); then 
          log "${LOAD_BALANCER_IP} is not reachable"
          return 1
        fi
        RESP2=`curl -s http://${LOAD_BALANCER_IP}  | head -4 | tail -1`
        log "http response:$RESP2"
        Check_Response "${RESP2}", "<title>Welcome to nginx!</title>,"
        LB_CHECK_RESULT=$?
        if [ ${LB_CHECK_RESULT} -ne 0 ]; then
            log "Service is interruptted during upgrade to ${VERSION} after retry"
            return 1
        fi
      fi
      NumberOfPods
      NUM_PODS=$?
      log "Number of running pod ${NUM_PODS}"
      REPEAT=$((REPEAT+1))
      sleep 1
    done

    UPGRADE_STATUS=`"${KUBECTL}" rollout status deployments/nginx-test | grep "successfully rolled out" `
    echo $UPGRADE_STATUS
    if [[ $UPGRADE_STATUS == *"nginx-test"* ]]; then
      log "Rolling Upgrade to ${VERSION} is done!"
      UPGRADE_DONE=1
    fi

    RESP1=`curl -s http://${LOAD_BALANCER_IP}  | head -4 | tail -1`
    log "http response:$RESP1"
    Check_Response "${RESP1}", "<title>Welcome to nginx!</title>,"
    LB_CHECK_RESULT=$?
    if [ ${LB_CHECK_RESULT} -ne 0 ]; then
        log "Service is  interruptted during upgrade to ${VERSION}"
        ping -c 1 ${LOAD_BALANCER_IP} 2>&1 \
           | while read line; do log ${line}; done
        RESULT_PING=$?
        if [ ${RESULT_PING} -ne 0  ]; then 
          log "${LOAD_BALANCER_IP} is not reachable"
          return 1
        fi
        RESP2=`curl -s http://${LOAD_BALANCER_IP}  | head -4 | tail -1`
        log "http response:$RESP2"
        Check_Response "${RESP2}", "<title>Welcome to nginx!</title>,"
        LB_CHECK_RESULT=$?
        if [ ${LB_CHECK_RESULT} -ne 0 ]; then
            log "Service is interruptted during upgrade to ${VERSION} after retry"
            return 1
        fi
      fi
 
    WAITED=$((WAITED+1))
  done
}

if [ "${LOAD_BALANCER_IP}" == "" ]; then
  log "Please specify an load balancer IP using the -l option, aborting!"
  exit 1
fi

log "Generating test service deployment"
echo "
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nginx-test
  namespace: default
  labels:
    app: nginx-test
spec:
  replicas: 3
  selector:
    matchLabels:
      app: nginx-test
  template:
    metadata:
      labels:
        app: nginx-test
    spec:
      containers:
      - name: nginx-test
        image: nginx:1.7.9
        ports:
        - containerPort: 80
---
apiVersion: v1
kind: Service
metadata:
  name: nginx-test
  namespace: default
spec:
  type: LoadBalancer
  ports:
  - port: 80
    protocol: TCP
    targetPort: 80
  selector:
    app: nginx-test
  loadBalancerIP: ${LOAD_BALANCER_IP}
" | tee ${TEST_YAML}

# We first have to deploy the service before checking for it on load balancer
"${KUBECTL}" --kubeconfig ${KUBECONFIG} apply -f ${TEST_YAML} \
  | while read line; do log ${line}; done

NUM_PODS=0
EXPECTED_PODS=3
while [ ${NUM_PODS} -lt ${EXPECTED_PODS} ]
do
  sleep 1
  NUM_PODS=`"${KUBECTL}" --kubeconfig ${KUBECONFIG} \
    get pods \
    | grep 'nginx-test' \
    | grep Running \
    | wc -l`
  log "Waiting for pods, ${NUM_PODS} of ${EXPECTED_PODS} created."
done

log "Waiting for the LB to be ready"

set +e
set -o pipefail

log "Waiting 10s for the service to sync"
sleep 10
WAITED=10
# Wait for VIP to come up
log "Waiting for VIP to come up"
VIP_UP=1
while [[ ${VIP_UP} -ne 0 && ${WAITED} -lt 120 ]]
do
  ping -c 1 ${LOAD_BALANCER_IP} 2>&1 \
    | while read line; do log ${line}; done
  VIP_UP=$?
  sleep 1
  WAITED=$((WAITED+1))
done

if [ ${VIP_UP} -ne 0 ]; then
  log "Timeout waiting for VIP to up after ${WAITED} seconds."
  exit 1
fi
log "VIP is up."

LB_CHECK_RESULT=1
RESP1=`curl -s http://${LOAD_BALANCER_IP}  | head -4 | tail -1`
Check_Response "${RESP1}", "<title>Welcome to nginx!</title>," 
LB_CHECK_RESULT=$?
if [ ${LB_CHECK_RESULT} -ne 0 ]; then
       log "Http response is not the same as expected"
       exit 0
fi

if [ ${LB_CHECK_RESULT} -eq 0 ]; then
    log "Start Rolling upgrade test"
    RollingUpgrade 1.10.1  ${LOAD_BALANCER_IP}
    Result_RU=$?
    if [ ${Result_RU} -ne 0 ]; then
       log "Rolling update fails"
       sleep 1000
       exit 0
    fi
    sleep 10
    RollingUpgrade 1.7.9   ${LOAD_BALANCER_IP}
    Result_RU=$?
    
    if [ ${Result_RU} -ne 0 ]; then
       log "Rolling update fails"
       sleep 1000
       exit 0
    fi
    exit 0
fi
