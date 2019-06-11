#!/usr/bin/env python
from __future__ import with_statement
import logging
import sys
import time
import os
import datetime
import re
import argparse
import subprocess
import importlib


test_results = [] 
RED   = "\033[1;31m"
BLUE  = "\033[1;34m"
CYAN  = "\033[1;36m"
GREEN = "\033[0;32m"
RESET = "\033[0;0m"
BOLD    = "\033[;1m"
REVERSE = "\033[;7m"


nginx_yaml_string = '''
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nginx-sanity-test
  namespace: nginx-sanity-ns
  labels:
    app: nginx-sanity-test
spec:
  replicas: 3
  selector:
    matchLabels:
      app: nginx-sanity-test
  template:
    metadata:
      labels:
        app: nginx-sanity-test
    spec:
      containers:
      - name: nginx-sanity-test
        image: nginx:1.7.9
        ports:
        - containerPort: 80
---
apiVersion: v1
kind: Service
metadata:
  name: nginx-sanity-test
  namespace: nginx-sanity-ns
spec:
  type: LoadBalancer
  ports:
  - port: 80
    protocol: TCP
    targetPort: 80
  selector:
    app: nginx-sanity-test
'''

patch_node_string = '''
spec:
'''

workloadyaml = 'nginx.yaml'
patchnodeyaml = "patch.node.yaml"
http_target_string = 'Welcome to nginx!'


def create_yaml_file_from_string(yaml_string, yaml_file):
    try:
        with open(yaml_file, 'w') as writer:
            writer.write(yaml_string)
    except EnvironmentError: 
        print 'Oops: open file {} for write fails.'.format(yaml_file)
        exit()

def delete_yaml_files():
    exists = os.path.isfile(workloadyaml)
    if exists:
        os.remove(workloadyaml)
    exists = os.path.isfile(patchnodeyaml)
    if exists:
        os.remove(patchnodeyaml)
    

def send_log_to_stdout():
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    root.addHandler(handler)

def env_prepare():
    cmdline = "pip list"
    retOutput = subprocess.check_output(cmdline.split())
    #print cmdline, retOutput
    if not "six" in retOutput or True:
        package_install_cli ='sudo apt-get install python-pip -y'
        retOutput = subprocess.check_output(cmdline.split())
        #print package_install_cli, retOutput
        package_install_cli = 'pip install six'
        retOutput = subprocess.check_output(cmdline.split())
        #print package_install_cli, retOutput

    lib = 'iterpopen'
    globals()[lib] = importlib.import_module(lib)
    cmdline =  "sudo apt list --installed"
    (retcode, retOutput) = RunCmd(cmdline, 15, None, wait=2, counter=0)
    #print cmdline, retOutput
    if not "apache2-utils" in retOutput:
        package_install_cli ='sudo apt-get install apache2-utils -y'
        (retcode, retOutput) = RunCmd(package_install_cli, 15, None, wait=2, counter=0)
        #print package_install_cli, retOutput

def env_check():
    cmdline = "kubectl --help"
    (retcode, retOuput) = RunCmd(cmdline, 15, None, wait=2, counter=0)
    if retcode == 1:
        print "Fail to run kubectl. Kubectl is required to run the test script."
        return False 
    cmdline = "gkectl --help"
    (retcode, retOuput) = RunCmd(cmdline, 15, None, wait=2, counter=0)
    if retcode == 1:
        print "Fail to run gkectl. gkectl is required to run the test script."
        return False
    return True
   
class CommandFailError(Exception):
     pass

def RunCmd(cmd, timeout, output_file=None, wait=2, counter=3, **kwargs):
  """Run a command from console and wait/return command reply.

  Args:
    cmd: the command to execute
    timeout: command timeout value
    output_file: the abusolute path to the output file for command reply
    wait: time interval between command retries
    counter: number of retry times if command failed, by default, no retry
             needed
    **kwargs: other args to control command execution,
      "no_raise": if True, do not raise exception if command failed.

  Returns:
    tuple: return code and reply message
  """

  def RetryCmd(cmd, timeout, output_file=None):
    """Execute a command with timeout restriction."""
    outfile = output_file and open(output_file, 'a') or iterpopen.PIPE
    bash = iterpopen.Popen(cmd, stdout=outfile, stderr=outfile, timeout=timeout,
                           shell=True)
    output, err = bash.communicate()
    if bash.returncode != 0 and not kwargs.get('no_raise'):
        print "Fail to run cmd {}".format(cmd)
    return bash.returncode, output, err

  rc, out, err = RetryCmd(cmd, timeout, output_file)
  if rc == 0:
    return (0, err and out + '\n' + err or out)
  return (rc, err)

def gcp_auth(serviceacct):
    # gcloud auth activate-service-account --key-file=release-reader-key.json
    cmdline = 'gcloud auth activate-service-account --key-file={}'.format(serviceacct)
    print cmdline
    (retcode, retOuput) = RunCmd(cmdline, 15, None, wait=2, counter=0)
    print retOuput
    if retcode == 1:
        print "Failure to run cmd {}".format(cmdline)
    return retcode    

def upload_testlog(testlog, gcs_bucket):
    """Upload test log to gcs bucket.
    Args:
      testlog: the full path of testlog file
      gcs_bucket: the gcs bucket identifier
    
     Returns:
    tuple: return code and reply message
    """
    cmdline = 'gsutil cp {} {}/{}'.format(testlog, gcs_bucket, testlog)
    try:
        cmdoutput = subprocess.check_output(cmdline.split())
    except Exception as e:
        print "Upload file to gcs bucker fails."
        return False, errmsg
    return True, cmdoutput

def check_service_availbility(svc_endpoint, testreportlog):
    cmdline = 'curl -s http://{}/index.html'.format(svc_endpoint)
    (retcode, cmdOutput) = RunCmd(cmdline, 10, None, wait=2, counter=0)
    testreportlog.detail2file(cmdline)
    testreportlog.detail2file(cmdOutput)
    if http_target_string in cmdOutput:
        return True
    else:
        return False

def get_info_from_workload_yaml_file():
    namespace = None
    replicas = None
    deployment = None
    servicetype = None
    namespacepattern = re.compile(r"namespace:\s([a-zA-Z\d\-]+)")
    with open(workloadyaml) as f:
        output = f.read()
    namespacepattern = re.compile(r"namespace:\s([a-zA-Z\d\-]+)")
    matched =  namespacepattern.search(output)
    if matched:
        namespace = matched.group(1)
    deploymentpattern = re.compile(r"name:\s([a-zA-Z\d\-]+)")
    matched =  deploymentpattern.search(output)
    if matched:
        deployment = matched.group(1)
    replicaspattern = re.compile(r"replicas:\s(\d+)")
    matched =  replicaspattern.search(output)
    if matched:
        replicas = int(matched.group(1))
    servicetypepattern = re.compile(r"type:\s([a-zA-Z\d\-]+)")
    matched =  servicetypepattern.search(output)
    if matched:
        servicetype = matched.group(1)
    return namespace, replicas, deployment, servicetype

class testlog:

    def __init__(self, logdest, loglevel):
        self.logger = logging.getLogger(__name__)
        if loglevel == 3:
            self.logger.setLevel(logging.CRITICAL)
        elif loglevel == 4:
            self.logger.setLevel(logging.ERROR)
        elif loglevel == 5:
            self.logger.setLevel(logging.WARNING)
        elif loglevel == 6:
            self.logger.setLevel(logging.INFO)
        elif loglevel == 7:
            self.logger.setLevel(logging.DEBUG)
 
        self.handler = logging.FileHandler(logdest)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        self.handler.setFormatter(formatter)
        self.logger.addHandler(self.handler)

    def info2file(self, logmessage):
        if "PASS" in logmessage:
            sys.stdout.write(GREEN)
        elif "FAIL" in logmessage or "fail" in logmessage:
            sys.stdout.write(RED)
        else:
            sys.stdout.write(BLUE)
    
        self.logger.info(logmessage)
        sys.stdout.write(RESET)
       
    def detail2file(self, logmessage):
        if "PASS" in logmessage:
            sys.stdout.write(GREEN)
        elif "FAIL" in logmessage or "fail" in logmessage:
            sys.stdout.write(RED)
        else:
            sys.stdout.write(BLUE)
        
        self.logger.debug(logmessage)
        sys.stdout.write(RESET)


class gkeonpremcluster:
    def __init__(self, clustercfgfile, isAdminCluster, detaillogfile, platform):
        self.clustercfgfile = clustercfgfile
        self.isAdminCluster = isAdminCluster
        self.objectsList = []
        self.objectsDict = {}
        self.namespacesDict = {}
        self.detaillog = detaillogfile
        self.platform = platform
        self.get_cluster_server_ip()
        self.get_cluster_name()
        self.get_gke_version()
        if isAdminCluster:
            self.readyreplicas = 0
        else:    
            self.get_number_machine_deployments()
        self.get_namespace()
        for eachnamespace in self.namespacesDict.keys():
            self.get_all_for_namespace(eachnamespace)            
        self.detaillog.detail2file("Self server ip for cluster {}: {}".format(self.clustername, self.serverip))
        self.detaillog.detail2file("Number of machines in the cluster: ".format(self.readyreplicas))



    def get_cluster_name(self):
        cmdline = 'kubectl --kubeconfig {} get cluster'.format(self.clustercfgfile)
        self.detaillog.detail2file(cmdline)
        try:
            cmdoutput = subprocess.check_output(cmdline.split())
        except Exception as e:
            self.clustername = None 
        self.detaillog.detail2file(cmdoutput)
        pattern = re.compile(r"([a-zA-Z\d\-]+)\s+(\d+)(s|d|m|h)")
        if pattern.search(cmdoutput) != None:
            self.clustername = pattern.search(cmdoutput).group(1)
            self.detaillog.detail2file("Cluster name: {}".format(self.clustername))


    def get_gke_version(self):
        self.control_version = None
        cmdline = 'kubectl --kubeconfig {} describe cluster {}'.format(self.clustercfgfile, self.clustername)
                
        self.detaillog.detail2file(cmdline)
        try:
             cmdoutput = subprocess.check_output(cmdline.split())
        except Exception as e:
              self.control_version = None     
        self.detaillog.detail2file(cmdoutput)
        #Control Plane Version:    1.11.2-gke.31
        pattern = re.compile(r"Control\sPlane\sVersion:\s*([a-zA-Z\d\-\.]+)")
        matched = pattern.search(cmdoutput)
        if matched:
            self.control_version = matched.group(1)

    def get_number_machine_deployments(self):
        #  Available Replicas:    3
        #  Observed Generation:   49
        #  Ready Replicas:        3
        #  Replicas:              10
        #  Unavailable Replicas:  7
        #  Updated Replicas:      10

        availablereplicas = -1
        unavailablereplicas = -1
        readyreplicas = 0
        updatedreplicas = -2 
        # change this
        retry = 6
        count = 0
        interval = 20
        while count < retry and (not unavailablereplicas == 0 or not availablereplicas == updatedreplicas or not readyreplicas == updatedreplicas):
            print "polling ready machine, ".format(count), "continue? {}".format(count < retry)
            cmdline = 'kubectl --kubeconfig {} describe machinedeployments {} | grep Replicas'.format(self.clustercfgfile, self.clustername)
            self.detaillog.detail2file(cmdline)
            retcode, retoutput = RunCmd(cmdline, 15, None, wait=2, counter=0)
            self.detaillog.detail2file(retoutput)
            retoutput = ' '.join(re.split("\n+", retoutput))
            #print retoutput
            if retcode == 0:
                pattern = re.compile(r"Available\sReplicas:\s+(\d+)")
                matched = pattern.search(retoutput)
                if matched:
                    availablereplicas = matched.group(1)
                pattern = re.compile(r"Updated\sReplicas:\s+(\d+)")
                matched = pattern.search(retoutput)
                if matched:
                    updatedreplicas = matched.group(1)
                else:
                    updatedreplicas = availablereplicas
                pattern = re.compile(r"Unavailable\sReplicas:\s+(\d+)")
                matched = pattern.search(retoutput)
                if matched:
                    unavailablereplicas = matched.group(1)
                else:   
                    unavailablereplicas = 0  
                pattern = re.compile(r"Ready\sReplicas:\s+(\d+)")
                matched = pattern.search(retoutput)
                if matched:
                    readyreplicas = matched.group(1)
            if not availablereplicas == updatedreplicas or not readyreplicas == updatedreplicas or not unavailablereplicas == 0:
                time.sleep(interval)
            count += 1
        self.readyreplicas = int(readyreplicas)
        self.detaillog.detail2file("Cluster ready Replicas: {}".format(self.readyreplicas))

    def check_cluster_server_connectivity(self):
        cmdline = 'ping {} -c 5'.format(self.serverip)
        self.detaillog.detail2file(cmdline)
        try:
            cmdoutput = subprocess.check_output(cmdline.split())
        except Exception as e:
            print "Run cmd {} fails.".format(cmdline)
    
        self.detaillog.detail2file(cmdoutput)
        pattern = re.compile(r"5 packets transmitted, 5 received, 0% packet loss")
        if pattern.search(cmdoutput) != None:
            return True
        else:
            return False

    def get_cluster_server_ip(self):
       #server: https://100.115.253.83:443
        cluser_server_ip = '127.0.0.1'
        with open(self.clustercfgfile) as cfgfile:
            cfgoutput = cfgfile.read()
            #print cfgoutput
            pattern = re.compile(r"server:\shttps:\/\/(\d+.\d+.\d+.\d+):443")
            if pattern.search(cfgoutput) != None:
                #print "found server ip"
                cluser_server_ip = pattern.search(cfgoutput).group(1)
        self.serverip = cluser_server_ip

    def description(self):
        if self.isAdminCluster:
            self.detaillog.detail2file("Cluster defined in {} is admin cluster built on {}".format(self.clustercfgfile, self.platform))  
            return "Admin Cluster defined in {} is admin cluster built on {}".format(self.clustercfgfile, self.platform)
        else:
            self.detaillog.detail2file("Cluster defined in {} is user cluster built on {}".format(self.clustercfgfile, self.platform))
            return "Admin Cluster defined in {} is admin cluster built on {}".format(self.clustercfgfile, self.platform)
         

    def dump_cluster_all(self):
        cmdline = 'kubectl --kubeconfig {} get all --all-namespaces'.format(self.clustercfgfile)
        self.detaillog.detail2file(cmdline)
        try:
            cmdoutput = subprocess.check_output(cmdline.split())
        except Exception as e:
            print "Run cmd {} fails.".format(cmdline)
    
        self.detaillog.detail2file(cmdoutput)
        return cmdoutput


    def workload_deployment(self):
        namespace, _, _, _ = get_info_from_workload_yaml_file()

        if not namespace in self.objectsList:
            cmdline = 'kubectl --kubeconfig {} create namespace {}'.format(self.clustercfgfile, namespace)
            self.detaillog.detail2file(cmdline)
            try:
                cmdoutput = subprocess.check_output(cmdline.split())
            except Exception as e:
                print "Run cmd {} fails.".format(cmdline)
    
            self.detaillog.detail2file(cmdoutput)

        self.detaillog.detail2file("Generating test service deployment")
        cmdline = 'kubectl --kubeconfig {} apply -f {}'.format(self.clustercfgfile, workloadyaml)
        self.detaillog.detail2file(cmdline)
        try:
            cmdoutput = subprocess.check_output(cmdline.split())
        except Exception as e:
            print "Run cmd {} fails.".format(cmdline)
            self.detaillog.detail2file(cmdoutput)
            return 1

        self.detaillog.detail2file(cmdoutput)

        return cmdoutput

    def workload_withdraw(self):
        self.detaillog.detail2file("Removing test service deployment")
        cmdline = 'kubectl --kubeconfig {} delete -f {}'.format(self.clustercfgfile, workloadyaml)
        self.detaillog.detail2file(cmdline)
        try:
            cmdoutput = subprocess.check_output(cmdline.split())
        except Exception as e:
            print "Run cmd {} fails.".format(cmdline)
            self.detaillog.detail2file(cmdoutput)
            return 1

        self.detaillog.detail2file(cmdoutput)
        return cmdoutput

    def delete_namespace(self, namespace):
        cmdline = 'kubectl --kubeconfig {} delete namespace {}'.format(self.clustercfgfile, namespace)
        self.detaillog.detail2file(cmdline)
        try:
            cmdoutput = subprocess.check_output(cmdline.split())
        except Exception as e:
            print "Run cmd {} fails.".format(cmdline)
            self.detaillog.detail2file(cmdoutput)
            return 1

        self.detaillog.detail2file(cmdoutput)
        self.objectsList.remove(namespace)
        return cmdoutput

    def workload_replica_modify(self, deployment_name, workloadns, new_replica):
        #kubectl --kubeconfig kubecfg/userclustercfg scale --replicas=4 deployment/nginx-sanity-test

        cmdline = 'kubectl --kubeconfig {} scale --replicas={} deployment/{} -n {}'.format(self.clustercfgfile, new_replica, deployment_name, workloadns)
        self.detaillog.detail2file(cmdline)
        try: 
            cmdoutput = subprocess.check_output(cmdline.split())
        except Exception as e:
            print "Run cmd {} fails.".format(cmdline)
            self.detaillog.detail2file(cmdoutput)
            return 1
    
        self.detaillog.detail2file(cmdoutput)

        return cmdoutput


    def change_number_of_machine_deployment(self, numberofmachines, patchnodeyaml):
        yaml_string = "{}  replicas: {}".format(patch_node_string, numberofmachines)
        create_yaml_file_from_string(yaml_string, patchnodeyaml)

        #cmdline = 'kubectl --kubeconfig {} patch machinedeployment {} -p "{\"spec\": {\"replicas\": {}}}" --type=merge'.format(self.clustercfgfile, self.clustername, numberofmachines)
        #print cmdline
        #kubectl --kubeconfig {} patch machinedeployment cpe-user-1-1 -p "{\"spec\": {\"replicas\": 3}}" --type=merge
        #machinedeployment.cluster.k8s.io/cpe-user-1-1 patched
        cmdline = 'kubectl --kubeconfig {} patch machinedeployment {} --patch "$(cat patch.node.yaml)"  --type=merge'.format(self.clustercfgfile, self.clustername)
        (retcode, retOutput) = RunCmd(cmdline, 15, None, wait=2, counter=0)
        self.detaillog.detail2file(retOutput)
        return retcode


    def gkectl_diagnose_cluster(self):
        # change this
        retry = 3
        interval = 10 
        resultpending = True
        cmdoutput = ""
        count = 1
        check_output = "Cluster is healthy"
        cmdline = 'gkectl diagnose cluster --kubeconfig {}'.format(self.clustercfgfile)
        self.detaillog.detail2file(cmdline)
        while count < retry and resultpending:
            (retcode, retOutput) = RunCmd(cmdline, 15, None, wait=2, counter=0)
            self.detaillog.detail2file(retOutput)
            if retcode == 1: 
                count += 1
                time.sleep(interval)
            else:
                resultpending = False
        return check_output in retOutput and retcode == 0


    def get_namespace(self):
        #kubectl --kubeconfig /home/ubuntu/anthos_ready/kubecfg/userclustercfg get namespace
        #NAME                       STATUS    AGE
        #config-management-system   Active    10d
        #default                    Active    10d
        #gke-connect                Active    10d
        #gke-system                 Active    10d
        #kube-public                Active    10d
        #kube-system                Active    10d
        #nginx-sanity-ns            Active    1d

        cmdline = 'kubectl --kubeconfig {} get namespace'.format(self.clustercfgfile)
        self.detaillog.detail2file(cmdline)
        try:
            cmdoutput = subprocess.check_output(cmdline.split()).splitlines()[1:] 
        except Exception as e:
             print "Run cmd {} fails.".format(cmdline)

        for eachline in cmdoutput:
            self.detaillog.detail2file(eachline)

        for eachline in cmdoutput:
            #print eachline
            #print eachline.split()
            self.namespacesDict[eachline.split()[0]] = eachline.split()[1:]
        

    def get_all_for_namespace(self, namespace):
        #ubuntu@admin-beta-4:~/anthos_ready$ kubectl  --kubeconfig kubecfg/cpe-user-1-1-kubeconfig get all -n nginx-sanity-ns
        #NAME                                     READY     STATUS    RESTARTS   AGE
        #pod/nginx-sanity-test-67b5687c6d-8vn2p   1/1       Running   0          1m
        #pod/nginx-sanity-test-67b5687c6d-fzmmq   1/1       Running   0          1m
        #pod/nginx-sanity-test-67b5687c6d-jlwz9   1/1       Running   0          1m

        #NAME                        TYPE           CLUSTER-IP    EXTERNAL-IP       PORT(S)        AGE
        #service/nginx-sanity-test   LoadBalancer   10.98.29.27   100.115.253.112   80:31816/TCP   1m

        #NAME                                DESIRED   CURRENT   UP-TO-DATE   AVAILABLE   AGE
        #deployment.apps/nginx-sanity-test   3         3         3            3           1m

        #NAME                                           DESIRED   CURRENT   READY     AGE
        #replicaset.apps/nginx-sanity-test-67b5687c6d   3         3         3         1m
        retry = 10 
        count = 0
        internal = 10
        stable_state = False
        
        cmdline = 'kubectl --kubeconfig {} get all -n {}'.format(self.clustercfgfile, namespace)
        self.detaillog.detail2file(cmdline)
        while count < retry and not stable_state: 
            try:
                cmdoutput = subprocess.check_output(cmdline.split()).splitlines()[1:]
            except Exception as e:
                print "Run cmd {} fails.".format(cmdline)
    
            for eachline in cmdoutput:
                self.detaillog.detail2file(eachline)
            if not "pending" in cmdoutput and not "ContainerCreating" in cmdoutput and not "Terminating" in cmdoutput:
                stable_state = True
            else:
                time.sleep(internal)
            count += 1    
        #print cmdoutput

        pods = {}
        services = {}
        deployment = {}
        replicaset = {}
        daemonset = {}
        statefulset = {}
        podpattern = re.compile(r"^pod\/([a-zA-Z0-9\-]*)\s*([0-9]*)\/([0-9]*)\s*([a-zA-Z]*)\s*([0-9]*)\s*([0-9]*)")
        servicepattern = re.compile(r"^service\/([a-zA-Z0-9\-]*)\s*([a-zA-Z]*)\s*([0-9\.]*)\s*([0-9\.]*)\s*([0-9:\/[A-Z]*)\s*([0-9]*)")
        deploymentpattern = re.compile(r"^deployment.apps\/([a-zA-Z0-9\-]*)\s*([0-9]*)\s*([0-9]*)\s*([0-9]*)\s*([0-9]*)\s*([0-9]*)\s*")
        replicasetpattern = re.compile(r"^replicaset.apps\/([a-zA-Z0-9\-]*)\s*([0-9]*)\s*([0-9]*)\s*([0-9]*)\s*([0-9]*)\s*")
        daemonsetpattern = re.compile(r"^daemonset.apps\/([a-zA-Z0-9\-]*)\s*([0-9]*)\s*([0-9]*)\s*([0-9]*)\s*([0-9]*)\s*([0-9]*)\s*")
        statefulsetpattern = re.compile(r"statefulset.apps\/([a-zA-Z0-9\-]*)\s*([0-9]*)\s*([0-9aa-z]*)\s*")
       
        found = False
        for eachline in cmdoutput:
            matchresult = podpattern.search(eachline)
            if matchresult:
                pods[matchresult.group(1)] = [matchresult.group(2), matchresult.group(3), matchresult.group(4), matchresult.group(5)]
                found = True
                continue     
            matchresult = deploymentpattern.search(eachline)
            if matchresult:
                deployment[matchresult.group(1)] = [int(matchresult.group(2)), int(matchresult.group(3)), matchresult.group(4), matchresult.group(5)]
                found = True
                continue
            matchresult = servicepattern.search(eachline)
            if matchresult:
                services[matchresult.group(1)] = [matchresult.group(2), matchresult.group(3), matchresult.group(4), matchresult.group(5)]
                found = True
                continue
            matchresult = replicasetpattern.search(eachline)
            if matchresult:
                replicaset[matchresult.group(1)] = [int(matchresult.group(2)), int(matchresult.group(3)), int(matchresult.group(4)), matchresult.group(5)]
                found = True
                continue
            matchresult = daemonsetpattern.search(eachline)
            if matchresult:
                #daemonset.apps/calico-node     3         3         3         3            3           <none>          10d
                daemonset[matchresult.group(1)] = [int(matchresult.group(2)), int(matchresult.group(3)), int(matchresult.group(4)), matchresult.group(5)]
                found = True
                continue
            matchresult = statefulsetpattern.search(eachline)
            if matchresult:
                statefulset[matchresult.group(1)] = [int(matchresult.group(2)), int(matchresult.group(3))]
                found = True
                continue
        if found:
            self.objectsDict[namespace] = [pods, services, deployment, replicaset, daemonset, statefulset]

        self.objectsList.append(namespace)


def test_abort(testreportlog):
    testreportlog.info2file("Test is aborted.")
    generate_test_summary(testreportlog)
    exit() 


def test_workload_deleted(cluster, namespace, abortonfailure):
    test_detail = "Workload defined by {} is deleted for cluster {} in namespace {}.".format(workloadyaml, cluster.clustercfgfile, namespace)
    test_name = 'test_workload_deleted'
    cmdline = 'kubectl --kubeconfig={} get -f {} -n {}'.format(cluster.clustercfgfile, workloadyaml, namespace)
    #print cmdline
    retcode, retoutput = RunCmd(cmdline, 15, None, wait=2, counter=0)
    if retcode==1:
         test_result = "PASS"
    else:
        test_result="FAIL"
    testreportlog.info2file("Test_Case: {}: {}: {}".format(test_name, test_result, test_detail))
    test_results.append([test_name, test_result, test_detail])
    #print retcode,retoutput
    if test_result == "FAIL" and abortonfailure:
        test_abort(testreportlog)

    return retcode==1


def test_cluster_sanity(cluster, testreportlog, abortonfailure):
    test_detail = "Cluster Sanity Check for cluster defined by {}.".format(cluster.clustercfgfile)
    test_name = "test_cluster_sanity"
    retCode = cluster.gkectl_diagnose_cluster()
    if retCode:
        test_result = "PASS"
    else:
        test_result="FAIL"

    testreportlog.info2file("Test_Case: {}: {}: {}".format(test_name, test_result, test_detail))
    test_results.append([test_name, test_result, test_detail])
    if test_result == "FAIL" and abortonfailure:
        test_abort(testreportlog)
    return retCode 

def test_machinedeployment_update(usercluster, testreportlog, abortonfailure):
    # change this
    retry = 3
    count = 0
    delta = 1
    interval = 20
    test_result="FAIL"
    test_name = "test_machinedeployment_update"
    usercluster.get_number_machine_deployments()
    tempnode = max(3, usercluster.readyreplicas)
    for newmachine in [tempnode+delta, tempnode]: 
        test_detail = "Modify number of machine deployment for cluster defined by {} to {}.".format(usercluster.clustercfgfile, newmachine)
        test_result = "FAIL"
        count = 0
        usercluster.change_number_of_machine_deployment(newmachine, patchnodeyaml)
        while count < retry and test_result == "FAIL": 
            usercluster.get_number_machine_deployments()
            if usercluster.readyreplicas == newmachine:
                test_result = "PASS"
            else:
                test_result="FAIL"
                time.sleep(interval)
            count += 1    
        
        testreportlog.info2file("Test_Case: {}: {}: {}".format(test_name, test_result, test_detail))
        test_results.append([test_name, test_result, test_detail])

        if test_result == "FAIL" and abortonfailure:
            test_abort(testreportlog)
 
    return test_result == "PASS"    


def test_workload_deployment(usercluster, lbsvcip, testreportlog, abortonfailure):
    test_detail = "Apply yaml file {} in cluster {}.".format(workloadyaml, usercluster.clustername)
    test_name = "test_workload_deployment"
    yaml_string = "{}  loadBalancerIP: {}".format(nginx_yaml_string, lbsvcip)
    create_yaml_file_from_string(yaml_string, workloadyaml)
    retOutput = usercluster.workload_deployment()
    testreportlog.detail2file(retOutput)
    if "created" in retOutput:
        test_result = "PASS"
    else:
        test_result = "FAIL"
    testreportlog.info2file("Test_Case: {}: {}: {}".format(test_name, test_result, test_detail))
    test_results.append([test_name, test_result, test_detail])
    if test_result == "FAIL" and abortonfailure:
        test_abort(testreportlog)

    return test_result == "PASS"

def test_workload_withdraw(usercluster, testreportlog, abortonfailure):
    test_detail = "Delete workflow defined by yaml file {} in cluster {}.".format(workloadyaml, usercluster.clustername)
    test_name = "test_workload_withdraw"

    retOutput = usercluster.workload_withdraw()
    if "deleted" in retOutput:
         test_result="PASS"
    else:
        test_result="FAIL"
    testreportlog.info2file("Test_Case: {}: {}: {}".format(test_name, test_result, test_detail))
    test_results.append([test_name, test_result, test_detail])
    if test_result == "FAIL" and abortonfailure:
        test_abort(testreportlog)

    return  test_result == "PASS"


def test_workload_deployed(usercluster, workloadns, testreportlog, abortonfailure):
    test_detail = "Verify workflow specified by {} is deployed in cluster {}.".format(workloadns, usercluster.clustername)
    test_name = "test_workload_deployed"
    if workloadns in usercluster.objectsList:
        test_result="PASS"
    else:
        test_result="FAIL"
    testreportlog.info2file("Test_Case: {}: {}: {}".format(test_name, test_result, test_detail))
    test_results.append([test_name, test_result, test_detail])
    if test_result == "FAIL" and abortonfailure:
        test_abort(testreportlog)

    return  test_result == "PASS"


def test_workload_number_of_pods(usercluster, workloadns, expected_number, testreportlog, abortonfailure):
    test_detail = "Verify number of pods for workload specified by {} deployed in cluster {} equals to the expected number {}.".format(workloadns, usercluster.clustername, expected_number)
    test_name = "test_workload_number_of_pods"
    if len(usercluster.objectsDict[workloadns][0].keys()) == expected_number:
            test_result="PASS"
    else:
        test_result="FAIL"
    testreportlog.info2file("Test_Case: {}: {}: {}".format(test_name, test_result, test_detail))
    test_results.append([test_name, test_result, test_detail])
    if test_result == "FAIL" and abortonfailure:
        test_abort(testreportlog)

    return  test_result == "PASS"


def test_workload_pod_state(usercluster, workloadns, expected_state, testreportlog, abortonfailure):
    test_detail = "Verify all pods for workload specified by {} deployed in cluster {} are {}.".format(workloadns, usercluster.clustername, expected_state)
    test_name = "test_workload_pod_state"
    pod_detail = usercluster.objectsDict[workloadns][0].values()
    test_result="PASS"
    for each in pod_detail:
        if not each[2] == expected_state:
            test_result="FAIL"
            continue
    testreportlog.info2file("Test_Case: {}: {}: {}".format(test_name, test_result, test_detail))        
    test_results.append([test_name, test_result, test_detail])
    if test_result == "FAIL" and abortonfailure:
        test_abort(testreportlog)

    return  test_result == "PASS"
        

def test_workload_service_state(usercluster,  workloadns, service_type, expected_lb_ip, testreportlog, abortonfailure):
    test_detail = "Verify service for workload specified by {} deployed in cluster {} has {} at {}.".format(workloadns, usercluster.clustername, service_type, expected_lb_ip)
    test_name = "test_workload_service_state"
    #print usercluster.objectsDict[workloadns]
    
    if usercluster.objectsDict[workloadns][1].values()[0][0] == service_type and usercluster.objectsDict[workloadns][1].values()[0][2] == expected_lb_ip:
        test_result="PASS"
    else:
        test_result="FAIL"
    testreportlog.info2file("Test_Case: {}: {}: {}".format(test_name, test_result, test_detail))
    test_results.append([test_name, test_result, test_detail])
    if test_result == "FAIL" and abortonfailure:
        test_abort(testreportlog)

    return  test_result == "PASS"

def test_workload_deployment_state(usercluster, workloadns, expected_number, testreportlog, abortonfailure):
    test_detail = "Verify deployment for workload specified by {} deployed in cluster {} equals to {}.".format(workloadns, usercluster.clustername, expected_number)
    test_name = "test_workload_service_state"
    if usercluster.objectsDict[workloadns][2].values()[0][0] == expected_number and usercluster.objectsDict[workloadns][2].values()[0][1] == expected_number:
        test_result="PASS"
    else:
        test_result="FAIL"
    testreportlog.info2file("Test_Case: {}: {}: {}".format(test_name, test_result, test_detail))
    test_results.append([test_name, test_result, test_detail])
    if test_result == "FAIL" and abortonfailure:
        test_abort(testreportlog)

    return  test_result == "PASS"


def test_workload_replica_state(usercluster, workloadns, expected_number, testreportlog, abortonfailure):
    test_detail = "Verify replicas for workload specified by {} deployed in cluster {} equals to {}.".format(workloadns, usercluster.clustername, expected_number)
    test_name = "test_workload_replica_state"
    if usercluster.objectsDict[workloadns][3].values()[0][0] == expected_number and usercluster.objectsDict[workloadns][3].values()[0][1] == expected_number:
        test_result="PASS"
    else:
        test_result="FAIL"
    testreportlog.info2file("Test_Case: {}: {}: {}".format(test_name, test_result, test_detail))
    test_results.append([test_name, test_result, test_detail])
    if test_result == "FAIL" and abortonfailure:
        test_abort(testreportlog)

    return  test_result == "PASS"


def test_workload_accessible_via_lbsvcip(usercluster, workloadns, lbsvcip, testreportlog, abortonfailure):
    test_detail = "Verify service provided by workload is accessible via LBIP {} in cluster {}.".format(lbsvcip, usercluster.clustername)
    test_name = "test_workload_accessible_via_lbsvcip"

    if check_service_availbility(lbsvcip, testreportlog):
        test_result="PASS"
    else:
        test_result="FAIL"
    testreportlog.info2file("Test_Case: {}: {}: {}".format(test_name, test_result, test_detail))
    test_results.append([test_name, test_result, test_detail])
    if test_result == "FAIL" and abortonfailure:
        test_abort(testreportlog)

    return  test_result == "PASS"


def test_service_traffic(concurrent_session, total_request, lbsvcip, expected_duration, testreportlog, abortonfailure):
    retOutput = None
    test_result = "FAIL"
    test_detail = "Verify service traffic at {} in user cluster {}.".format(lbsvcip, usercluster.clustername)
    test_name = "test_service_traffic"
    cmdline = "ab -c {} -n {}  http://{}/index.html".format(concurrent_session, total_request, lbsvcip)
    testreportlog.info2file(cmdline)
    (retcode, retOutput) = RunCmd(cmdline, 10*expected_duration, None, wait=2, counter=0)
    testreportlog.info2file(retOutput)
    if retcode == 1:
        print "Fail to run cmd {}".format(cmdline)
        test_result = "FAIL"
    else:
        print "this is output:\n",retOutput 
        match_string = "Finished {} requests".format(total_request)
        if match_string in retOutput:
            timepattern = re.compile(r"Time\staken\sfor\stests:\s+(\d+\.\d*)\sseconds")
            if timepattern.search(retOutput) != None:
                actual_time_used = float(timepattern.search(retOutput).group(1))
                print "actual_time_used:{}".format(actual_time_used)
                testreportlog.info2file("Actual time used: {} for {} request with {} concurrent sessions".format(actual_time_used, total_request, concurrent_session))
                testreportlog.info2file("Ideal time used should be less than 0.5s for {} request with {} concurrent sessions.".format(total_request, concurrent_session))
                if actual_time_used < expected_duration:
                    test_result = "PASS"
                else:
                    test_result = "FAIL"
            else:
                print "fail to find time taken"
                test_result = "FAIL"
        else:
            test_result = "FAIL"
            print "failure to find completed request"
    testreportlog.info2file("Test_Case: {}: {}: {}".format(test_name, test_result, test_detail))
    test_results.append([test_name, test_result, test_detail])
    if test_result == "FAIL" and abortonfailure:
        test_abort(testreportlog)
    return test_result == "PASS"


def cluster_cleanup(usercluster):
    namespace, _, _, _ = get_info_from_workload_yaml_file()
    if namespace in usercluster.objectsList:
        usercluster.delete_namespace(namespace)
        usercluster.objectsList.remove(namespace)


def admin_cluster_test(admincluster, testreportlog, abortonfailure):
    if not admincluster.check_cluster_server_connectivity():
        testreportlog.info2file('Admin Cluster Server IP {} is not reachable! Test Aborted'.format(admincluster.serverip))
        exit
    else:
        testreportlog.info2file(admincluster.description())
        testreportlog.info2file(admincluster.dump_cluster_all())
    test_cluster_sanity(admincluster, testreportlog, abortonfailure)
 

def test_workflow_state(usercluster, testreportlog, workloadns, expected_state, expected_number, service_type, lbsvcip, abortonfailure):
    concurrent_session = 100
    total_request = 10000
    expected_duration = 5

    #test_cluster_sanity(usercluster, testreportlog, abortonfailure)
    usercluster.get_all_for_namespace(workloadns)
    if test_workload_deployed(usercluster, workloadns, testreportlog, abortonfailure):
        test_workload_pod_state(usercluster, workloadns, expected_state, testreportlog, abortonfailure)
        test_workload_number_of_pods(usercluster, workloadns, expected_number, testreportlog, abortonfailure)
        if test_workload_service_state(usercluster, workloadns, service_type, lbsvcip, testreportlog, abortonfailure):
            if test_workload_accessible_via_lbsvcip(usercluster, workloadns, lbsvcip, testreportlog, abortonfailure):
                test_service_traffic(concurrent_session, total_request, lbsvcip, expected_duration, testreportlog, abortonfailure)
        test_workload_deployment_state(usercluster, workloadns, expected_number, testreportlog, abortonfailure)
        test_workload_replica_state(usercluster,  workloadns, expected_number, testreportlog, abortonfailure)


def user_cluster_test(usercluster, lbsvcip, testreportlog, abortonfailure):    
    
    expected_state = "Running"

    if not usercluster.check_cluster_server_connectivity():
        testreportlog.info2file('User Cluster Server IP {} is not reachable! Test Aborted'.format(usercluster.serverip))
        exit()
    else:
        testreportlog.info2file(usercluster.description())
        testreportlog.info2file(usercluster.dump_cluster_all())

    test_cluster_sanity(usercluster, testreportlog, abortonfailure)
    test_machinedeployment_update(usercluster, testreportlog, abortonfailure) 

    
    test_workload_deployment(usercluster, lbsvcip, testreportlog, abortonfailure)
    workloadns, expected_number, deployment_name, service_type = get_info_from_workload_yaml_file()
    time.sleep(30)
    test_workflow_state(usercluster, testreportlog, workloadns, expected_state, expected_number, service_type, lbsvcip, abortonfailure)

    new_replica = expected_number*2
    usercluster.workload_replica_modify(deployment_name, workloadns, new_replica)
    time.sleep(30)
    test_workflow_state(usercluster, testreportlog, workloadns, expected_state, new_replica, service_type, lbsvcip, abortonfailure)

    test_workload_withdraw(usercluster, testreportlog, abortonfailure)
    time.sleep(10)
    usercluster.get_all_for_namespace(workloadns)

    test_workload_deleted(usercluster, workloadns, abortonfailure)
    time.sleep(10)
 
    #test_cluster_sanity(usercluster,  testreportlog, abortonfailure)


def generate_test_summary(testreportlog):
    passed_tests = 0
    failed_tests = 0
    for each in test_results:
        if each[1] == "PASS":
            passed_tests += 1
        else:
            failed_tests += 1

    testreportlog.info2file('\n\n')
    testreportlog.info2file("Summary:")
    testreportlog.info2file("    partner: {}, platform: {}, version: {}".format(args.partner, args.platform, args.version))
    testreportlog.info2file("    admin cluster version: {}, user cluster version: {}".format(admincluster.control_version, usercluster.control_version))
    testreportlog.info2file("    Total Tests: {}, Passed Tests: {}, Failed Tests: {}".format(len(test_results), passed_tests, failed_tests))
    testreportlog.info2file("======================================================================================================================================")
    for eachcase in test_results:
        testreportlog.info2file("      {}".format(':'.join(eachcase)))
    testreportlog.info2file("======================================================================================================================================")

env_prepare()

# pre-check
if not env_check():
    print "WARNING: Please check your env. gkectl and kubectl are needed to run the test script."
    exit

# Test 
parser = argparse.ArgumentParser()
parser.add_argument('-clustercfgpath', '--clustercfgpath', dest='clustercfgpath', type=str, default=None, required=True)
parser.add_argument('-adminclustercfg', '--adminclustercfg', dest='admcfg', type=str, default=None, required=True)
parser.add_argument('-userclustercfg', '--userclustercfg', dest='usercfg', type=str, default=None, required=True)
parser.add_argument('-lbsvcip', '--lbsvcip', dest='lbsvcip', type=str, default=None, required=True)
parser.add_argument('-anthostestlog', '--anthostestlog', dest='anthostestlog', type=str, default='gkeonprem.test')
parser.add_argument('-gcsbucket', '--gcsbucket', dest='gcsbucket', type=str, default=None)
parser.add_argument('-serviceacct', '--serviceacct', dest='serviceacct', type=str, default=None)
parser.add_argument('-testloop', '--testloop', dest='testloop', type=int, default=1)
parser.add_argument('-abortonfailure', '--abortonfailure', dest='abortonfailure', type=bool, default=False)
parser.add_argument('-partner', '--partner', dest='partner', type=str, default='gcp')
parser.add_argument('-platform', '--platform', dest='platform', type=str, default='unknown')
parser.add_argument('-version', '--version', dest='version', type=str, default='unknown')
parser.add_argument('-upgrade', '--upgrade', dest='upgrade', type=bool, default=False)


args = parser.parse_args()

currentDT = datetime.datetime.now()
timestamp = time.strftime("%Y-%m-%d-%H-%M")
anthosreportlog = '{}.{}.{}.{}.T{}.log'.format(args.anthostestlog, args.partner, args.platform, args.version, timestamp)
testreportlog = testlog(anthosreportlog, 7)
testreportlog.info2file("Anthos-Ready Platform Test for platform {}, Partner {} starting at {}.\n\n".format(args.platform, args.partner, timestamp))
# Set log to stdout 
send_log_to_stdout()


# Admin Cluster Sanity Check
adminclustercfgfile = '{}/{}'.format(args.clustercfgpath, args.admcfg)
admincluster = gkeonpremcluster(adminclustercfgfile, True, testreportlog, args.partner)
admin_cluster_test(admincluster, testreportlog, False)

lbsvcip = args.lbsvcip

# User Cluster Sanity Check
userclustercfgs = args.usercfg.split(',')
for userclustercfg in userclustercfgs:
    loop = 0
    userclustercfgfile = '{}/{}'.format(args.clustercfgpath, userclustercfg)
    usercluster = gkeonpremcluster(userclustercfgfile, False, testreportlog, args.partner)
    while loop < args.testloop: 
        user_cluster_test(usercluster, lbsvcip, testreportlog, args.abortonfailure)
        loop += 1
        time.sleep(60)
    cluster_cleanup(usercluster)
    delete_yaml_files()
    time.sleep(60)


print "Before script ends run sanity check on both admin and user cluster"
#Final check for admin Cluster
test_cluster_sanity(admincluster, testreportlog, args.abortonfailure)
#Final check for user Cluster
test_cluster_sanity(usercluster, testreportlog, args.abortonfailure)

generate_test_summary(testreportlog)

if args.gcsbucket:
    if args.serviceacct:
        if gcp_auth(args.serviceacct) == 1:
            exit()
    upload_testlog(anthosreportlog, args.gcsbucket)
