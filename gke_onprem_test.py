#!/usr/bin/env python
import argparse
import datetime
import errno
import importlib
import random
import logging
import os
import pty
import re
import select
import signal
import six
import subprocess
import sys
import threading
import time
import tty


VERSION = "1.0.2"

test_results = []
test_cfg = {}

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

http_target_string = 'Welcome to nginx!'


# define convenient aliases for subprocess constants
# Note subprocess.PIPE == -1, subprocess.STDOUT = -2
PIPE = subprocess.PIPE
STDOUT = subprocess.STDOUT
PTY = -3


class Error(Exception):
  """Exception when Popen suprocesses fail."""


class TimeoutError(Error):
  """Exception when Popen suprocesses time out."""


PopenTimeoutError = TimeoutError


class PollError(Error):
  """Exception when Popen suprocesses have poll errors."""


class ReturncodeError(Error):
  """Exception raised for non-zero returncodes.

  Attributes:
    returncode: the returncode of the failed process.
    cmd: the Popen args argument of the command executed.
  """

  def __init__(self, returncode, cmd):
    Error.__init__(self, returncode, cmd)
    self.returncode = returncode
    self.cmd = cmd

  def __str__(self):
    return "Command '%s' returned non-zero returncode %d" % (
        self.cmd, self.returncode)


def setraw(*args, **kwargs):
  """Wrapper for tty.setraw that retries on EINTR."""
  while True:
    try:
      return tty.setraw(*args, **kwargs)
    except OSError as e:
      if e.errno == errno.EINTR:
        continue
      else:
        raise


def call(*args, **kwargs):
  """Run a command, wait for it to complete, and return the returncode.

  Example:
    retcode = call(["ls", "-l"])

  Args:
    See the Popen constructor.

  Returns:
    The int returncode.
  """
  # Make the default stdout None.
  kwargs.setdefault('stdout', None)
  return Popen(*args, **kwargs).wait()


class Popen(subprocess.Popen):
  """An extended Popen class that is iterable.

  Args:
    args: str or argv arguments of the command
      (sets shell default to True if it is a str)
    bufsize: buffer size to use for IO and iterating
      (default: 1 means linebuffered, 0 means unbuffered)
    input: stdin input data for the command
      (default: None, sets stdin default to PIPE if it is a str)
    timeout: timeout in seconds for command IO processing
      (default:None means no no timeout)
    **kwargs: other subprocess.Popen arguments
  """

  def __init__(self, args, bufsize=1, input=None, timeout=None, **kwargs):
    # make arguments consistent and set defaults
    if isinstance(args, (six.text_type, six.binary_type)):
      kwargs.setdefault('shell', True)
    if isinstance(input, six.text_type):
      input = input.encode('utf-8')
    if isinstance(input, six.binary_type):
      kwargs.setdefault('stdin', PIPE)
    kwargs.setdefault('stdout', PIPE)
    self.__race_lock = threading.RLock()
    super(Popen, self).__init__(args, bufsize=bufsize, **kwargs)
    self.bufsize = bufsize
    self.input = input
    self.timeout = timeout
    # Initialise stdout and stderr buffers as attributes such that their content
    # does not get lost if an iterator is abandoned.
    self.outbuff, self.errbuff = b'', b''

  def _get_handles(self, stdin, stdout, stderr):
    """Construct and return tuple with IO objects.

    This overrides and extends the inherited method to also support PTY as a
    special argument to use pty's for stdin/stdout/stderr.

    Args:
      stdin: the stdin initialisation argument
      stdout: the stdout initialisation argument
      stderr: the stderr initialisation argument

    Returns:
      For recent upstream python2.7+ versions;
      (p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite), to_close
      For older python versions it returns;
      (p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite)
    """
    # For upstream recent python2.7+ this returns a tuple (handles, to_close)
    # where handles is a tuple of file handles to use, and to_close is the set
    # of file handles to close after the command completes. For older versions
    # it just returns the file handles.
    orig = super(Popen, self)._get_handles(stdin, stdout, stderr)  # type: ignore
    if len(orig) == 2:
      handles, to_close = orig
    else:
      handles, to_close = orig, set()
    p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite = handles
    if stdin == PTY:
      p2cread, p2cwrite = pty.openpty()
      setraw(p2cwrite)
      to_close.update((p2cread, p2cwrite))
    if stdout == PTY:
      c2pread, c2pwrite = pty.openpty()
      setraw(c2pwrite)
      to_close.update((c2pread, c2pwrite))
      # if stderr==STDOUT, we need to set errwrite to the new stdout
      if stderr == STDOUT:
        errwrite = c2pwrite
    if stderr == PTY:
      errread, errwrite = pty.openpty()
      setraw(errwrite)
      to_close.update((errread, errwrite))
    handles = p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite
    if len(orig) == 2:
      return handles, to_close
    else:
      return handles

  def __iter__(self):
    """Iterate through the output of the process.

    Multiple iterators can be instatiated for a Popen instance, e.g. to continue
    reading after a TimeoutError. Creating a new iterator invalidates all
    existing ones. The behavior when reading from old iterators is undefined.

    Raises:
      TimeoutError: if iteration times out
      PollError: if there is an unexpected poll event

    Yields:
      'outdata': if only stdout was PIPE or PTY
      'errdata': if only stderr was PIPE or PTY
      ('outdata', 'errdata') - if both stdout and stderr were PIPE or PTY
      an empty string indicates no output for that iteration.
    """
    # set the per iteration size based on bufsize
    print "Command issued is still running, please wait......"

    if self.bufsize < 1:
      itersize = 2**20  # Use 1M itersize for "as much as possible".
    else:
      itersize = self.bufsize
    # intialize files map and poller
    poller, files = select.poll(), {}
    # register stdin if we have it and it wasn't closed by a previous iterator.
    if self.stdin and not self.stdin.closed:
      # only register stdin if we have input, otherwise just close it
      if self.input:
        poller.register(self.stdin, select.POLLOUT)
        files[self.stdin.fileno()] = self.stdin
      else:
        self.stdin.close()
    # register stdout and sterr if we have them and they weren't closed by a
    # previous iterator.
    for handle in (f for f in (self.stdout, self.stderr) if f and not f.closed):
      poller.register(handle, select.POLLIN)
      files[handle.fileno()] = handle
    # iterate until input and output is finished
    while files:
      # make sure poll/read actions are atomic by aquiring lock
      with self.__race_lock:
        try:
          ready = poller.poll(self.timeout and self.timeout*1000.0)
        except select.error as e:
          # According to chapter 17, section 1 of Python standard library,
          # the exception value is a pair containing the numeric error code
          # from errno and the corresponding string as printed by C function
          # perror().
          if e.args[0] == errno.EINTR:
            # An interrupted system call. try the call again.
            continue
          else:
            # raise everything else that could happen.
            raise
        if not ready:
          raise TimeoutError(
              'command timed out in %s seconds' % self.timeout)
        for fd, event in ready:
          if event & (select.POLLERR | select.POLLNVAL):
            raise PollError(
                'command failed with invalid poll event %s' % event)
          elif event & select.POLLOUT:
            # write input and set data to remaining input
            if self.bufsize == 1:
              itersize = (self.input.find(b'\n') + 1) or None
            self.input = self.input[os.write(fd, self.input[:itersize]):]
            data = self.input
          else:
            # read output into data and set it to outdata or errdata
            try:
              if self.bufsize == 1:
                itersize = 2**10  # Use 1K itersize for line-buffering.
              data = os.read(fd, itersize)
            except (OSError, IOError) as e:
              # reading closed pty's raises IOError or OSError
              if not os.isatty(fd) or e.errno != 5:
                raise
              data = b''
            # Append the read data to the stdout or stderr buffers.
            if files[fd] is self.stdout:
              self.outbuff += data
            else:
              self.errbuff += data
          if not data:
            # no input remaining or output read, close and unregister file
            files[fd].close()
            poller.unregister(fd)
            del files[fd]
      # Break up the output buffers into blocks based on bufsize.
      outdata, errdata = self.outbuff, self.errbuff
      while outdata or errdata:
        if self.bufsize < 1:
          # For unbuffered modes, yield all the buffered data at once.
          outdata, self.outbuff = self.outbuff, b''
          errdata, self.errbuff = self.errbuff, b''
        else:
          # For buffered modes, yield the buffered data as itersize blocks.
          outdata, errdata = b'', b''
          if self.bufsize == 1:
            itersize = (self.outbuff.find(b'\n') + 1) or (len(self.outbuff) + 1)
          if self.outbuff and (len(self.outbuff) >= itersize or
                               self.stdout.closed):
            outdata, self.outbuff = (self.outbuff[:itersize],
                                     self.outbuff[itersize:])
          if self.bufsize == 1:
            itersize = (self.errbuff.find(b'\n') + 1) or (len(self.errbuff) + 1)
          if self.errbuff and (len(self.errbuff) >= itersize or
                               self.stderr.closed):
            errdata, self.errbuff = (self.errbuff[:itersize],
                                     self.errbuff[itersize:])
        # Yield appropriate output depending on what was requested.
        if outdata or errdata:
          if self.stdout and self.stderr:
            yield outdata, errdata
          elif self.stdout:
            yield outdata
          elif self.stderr:
            yield errdata
    # make sure the process is finished
    self.wait()

  def communicate(self, input=None):
    """Interact with a process, feeding it input and returning output.

    This is the same as subprocess.Popen.communicate() except it adds support
    for timeouts and sends any input provided at initialiasation before
    sending additional input provided to this method.

    Args:
      input: extra input to send to stdin after any initialisation input
        (default: None)

    Raises:
      TimeoutError: if IO times out
      PollError: if there is an unexpected poll event

    Returns:
      (stdout, sterr) tuple of ouput data
    """
    # extend self.input with additional input
    if isinstance(input, six.text_type):
      input = input.encode('utf-8')
    self.input = (self.input or b'') + (input or b'')
    # As an optimization (and to avoid potential b/3469176 style deadlock), set
    # aggressive buffering for communicate, regardless of bufsize.
    self.bufsize = -1
    try:
      # Create a list out of the iterated output.
      output = list(self)
    except TimeoutError:
      # On timeout, kill and reap the process and re-raise.
      self.kill()
      self.wait()
      raise
    # construct and return the (stdout, stderr) tuple
    if self.stdout and self.stderr:
      return b''.join(o[0] for o in output), b''.join(o[1] for o in output)
    elif self.stdout:
      return b''.join(output), None
    elif self.stderr:
      return None, b''.join(output)
    else:
      return None, None

  def poll(self, *args, **kwargs):
    """Work around a known race condition in subprocess fixed in Python 2.5."""
    # Another thread is operating on (likely waiting on) this process. Claim
    # that the process has not finished yet, unless the returncode attribute
    # has already bet set. Even if this is a lie, it's a harmless one --
    # generally anyone calling poll() will check back later. Much more often,
    # it means that another thread is blocking on wait().
    print "Command is finished."
    if not self.__race_lock.acquire(blocking=False):
      return self.returncode
    try:
      return super(Popen, self).poll(*args, **kwargs)
    finally:
      self.__race_lock.release()

  def wait(self, *args, **kwargs):
    """Work around a known race condition in subprocess fixed in Python 2.5."""
    print "Command is finished."

    with self.__race_lock:
      return super(Popen, self).wait(*args, **kwargs)

  # Python v2.6 introduced the kill() method.
  if not hasattr(subprocess.Popen, 'kill'):

    def kill(self):
      """Kill the subprocess."""
      os.kill(self.pid, signal.SIGKILL)

  # Python v2.6 introduced the terminate() method.
  if not hasattr(subprocess.Popen, 'terminate'):

    def terminate(self):
      """Terminate the subprocess."""
      os.kill(self.pid, signal.SIGTERM)


def countdown(t, step=1, msg='sleeping'):
    for i in range(t, 0, -step):
        pad_str = '.' * len('%d' % i)
        print '%s for the next %d seconds %s.\r' % (msg, i, pad_str),
        sys.stdout.flush()
        time.sleep(step)
    print 'Done %s for %d seconds!  %s' % (msg, t, pad_str)


def create_yaml_file_from_string(yaml_string, yaml_file):
    try:
        with open(yaml_file, 'w') as writer:
            writer.write(yaml_string)
    except EnvironmentError:
        print 'Oops: open file {} for write fails.'.format(yaml_file)
        sys.exit()
    return yaml_file    


def delete_yaml_files(userclusters):
    for usercluster in userclusters:
        workloadyaml = '{}.nginx.yaml'.format(usercluster.clustername)
        patchnodeyaml = "{}.patch.node.yaml".format(usercluster.clustername)
        exists = os.path.isfile(workloadyaml)
        if exists:
            try:
                os.remove(workloadyaml)
            except EnvironmentError:
                print 'Oops: delete yaml file fails.'
                sys.exit()
        exists = os.path.isfile(patchnodeyaml)
        if exists:
            try:
                os.remove(patchnodeyaml)
            except EnvironmentError:
                print 'Oops: delete yaml file fails.'
                sys.exit()


def send_log_to_stdout():
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    root.addHandler(handler)


def env_prepare():
    retcode = 1
    retOutput = ""
    cmdline =  "sudo apt list --installed"
    try:
        (retcode, retOutput) = RunCmd(cmdline, 15, None, wait=2, counter=3)
    except:
        retOutput = ""
    if not "apache2-utils" in retOutput:
        package_install_cli = 'sudo apt-get install apache2-utils -y'
        try:
            (retcode, retOutput) = RunCmd(package_install_cli, 15, None, wait=2, counter=3)
        except:
            print "Fail to install package. Please check package apache2-utils is installed."
            print "Use {} to install package".format(package_install_cli)
            print "Output for cmdline {}: {}".format(package_install_cli, retOutput)
            countdown(2)
        if retcode == 1:
            print "Fail to install package. Please check package apache2-utils is installed."
            print "Use {} to install package".format(package_install_cli)
            print "Output for cmdline {}: {}".format(package_install_cli, retOutput)
            countdown(2)

def env_check():
    cmdline = "kubectl --help"
    (retcode, retOuput) = RunCmd(cmdline, 15, None, wait=2, counter=3)
    if retcode == 1:
        print "Fail to run kubectl. Kubectl is required to run the test script."
        print "Please refer to https://kubernetes.io/docs/tasks/tools/install-kubectl/ to install kubectl"
        sys.exit()
    cmdline = "gkectl --help"
    (retcode, retOuput) = RunCmd(cmdline, 15, None, wait=2, counter=3)
    if retcode == 1:
        print "Fail to run gkectl. gkectl is required to run the test script."
        sys.exit()


class CommandFailError(Exception):
     pass


def RunCmd(cmd, timeout, output_file=None, wait=2, counter=0, **kwargs):
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
    outfile = output_file and open(output_file, 'a') or PIPE
    bash = Popen(cmd, stdout=outfile, stderr=outfile, timeout=timeout,
                           shell=True)
    output, err = bash.communicate()
    if bash.returncode != 0 and not kwargs.get('no_raise'):
        print "Fail to run cmd {}".format(cmd)
    return bash.returncode, output, err

  timeout = max(timeout, 20)
  rc, out, err = RetryCmd(cmd, timeout, output_file)
  if rc == 0:
    return (0, err and out + '\n' + err or out)
  return (rc, err)


def gcp_auth(serviceacct):
    # gcloud auth activate-service-account --key-file=release-reader-key.json
    cmdline = 'gcloud auth activate-service-account --key-file={}'.format(serviceacct)
    print cmdline
    (retcode, retOuput) = RunCmd(cmdline, 15, None, wait=2, counter=3)
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
        (retcode, cmdoutput) = RunCmd(cmdline, 15, None, wait=2, counter=3)
    except Exception as e:
        print "Upload file to gcs bucker fails."
        return False, errmsg
    return True, cmdoutput


def check_service_availbility(svc_endpoint, testreportlog):
    cmdline = 'curl -s http://{}/index.html'.format(svc_endpoint)
    retcode = 1
    retry = 5
    interval = 2
    count = 0
    cmdOutput = ""
    while count < retry and not http_target_string in cmdOutput:
        print "Running {} for {} time".format(cmdline, count)
        try: 
            (retcode, cmdOutput) = RunCmd(cmdline, 15, None, wait=2, counter=3)
        except:
            retcode = 1
            cmdOutput = ""
        count += 1
        print cmdOutput
        if not http_target_string in cmdOutput:
            countdown(interval)
        testreportlog.detail2file(cmdline)
        testreportlog.detail2file(cmdOutput)
    if http_target_string in cmdOutput:
        return True
    else:
        return False


def get_info_from_workload_yaml_file(workloadyaml):
    namespace = None
    replicas = None
    deployment = None
    servicetype = None
    output = None
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

    def changeformat(self):
        formatter = logging.Formatter(' %(message)s')
        self.handler.setFormatter(formatter)


class gkeonpremcluster:
    def __init__(self, clustercfgfile, isAdminCluster, detaillogfile):
        self.clustercfgfile = clustercfgfile
        self.isAdminCluster = isAdminCluster
        self.objectsList = []
        self.objectsDict = {}
        self.namespacesDict = {}
        self.detaillog = detaillogfile
        self.get_cluster_server_ip()
        if not self.check_cluster_server_connectivity():
            self.reachable = False
            self.control_version = '0.0.0'
            self.detaillog.detail2file("server ip for cluster defined by {} is not reachable at server ip {}.".format(self.clustercfgfile, self.serverip))
        else:
            self.reachable = True
            self.get_cluster_name()
            self.get_gke_version()
            if isAdminCluster:
                self.readyreplicas = 0
            else:
                self.get_number_machine_deployments()
            self.get_namespace()
            for eachnamespace in self.namespacesDict.keys():
                self.get_all_for_namespace(eachnamespace)
            if not isAdminCluster:    
                self.get_machine_deployment_name()    
            self.description()
            self.detaillog.detail2file("Self server ip for cluster {}: {}".format(self.clustername, self.serverip))
            self.detaillog.detail2file("Number of machines in the cluster: ".format(self.readyreplicas))

    def get_cluster_name(self):
        cmdline = 'kubectl --kubeconfig {} get cluster'.format(self.clustercfgfile)
        cmdoutput = ""
        self.detaillog.detail2file(cmdline)
        try:
            (retcode, cmdoutput) = RunCmd(cmdline, 15, None, wait=2, counter=3)
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
             (retcode, cmdoutput) = RunCmd(cmdline, 15, None, wait=2, counter=3)
        except Exception as e:
              self.control_version = None
        self.detaillog.detail2file(cmdoutput)
        #Control Plane Version:    1.11.2-gke.31
        pattern = re.compile(r"Control\sPlane\sVersion:\s*([a-zA-Z\d\-\.]+)")
        matched = pattern.search(cmdoutput)
        if matched:
            self.control_version = matched.group(1)



    def get_machine_deployment_name(self):
        cmdline = 'kubectl --kubeconfig {} get machinedeployments'.format(self.clustercfgfile)
        (retcode, retoutput) = RunCmd(cmdline, 15, None, wait=2, counter=3)
        self.detaillog.detail2file(retoutput)
        machine_deployment_name = None
        deployment_pattern = re.compile(r"([a-zA-Z0-9\-]+)\s*[0-9]+")
        if deployment_pattern.search(retoutput):
            machine_deployment_name = deployment_pattern.search(retoutput).group(1)
        self.machine_deployment_name = machine_deployment_name
        print "get_machine_deployment_name: ", self.machine_deployment_name


    def get_number_machine_deployments(self):
        #  Available Replicas:    3
        #  Observed Generation:   49
        #  Ready Replicas:        3
        #  Replicas:              10
        #  Unavailable Replicas:  7
        #  Updated Replicas:      10
        self.readyreplicas = 1
        availablereplicas = -1
        unavailablereplicas = -1
        readyreplicas = 0
        updatedreplicas = -2
        retry = 60
        count = 0
        interval = 2
        while count < retry and (not unavailablereplicas == 0 or not availablereplicas == updatedreplicas or not readyreplicas == updatedreplicas):
            print "Polling ready deployed machine, ".format(count), "continue? {}".format(count < retry)
            cmdline = 'kubectl --kubeconfig {} describe machinedeployments {} | grep Replicas'.format(self.clustercfgfile, self.clustername)
            self.detaillog.detail2file(cmdline)
            (retcode, retoutput) = RunCmd(cmdline, 15, None, wait=2, counter=3)
            self.detaillog.detail2file(retoutput)
            retoutput = ' '.join(re.split("\n+", retoutput))
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
                countdown(interval)
            count += 1
        self.readyreplicas = int(readyreplicas)
        self.detaillog.detail2file("Cluster ready Replicas: {}".format(self.readyreplicas))

    def check_cluster_server_connectivity(self):
        cmdline = 'ping {} -c 2'.format(self.serverip)
        self.detaillog.detail2file(cmdline)
        try:
            (retcode, cmdoutput) = RunCmd(cmdline, 15, None, wait=2, counter=3)
        except Exception as e:
            print "Run cmd {} fails.".format(cmdline)
        self.detaillog.detail2file(cmdoutput)
        pattern = re.compile(r"2 packets transmitted, 2 received, 0% packet loss")
        if pattern.search(cmdoutput) != None:
            return True
        else:
            return False

    def get_cluster_server_ip(self):
       #server: https://100.115.253.83:443
        cluser_server_ip = '0.0.0.0'
        with open(self.clustercfgfile) as cfgfile:
            cfgoutput = cfgfile.read()
            pattern = re.compile(r"server:\shttps:\/\/(\d+.\d+.\d+.\d+):443")
            if pattern.search(cfgoutput) != None:
                cluser_server_ip = pattern.search(cfgoutput).group(1)

        self.serverip = cluser_server_ip

    def description(self):
        if self.isAdminCluster:
            self.detaillog.detail2file("Cluster defined in {} is admin cluster".format(self.clustercfgfile))
            return "Admin Cluster defined in {} is admin cluster".format(self.clustercfgfile)
        else:
            self.detaillog.detail2file("Cluster defined in {} is user cluster ".format(self.clustercfgfile))
            return "User Cluster defined in {} is user cluster".format(self.clustercfgfile)

    def dump_cluster_all(self):
        cmdline = 'kubectl --kubeconfig {} get all --all-namespaces'.format(self.clustercfgfile)
        self.detaillog.detail2file(cmdline)
        try:
            (retcode, cmdoutput) = RunCmd(cmdline, 15, None, wait=2, counter=3)
        except Exception as e:
            print "Run cmd {} fails.".format(cmdline)
            sys.exit()
        self.detaillog.detail2file(cmdoutput)
        return cmdoutput

    def workload_deployment(self, workloadyaml):
        namespace, _, _, _ = get_info_from_workload_yaml_file(workloadyaml)
        cmdoutput = ""
        retcode = 1
        if not namespace:
            namespace = "nginx-sanity-ns"
        if not namespace in self.objectsList:
            cmdline = 'kubectl --kubeconfig {} create namespace {}'.format(self.clustercfgfile, namespace)
            self.detaillog.detail2file(cmdline)
            try:
                (retcode, cmdoutput) = RunCmd(cmdline, 15, None, wait=2, counter=3)
            except Exception as e:
                self.detaillog.detail2file(cmdoutput)
                return (retcode, cmdoutput)
            self.detaillog.detail2file(cmdoutput)

        cmdoutput = ""
        retcode = 1
        self.detaillog.detail2file("Generating test service deployment")
        cmdline = 'kubectl --kubeconfig {} apply -f {}'.format(self.clustercfgfile, workloadyaml)
        self.detaillog.detail2file(cmdline)
        try:
           (retcode, cmdoutput) = RunCmd(cmdline, 15, None, wait=2, counter=3)
        except Exception as e:
            print "Run cmd {} fails.".format(cmdline)
            self.detaillog.detail2file(cmdoutput)
            return (retcode, cmdoutput)

        self.detaillog.detail2file(cmdoutput)
        return (retcode, cmdoutput)

    def workload_withdraw(self, workloadyaml):
        retcode = 1
        cmdoutput = None
        self.detaillog.detail2file("Removing test service deployment")
        cmdline = 'kubectl --kubeconfig {} delete -f {}'.format(self.clustercfgfile, workloadyaml)
        self.detaillog.detail2file(cmdline)
        try:
            (retcode, cmdoutput) = RunCmd(cmdline, 15, None, wait=2, counter=3)
        except Exception as e:
            print "Run cmd {} fails.".format(cmdline)
            self.detaillog.detail2file(cmdoutput)
            return (retcode, cmdoutput)

        self.detaillog.detail2file(cmdoutput)
        return (retcode, cmdoutput)

    def delete_namespace(self, namespace):
        retcode = 1
        cmdoutput = ""

        cmdline = 'kubectl --kubeconfig {} delete namespace {}'.format(self.clustercfgfile, namespace)
        self.detaillog.detail2file(cmdline)
        try:
            (retcode, cmdoutput) = RunCmd(cmdline, 15, None, wait=2, counter=3)
        except Exception as e:
            print "Run cmd {} fails.".format(cmdline)
            self.detaillog.detail2file(cmdoutput)
            return (retcode, cmdoutput)

        self.detaillog.detail2file(cmdoutput)
        self.objectsList.remove(namespace)
        return (retcode, cmdoutput)

    def workload_replica_modify(self, deployment_name, workloadns, new_replica):
        #kubectl --kubeconfig kubecfg/userclustercfg scale --replicas=4 deployment/nginx-sanity-test
        retcode = 1
        cmdoutput = ""

        cmdline = 'kubectl --kubeconfig {} scale --replicas={} deployment/{} -n {}'.format(self.clustercfgfile, new_replica, deployment_name, workloadns)

        self.detaillog.detail2file(cmdline)
        try:
            (retcode, cmdoutput) = RunCmd(cmdline, 15, None, wait=2, counter=3)
        except Exception as e:
            print "Run cmd {} fails.".format(cmdline)
            self.detaillog.detail2file(cmdoutput)
            return (retcode, cmdoutput)
        self.detaillog.detail2file(cmdoutput)
        return (retcode, cmdoutput)

    def change_number_of_machine_deployment(self, numberofmachines, patchnodeyaml):
        retcode = 1
        cmdoutput = "" 
        yaml_string = "{}  replicas: {}".format(patch_node_string, numberofmachines)
        patchnodeyaml = create_yaml_file_from_string(yaml_string, patchnodeyaml)
        #'kubectl --kubeconfig {} patch machinedeployment {} -p "{\"spec\": {\"replicas\": {}}}" --type=merge'.format(self.clustercfgfile, self.clustername, numberofmachines)
        #kubectl --kubeconfig {} patch machinedeployment cpe-user-1-1 -p "{\"spec\": {\"replicas\": 3}}" --type=merge
        cmdline = 'kubectl --kubeconfig {} patch machinedeployment {} --patch "$(cat {})"  --type=merge'.format(self.clustercfgfile, self.clustername, patchnodeyaml)
        try:
            (retcode, retOutput) = RunCmd(cmdline, 15, None, wait=2, counter=0)
        except Exception as e:
            print "Run cmd {} fails.".format(cmdline)
            self.detaillog.detail2file(cmdoutput)
            return (retcode, cmdoutput)
        self.detaillog.detail2file(retOutput)
        return (retcode, cmdoutput)

    def gkectl_diagnose_cluster(self):
        retry = 15
        interval = 2
        retOutput = ""
        count = 0
        check_output = "Cluster is healthy"
        cmdline = 'gkectl diagnose cluster --kubeconfig {}'.format(self.clustercfgfile)
        self.detaillog.detail2file(cmdline)
        while count < retry and not check_output in retOutput:
            (retcode, retOutput) = RunCmd(cmdline, 15, None, wait=2, counter=0)
            self.detaillog.detail2file(retOutput)
            count += 1
            countdown(interval)
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
            (retcode, cmdoutput) = RunCmd(cmdline, 15, None, wait=2, counter=3)
            cmdoutput = cmdoutput.splitlines()[1:]
        except Exception as e:
             print "Run cmd {} fails.".format(cmdline)
        for eachline in cmdoutput:
            self.detaillog.detail2file(eachline)

        for eachline in cmdoutput:
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
        retry = 30
        count = 0
        internal = 2
        stable_state = False
        cmdoutput = ""
        cmdline = 'kubectl --kubeconfig {} get all -n {}'.format(self.clustercfgfile, namespace)
        self.detaillog.detail2file(cmdline)
        while count < retry and not stable_state:
            print "Polling all objects in namespace {}.".format(namespace)
            try:
                (retcode, cmdoutput) = RunCmd(cmdline, 15, None, wait=2, counter=3)
                self.detaillog.detail2file(cmdoutput)
                if not "Pending" in cmdoutput and not "ContainerCreating" in cmdoutput and not "Terminating" in cmdoutput:
                    stable_state = True
                else:
                    countdown(internal)
                    count += 1
                    continue

            except Exception as e:
                print "Run cmd {} fails.".format(cmdline)
        cmdoutput = cmdoutput.splitlines()[1:]

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
                daemonset[matchresult.group(1)] = [int(matchresult.group(2)), int(matchresult.group(3)), int(matchresult.group(4)), matchresult.group(5)]
                found = True
                continue
            matchresult = statefulsetpattern.search(eachline)
            if matchresult:
                statefulset[matchresult.group(1)] = [int(matchresult.group(2)), int(matchresult.group(3))]
                found = True
                continue
        self.objectsDict[namespace] = [pods, services, deployment, replicaset, daemonset, statefulset]
        self.objectsList.append(namespace)


def get_gkectl_version():
    cmdline = "gkectl version"
    retOutput = "Unknown"
    try:
        (retcode, retOutput) = RunCmd(cmdline, 15, None, wait=2, counter=3)
    except:
        print 'Fail to run cmd {}.'.format(cmdline)
    # gkectl 1.0.6 (git-732c79df2)
    #print retOutput
    verpattern = re.compile(r"gkectl\s([\d.]+)")
    matched =  verpattern.search(retOutput)
    if matched:
        gkectl_version = matched.group(1)
    else:
        gkectl_version = retOutput.strip()
    return gkectl_version


def test_abort(testreportlog, cluster=None):
    testreportlog.info2file("Test is aborted.")
    if not cluster:
        sys.exit(1)
    if cluster.isAdminCluster:
        generate_test_summary(testreportlog, cluster, None)
    else:
        generate_test_summary(testreportlog, None, cluster)
    sys.exit(1)


def test_workload_deleted(cluster, namespace):
    abortonfailure = test_cfg['abortonfailure']
    test_detail = "Workload defined by {} is deleted for cluster {} in namespace {}.".format(workloadyaml, cluster.clustercfgfile, namespace)
    test_name = 'test_workload_deleted'
    cmdline = 'kubectl --kubeconfig={} get all -n {}'.format(cluster.clustercfgfile, namespace)
    testreportlog.info2file(cmdline)
    retcode, retoutput = RunCmd(cmdline, 15, None, wait=2, counter=3)
    if "No resources found" in retoutput:
        test_result = "PASS"
    else:
        test_result = "FAIL"
    testreportlog.info2file("Test_Case: {}: {}: {}".format(test_name, test_result, test_detail))
    test_results.append([test_name, test_result, test_detail])
    if test_result == "FAIL" and abortonfailure:
        test_abort(testreportlog, cluster)

    return test_result=="PASS"


def test_cluster_sanity(cluster, testreportlog):
    abortonfailure = test_cfg['abortonfailure']
    test_detail = "Cluster Sanity Check for cluster defined by {}.".format(cluster.clustercfgfile)
    test_name = "test_cluster_sanity"
    retCode = cluster.gkectl_diagnose_cluster()
    if retCode:
        test_result = "PASS"
    else:
        test_result = "FAIL"

    testreportlog.info2file("Test_Case: {}: {}: {}".format(test_name, test_result, test_detail))
    test_results.append([test_name, test_result, test_detail])
    if test_result == "FAIL" and abortonfailure:
        test_abort(testreportlog, cluster)
    return test_result=="PASS"


def test_machinedeployment_update(usercluster, patchnodeyaml, testreportlog):
    abortonfailure = test_cfg['abortonfailure']
    retry = 3
    count = 0
    delta = 1
    interval = 10
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
                countdown(interval)
            count += 1

        testreportlog.info2file("Test_Case: {}: {}: {}".format(test_name, test_result, test_detail))
        test_results.append([test_name, test_result, test_detail])

        if test_result == "FAIL" and abortonfailure:
            test_abort(testreportlog, usercluster)

    return test_result == "PASS"


def test_workload_deployment(usercluster, testreportlog, workloadyaml):
    abortonfailure = test_cfg['abortonfailure']
    lbsvcip = test_cfg['lbsvcip']

    test_detail = "Apply yaml file {} in cluster {}.".format(workloadyaml, usercluster.clustername)
    test_name = "test_workload_deployment"
    yaml_string = "{}  loadBalancerIP: {}".format(nginx_yaml_string, lbsvcip)
    workloadyaml = create_yaml_file_from_string(yaml_string, workloadyaml)
    
    (retcode, retOutput) = usercluster.workload_deployment(workloadyaml)
    testreportlog.detail2file(retOutput)
    if "created" in retOutput or "unchanged" in retOutput:
        test_result = "PASS"
    else:
        test_result = "FAIL"
    testreportlog.info2file("Test_Case: {}: {}: {}".format(test_name, test_result, test_detail))
    test_results.append([test_name, test_result, test_detail])
    if test_result == "FAIL" and abortonfailure:
        test_abort(testreportlog, usercluster)

    return test_result == "PASS"


def test_workload_withdraw(usercluster, workloadyaml, testreportlog):
    abortonfailure = test_cfg['abortonfailure']

    test_detail = "Delete workflow defined by yaml file {} in cluster {}.".format(workloadyaml, usercluster.clustername)
    test_name = "test_workload_withdraw"

    (retcode, retOutput) = usercluster.workload_withdraw(workloadyaml)
    if "deleted" in retOutput:
         test_result = "PASS"
    else:
        test_result = "FAIL"
    testreportlog.info2file("Test_Case: {}: {}: {}".format(test_name, test_result, test_detail))
    test_results.append([test_name, test_result, test_detail])
    if test_result == "FAIL" and abortonfailure:
        test_abort(testreportlog, usercluster)

    return test_result


def test_workload_deployed(usercluster, workloadns, testreportlog):
    abortonfailure = test_cfg['abortonfailure']
    test_detail = "Verify workflow specified by {} is deployed in cluster {}.".format(workloadns, usercluster.clustername)
    test_name = "test_workload_deployed"
    if workloadns in usercluster.objectsList:
        test_result="PASS"
    else:
        test_result="FAIL"
    testreportlog.info2file("Test_Case: {}: {}: {}".format(test_name, test_result, test_detail))
    test_results.append([test_name, test_result, test_detail])
    if test_result == "FAIL" and abortonfailure:
        test_abort(testreportlog, usercluster)

    return  test_result == "PASS"


def test_workload_number_of_pods(usercluster, workloadns, expected_number, testreportlog):
    abortonfailure = test_cfg['abortonfailure']

    test_detail = "Verify number of pods for workload specified by {} deployed in cluster {} equals to the expected number {}.".format(workloadns, usercluster.clustername, expected_number)
    test_name = "test_workload_number_of_pods"
    if len(usercluster.objectsDict[workloadns][0].keys()) == expected_number:
            test_result="PASS"
    else:
        test_result="FAIL"
    testreportlog.info2file("Test_Case: {}: {}: {}".format(test_name, test_result, test_detail))
    test_results.append([test_name, test_result, test_detail])
    if test_result == "FAIL" and abortonfailure:
        test_abort(testreportlog, usercluster)

    return  test_result == "PASS"


def test_workload_pod_state(usercluster, workloadns, expected_state, testreportlog):
    abortonfailure = test_cfg['abortonfailure']
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
        test_abort(testreportlog, usercluster)

    return  test_result == "PASS"


def test_workload_service_state(usercluster, workloadns, service_type, testreportlog):
    abortonfailure = test_cfg['abortonfailure']
    expected_lb_ip = test_cfg['lbsvcip']
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
        test_abort(testreportlog, usercluster)

    return  test_result == "PASS"


def test_workload_deployment_state(usercluster, workloadns, expected_number, testreportlog):
    abortonfailure = test_cfg['abortonfailure']
    test_detail = "Verify deployment for workload specified by {} deployed in cluster {} equals to {}.".format(workloadns, usercluster.clustername, expected_number)
    test_name = "test_workload_service_state"
    if usercluster.objectsDict[workloadns][2].values()[0][0] == expected_number and usercluster.objectsDict[workloadns][2].values()[0][1] == expected_number:
        test_result="PASS"
    else:
        test_result="FAIL"
    testreportlog.info2file("Test_Case: {}: {}: {}".format(test_name, test_result, test_detail))
    test_results.append([test_name, test_result, test_detail])
    if test_result == "FAIL" and abortonfailure:
        test_abort(testreportlog, usercluster)

    return  test_result == "PASS"


def test_workload_replica_state(usercluster, workloadns, expected_number, testreportlog):
    abortonfailure = test_cfg['abortonfailure']
    test_detail = "Verify replicas for workload specified by {} deployed in cluster {} equals to {}.".format(workloadns, usercluster.clustername, expected_number)
    test_name = "test_workload_replica_state"
    if usercluster.objectsDict[workloadns][3].values()[0][0] == expected_number and usercluster.objectsDict[workloadns][3].values()[0][1] == expected_number:
        test_result="PASS"
    else:
        test_result="FAIL"
    testreportlog.info2file("Test_Case: {}: {}: {}".format(test_name, test_result, test_detail))
    test_results.append([test_name, test_result, test_detail])
    if test_result == "FAIL" and abortonfailure:
        test_abort(testreportlog, usercluster)

    return  test_result == "PASS"


def test_workload_accessible_via_lbsvcip(usercluster, workloadns, testreportlog):
    lbsvcip = test_cfg['lbsvcip']
    abortonfailure = test_cfg['abortonfailure']
    test_detail = "Verify service provided by workload is accessible via LBIP {} in cluster {}.".format(lbsvcip, usercluster.clustername)
    test_name = "test_workload_accessible_via_lbsvcip"

    if check_service_availbility(lbsvcip, testreportlog):
        test_result="PASS"
    else:
        test_result="FAIL"
    testreportlog.info2file("Test_Case: {}: {}: {}".format(test_name, test_result, test_detail))
    test_results.append([test_name, test_result, test_detail])
    if test_result == "FAIL" and abortonfailure:
        test_abort(testreportlog, usercluster)

    return  test_result == "PASS"


def test_service_traffic(usercluster, concurrent_session, total_request, expected_duration, testreportlog):
    lbsvcip = test_cfg['lbsvcip']
    abortonfailure = test_cfg['abortonfailure']
    retOutput = "" 
    test_result = "FAIL"
    test_detail = "Verify service traffic at {} in user cluster {}.".format(lbsvcip, usercluster.clustername)
    test_name = "test_service_traffic"
    cmdline = "ab -c {} -n {}  http://{}/index.html".format(concurrent_session, total_request, lbsvcip)
    testreportlog.info2file(cmdline)

    retry = 10
    interval = 2
    count = 0
    match_string = "Finished {} requests".format(total_request)
    finished_pattern = re.compile(r"Complete\srequests:\s*(\d*)")
    failed_pattern = re.compile(r"Failed\srequests:\s*0")
    traffic_passed = False

    while count < retry and not traffic_passed:
        retOutput = ""
        try:
            (retcode, retOutput) = RunCmd(cmdline, 10*expected_duration, None, wait=2, counter=0)
        except:
            count += 1
            retcode = 1
            testreportlog.info2file(retOutput)
            countdown(interval)
            continue
        
        if retcode == 1:
            print "Fail to run cmd {}".format(cmdline)
            counttraffic_passed = True
            testreportlog.info2file(retOutput)
            countdown(interval)
            continue
        else:
            testreportlog.info2file(retOutput)
            if finished_pattern.search(retOutput) != None:
                passed_request = int(finished_pattern.search(retOutput).group(1))
                traffic_passed = True
            else:
                countdown(interval)
        count += 1

    if retcode == 1:
        test_result = "FAIL"
    else:
        if traffic_passed:
            timepattern = re.compile(r"Time\staken\sfor\stests:\s+(\d+\.\d*)\sseconds")
            if timepattern.search(retOutput) != None:
                actual_time_used = float(timepattern.search(retOutput).group(1))
                print "Actual_time_used: {} seconds".format(actual_time_used)
                testreportlog.info2file("Actual time used: {} for {} request with {} concurrent sessions".format(actual_time_used, total_request, concurrent_session))
                testreportlog.info2file("Ideal time used should be less than 0.5s for {} request with {} concurrent sessions.".format(total_request, concurrent_session))
                if actual_time_used < expected_duration and passed_request == total_request:
                    test_result = "PASS"
                else:
                    test_result = "FAIL"
            else:
                print "Fail to find time taken"
                test_result = "FAIL"
        else:
            test_result = "FAIL"
            print "Fail to find number of completed request."
    testreportlog.info2file("Test_Case: {}: {}: {}".format(test_name, test_result, test_detail))
    test_results.append([test_name, test_result, test_detail])
    if test_result == "FAIL" and abortonfailure:
        test_abort(testreportlog, usercluster)
    return test_result == "PASS"


def cluster_cleanup(usercluster, workloadyaml):
    namespace, _, _, _ = get_info_from_workload_yaml_file(workloadyaml)
    if namespace in usercluster.objectsList:
        usercluster.delete_namespace(namespace)


def gkectl_diagnose_cluster(clustercfgfile, testreportlog):
        retry = 15
        interval = 2
        retOutput = ""
        count = 0
        check_output = "Cluster is healthy"
        cmdline = 'gkectl diagnose cluster --kubeconfig {}'.format(clustercfgfile)
        testreportlog.detail2file(cmdline)
        while count < retry and not check_output in retOutput:
            (retcode, retOutput) = RunCmd(cmdline, 15, None, wait=2, counter=0)
            testreportlog.detail2file(retOutput)
            count += 1
            countdown(interval)
        return check_output in retOutput and retcode == 0


def create_gke_onprem_cluster(yamlfile, cluster_name, usercluster_name,  testreportlog):
    test_detail = "Verify gkectl can create a new GKE OnPrem Cluster defined by {}.".format(yamlfile)
    test_name = "test_gke_onprem_cluster_creation"
    abortonfailure = test_cfg['abortonfailure']
    clustercfgpath = test_cfg['clustercfgpath']
    cmdline = "gkectl check-config --config {}".format(yamlfile)
    testreportlog.info2file(cmdline)
    try:
        (retcode, retoutput) = RunCmd(cmdline, 60, None, wait=2, counter=3)
    except:
        print "Fail to run cmd {}".format(cmdline)
    if retcode == 1 or not "All validations SUCCEEDED" in retoutput or "FAILURE" in retoutput:
        testreportlog.info2file("Cluster creation yaml file {} fails to pass sanity check.".format(yamlfile))
        test_result = "FAIL"
    if not test_cfg['skipimageprepare']:
        cmdline = "gkectl prepare --config {} --validate-attestations".format(yamlfile)
        testreportlog.info2file(cmdline)
        try:
            (retcode, retoutput) = RunCmd(cmdline, 120, None, wait=2, counter=3)
        except:
            print "Fail to run cmd {}".format(cmdline)
            retcode == 1
        if retcode == 1 or "error" in retoutput:
            testreportlog.info2file("Cluster prepare cmd fails for yaml file {} .".format(yamlfile))
            test_result = "FAIL"

    cmdline = "gkectl create cluster --config {} --kubeconfig-out {}/admin-{}-kubeconfig  -v 5".format(yamlfile, clustercfgpath, cluster_name)
    testreportlog.info2file(cmdline)
    try:
       (retcode, retoutput) = RunCmd(cmdline, 3000, None, wait=2, counter=3)
    except:
        print "Fail to run cmd {}".format(cmdline)
        retcode == 1
    if retcode == 1 or "FAILURE" in retoutput:
        testreportlog.info2file("Cluster creation fail using yaml file {}.".format(yamlfile))
        test_result = "FAIL"
    else:
        test_result="PASS"

    if not test_result == "PASS":    
        countdown(10)
        admincfg = "{}/admin-{}-kubeconfig".format(clustercfgpath, cluster_name)
        usercfg = "{}/{}-kubeconfig".format(clustercfgpath, usercluster_name)
        if gkectl_diagnose_cluster(admincfg, testreportlog) and gkectl_diagnose_cluster(usercfg, testreportlog):
            test_result="PASS"


    testreportlog.info2file("Test_Case: {}: {}: {}".format(test_name, test_result, test_detail))
    test_results.append([test_name, test_result, test_detail])
    if test_result == "FAIL" and abortonfailure:
        test_abort(testreportlog)

    return  test_result == "PASS"


def test_create_gke_on_prem_cluster(testreportlog):
    yamlfile = test_cfg['createyamlfile']
    onprem_cluster = re.compile(r"([a-zA-Z\-0-9]+).yaml")
    cluster_name = ""
    if onprem_cluster.search(yamlfile):
        cluster_name = onprem_cluster.search(yamlfile).group(1)

    clusteryamlfile = "{}/{}".format(test_cfg['clustercfgpath'], test_cfg['createyamlfile'])
    testreportlog.info2file('The script is going to create a brand new GKE On-Prem Cluster defined by creation yaml file {}.'.format(clusteryamlfile))

    cmdline = "grep clustername: {}".format(clusteryamlfile)
    (retcode, retoutput) = RunCmd(cmdline, 10, None, wait=2, counter=3)
    user_cluster_name = None
    if retcode == 0:
            namepattern = re.compile(r"clustername: \"([a-zA-Z0-9\-]+)\"")
            if namepattern.search(retoutput):
                user_cluster_name = namepattern.search(retoutput).group(1)
            else:
                print "fail to find user cluster name"
                sys.exit(0)
    else:
            print "fail to find user cluster name"
            sys.exit(0)


    retcode = create_gke_onprem_cluster(clusteryamlfile, cluster_name, user_cluster_name,  testreportlog)

    if not retcode:
        testreportlog.info2file('Fail to create GKE OnPrem Cluster')
        sys.exit(1)
    else:

        test_cfg['admcfg'] = "admin-{}-kubeconfig".format(cluster_name)
        test_cfg['usercfg'] = "{}-kubeconfig".format(user_cluster_name)
        testreportlog.info2file('GKE OnPrem Cluster is created. 2 kubeconfig files: {}, {} under {}'.format(test_cfg['admcfg'], test_cfg['usercfg'], test_cfg['clustercfgpath']))


def delete_user_cluster(admincluster, usercluster, testreportlog):
    # REF: https://cloud.google.com/gke-on-prem/docs/how-to/administration/deleting-a-user-cluster
    abortonfailure = test_cfg['abortonfailure']
    test_detail = "Verify user cluster {} can be deleted.".format(usercluster.clustername)
    test_name = "test_user_cluster_deletion"
    test_result = "FAIL"

    print usercluster.clustercfgfile, usercluster.clustername

    de_register_user_cluster_from_hub(usercluster, testreportlog)

    try:
        cmdline = "kubectl --kubeconfig {} delete monitoring --all -n kube-system".format(usercluster.clustercfgfile)
        testreportlog.info2file(cmdline) 
        (retcode, retoutput) = RunCmd(cmdline,120, None, wait=2, counter=3)
    except:
        print "fail to run cmd {}".format(cmdline)
        return False

    countdown(5)

    if retcode == 1:
        return False

    try:    
        cmdline = "kubectl --kubeconfig {} delete stackdriver --all -n kube-system".format(usercluster.clustercfgfile)
        testreportlog.info2file(cmdline) 
        (retcode, retoutput) = RunCmd(cmdline, 120, None, wait=2, counter=3)
    except:
        print "fail to run cmd {}".format(cmdline)
        return False

    countdown(5)

    if retcode == 1:
        return False


    cmdline = "kubectl --kubeconfig {} get pvc -n kube-system".format(usercluster.clustercfgfile)
    testreportlog.info2file(cmdline)
    try: 
        (retcode, retoutput) = RunCmd(cmdline, 60, None, wait=2, counter=3)
    except:
        print "fail to run cmd {}".format(cmdline)
        return False

    
    print retoutput
    if retcode == 0:
        pvc_pattern = re.compile(r"([a-zA-Z0-9\-]+)\s*Bound\s*pvc")
        pvc_list = []
        for eachline in retoutput.splitlines():
            print eachline
            if pvc_pattern.search(eachline):
                pvc_list.append(eachline.split()[0])
        print pvc_list        
        for eachpvc in pvc_list:
            cmdline = "kubectl --kubeconfig {} delete pvc {} -n kube-system".format(usercluster.clustercfgfile, eachpvc)
            testreportlog.info2file(cmdline)
            try:
                (retcode, retoutput) = RunCmd(cmdline, 60, None, wait=2, counter=3)
            except:
                print "fail to run cmd {}".format(cmdline)
                return False

            print retcode, retoutput
            countdown(5)
            if retcode == 1:
                return False
    else:
        return False

    
    cmdline = "kubectl --kubeconfig {} delete ns gke-system".format(usercluster.clustercfgfile)
    ### the above cmd fails
    testreportlog.info2file(cmdline)
    try:
        (retcode, retoutput) = RunCmd(cmdline, 120, None, wait=2, counter=3)
    except:
        print "fail to run cmd {}".format(cmdline)
        return False


    print retoutput
    countdown(15)
    if retcode == 1:
        return False


    cmdline = "kubectl --kubeconfig {} delete machinedeployments {}".format(usercluster.clustercfgfile, usercluster.machine_deployment_name)
    testreportlog.info2file(cmdline)
    try:
        (retcode, retoutput) = RunCmd(cmdline, 180, None, wait=2, counter=3)
    except:
        print "fail to run cmd {}".format(cmdline)
        return False

    countdown(120)
    if retcode == 1:
        return False

    # delete additional machine if existing
    cmdline = "kubectl --kubeconfig {} get machines".format(usercluster.clustercfgfile)
    testreportlog.info2file(cmdline)
    try:
        (retcode, retoutput) = RunCmd(cmdline, 120, None, wait=2, counter=3)
    except:
        print "fail to run cmd {}".format(cmdline)
    if retcode == 0:
        pvc_pattern = re.compile(r"([a-zA-Z0-9\-]+)\s*[0-9]+")
        pvc_list = []
        for eachline in retoutput.splitlines():
            if pvc_pattern.search(eachline):
                pvc_list.append(eachline.split()[0])
        for eachpvc in pvc_list:
            cmdline = "kubectl --kubeconfig {} delete machines {} ".format(usercluster.clustercfgfile,eachpvc)
            testreportlog.info2file(cmdline)
            try:
                (retcode, retoutput) = RunCmd(cmdline, 15, None, wait=2, counter=3)
            except:
                print "fail to run cmd {}".format(cmdline)
                return False

            print retcode, retoutput
            countdown(45)
            if retcode == 1:
                return False
    else:
        return False

    cmdline = "kubectl --kubeconfig {} delete cluster {} -n {}".format(admincluster.clustercfgfile, usercluster.clustername, usercluster.clustername)
    testreportlog.info2file(cmdline)

    try:
        (retcode, retoutput) = RunCmd(cmdline, 300, None, wait=2, counter=3)
    except:
        print "fail to run cmd {}".format(cmdline)
        return False


    countdown(45)
    if retcode == 1:
       return False

    #kubectl delete machines
    #kubectl --kubeconfig [ADMIN_CLUSTER_KUBECONFIG] get cluster --all-namespaces
    #kubectl --kubeconfig [ADMIN_CLUSTER_KUBECONFIG] delete machinedeployments -l kubernetes.googleapis.com/cluster-name=
    cmdline = "kubectl --kubeconfig {} delete machinedeployments -l kubernetes.googleapis.com/cluster-name={}".format(admincluster.clustercfgfile, usercluster.clustername)
    testreportlog.info2file(cmdline)
    try:
        (retcode, retoutput) = RunCmd(cmdline, 15, None, wait=2, counter=3)
    except:
        print "fail to run cmd {}".format(cmdline)
        return False

    countdown(45)

    if retcode == 1:
       return False 

    #### verfiy user cluster is deleted
    #1. kubectl get pvc -n kube-system
    #2. kubectl get statefulsets -n kube-system
    #3. kubectl get configmap --all-namespaces --selector=f5type=virtual-server -o jsonpath='{.items[*].metadata.annotations.status\.virtual-server\.f5\.com/ip}'
    #4. kubectl get namespaces
    #5. kubectl --kubeconfig [ADMIN_CLUSTER_KUBECONFIG] get machines -l kubernetes.googleapis.com/cluster-name=<name>

    print retoutput
    if retcode == 0:
        test_result = "PASS"

    testreportlog.info2file("Test_Case: {}: {}: {}".format(test_name, test_result, test_detail))
    test_results.append([test_name, test_result, test_detail])
    if test_result == "FAIL" and abortonfailure:
        test_abort(testreportlog)

    return test_result == "PASS"

def test_workflow_state(usercluster, testreportlog, workloadns, expected_state, expected_number, service_type):
    lbsvcip = test_cfg['lbsvcip']
    abortonfailure = test_cfg['abortonfailure']

    total_request = test_cfg['totalreq']
    concurrent_session = test_cfg['concurrent']
    expected_duration = test_cfg['maxtime']

    usercluster.get_all_for_namespace(workloadns)
    if test_workload_deployed(usercluster, workloadns, testreportlog):
        test_workload_pod_state(usercluster, workloadns, expected_state, testreportlog)
        test_workload_number_of_pods(usercluster, workloadns, expected_number, testreportlog)
        if test_workload_service_state(usercluster, workloadns, service_type, testreportlog):
            if test_workload_accessible_via_lbsvcip(usercluster, workloadns, testreportlog):
                test_service_traffic(usercluster, concurrent_session, total_request, expected_duration, testreportlog)
        test_workload_deployment_state(usercluster, workloadns, expected_number, testreportlog)
        test_workload_replica_state(usercluster, workloadns, expected_number, testreportlog)


def user_cluster_test(usercluster, testreportlog):
    lbsvcip = test_cfg['lbsvcip']
    abortonfailure = test_cfg['abortonfailure']
    lightmode = test_cfg['lightmode']
    expected_state = "Running"


    workloadyaml = '{}.nginx.yaml'.format(usercluster.clustername)
    patchnodeyaml = "{}.patch.node.yaml".format(usercluster.clustername)

    if not usercluster.check_cluster_server_connectivity():
        testreportlog.info2file('User Cluster Server IP {} is not reachable! Test Aborted'.format(usercluster.serverip))
        print('User Cluster Server IP {} is not reachable! Test Aborted'.format(admincluster.serverip))
        sys.exit()
    else:
        testreportlog.info2file(usercluster.description())
        testreportlog.info2file(usercluster.dump_cluster_all())

    if not lightmode:
        test_machinedeployment_update(usercluster, patchnodeyaml, testreportlog) 

    print workloadyaml, patchnodeyaml
    test_workload_deployment(usercluster, testreportlog, workloadyaml)
    workloadns, expected_number, deployment_name, service_type = get_info_from_workload_yaml_file(workloadyaml)
    countdown(5)
    test_workflow_state(usercluster, testreportlog, workloadns, expected_state, expected_number, service_type)

    new_replica = expected_number*2
    usercluster.workload_replica_modify(deployment_name, workloadns, new_replica)
    countdown(5)
    test_workflow_state(usercluster, testreportlog, workloadns, expected_state, new_replica, service_type)

    test_workload_withdraw(usercluster, workloadyaml, testreportlog)
    countdown(1)
    usercluster.get_all_for_namespace(workloadns)

    test_workload_deleted(usercluster, workloadns)


def prepare_logging():
    currentDT = datetime.datetime.now()
    timestamp = time.strftime("%Y-%m-%d-%H-%M")
    anthosreportlog = '{}.{}.T{}.log'.format(test_cfg['anthostestlog'], test_cfg['partner'], timestamp)
    testreportlog = testlog(anthosreportlog, 7)
    testreportlog.info2file("Test Script (gke_onprem_test.py) Version: {}\n".format(VERSION))
    testreportlog.info2file("Anthos-Ready Platform Test for Partner {} starting at {}.\n\n".format(test_cfg['partner'], timestamp))
    return anthosreportlog, testreportlog


def create_user_cluster(admincluster, testreportlog):
    clustercfgpath = test_cfg['clustercfgpath']
    admincfg = admincluster.clustercfgfile
    clusteryaml = test_cfg['clusteryaml']
    clustername = test_cfg['userclustername']
    bigippartition = test_cfg['userpartition']
    controlplanevip = test_cfg['controlplanevip']
    ingressvip = test_cfg['ingressvip']
    ipblock = test_cfg['staticipblock']
    abortonfailure = test_cfg['abortonfailure']


    test_result = "FAIL"
    test_detail = "Verify a new user cluster {} can be created in admin cluster defined by {}.".format(clustername, admincfg)
    test_name = "test_new_user_cluster_creation"


    testreportlog.info2file("Start creating a new user cluster:")
    testreportlog.info2file("      cluster create yaml file: {}".format(clusteryaml))
    testreportlog.info2file("      cluster name: {}".format(clustername))
    testreportlog.info2file("      partition for user cluster: {}".format(bigippartition))
    testreportlog.info2file("      controlplanevip: {}".format(controlplanevip))
    testreportlog.info2file("      ingressvip: {}".format(ingressvip))

    sourceyamlfile = "{}/{}".format(clustercfgpath, clusteryaml)
    print "sourceyamlfile : ", sourceyamlfile
    newuseryaml = generate_user_cluster_create_yaml(sourceyamlfile, clustername, bigippartition, controlplanevip, ingressvip, ipblock)
    if not newuseryaml and abortonfailure:
        test_abort(testreportlog)

    cmdline = "gkectl create cluster --config {} --kubeconfig {} --kubeconfig-out {}/{}-kubeconfig -v 7".format(newuseryaml, admincfg, clustercfgpath, clustername)
    testreportlog.info2file(cmdline)
    try:
        (retcode, cmdoutput) = RunCmd(cmdline, 3000, None, wait=2, counter=3)
    except Exception as e:
        testreportlog.info2file("Run cmd {} fails.".format(cmdline))
    if retcode == 0:
        test_result="PASS"
    else:
        test_result="FAIL"

    if not test_result == "PASS":
        countdown(10)
        usercfg = "{}/{}-kubeconfig".format(clustercfgpath, clustername)
        if gkectl_diagnose_cluster(usercfg, testreportlog):
            test_result="PASS"
    

    testreportlog.info2file("Test_Case: {}: {}: {}".format(test_name, test_result, test_detail))
    test_results.append([test_name, test_result, test_detail])
    if test_result == "FAIL" and abortonfailure:
        test_abort(testreportlog, admincluster)
    return test_result == "PASS"         


def wait_for_gkectl_done():
    timeout = 6000
    count = 0 
    cmdline = "ps -eaf|grep gkectl"
    gkectl_done = False
    while count < timeout and not gkectl_done:
        time.sleep(5)
        print "wait......"
        (retcode, retOutput) = RunCmd(cmdline, 15, None, wait=2, counter=3)
        if not "gkectl create cluster" in retOutput:
            gkectl_done = True
        count += 5    


def get_cluster_list(testreportlog):
    clustercfgpath = test_cfg['clustercfgpath']
    if test_cfg['usercfg']:
        userclustercfgs = test_cfg['usercfg'].split(',')
    else:
        userclustercfgs = []


    adminclustercfgfile = '{}/{}'.format(test_cfg['clustercfgpath'], test_cfg['admcfg'])
    admincluster = gkeonpremcluster(adminclustercfgfile, True, testreportlog) 
    usercluster_list = []

    if test_cfg['newusercluster']:
        testreportlog.info2file('Script is going to create a new user cluster.')
        testreportlog.info2file('Please make sure vcenter and load balancer are ready for new user cluster creation, If not you can press Ctl+C to abort.')
        if create_user_cluster(admincluster, testreportlog):
            newusercfg = "{}-kubeconfig".format(test_cfg['userclustername'])
            userclustercfgs.append(newusercfg)
        else:
            print "Fail to create user cluster"


    for userclustercfg in userclustercfgs:
        userclustercfgfile = '{}/{}'.format(clustercfgpath, userclustercfg)
        usercluster = gkeonpremcluster(userclustercfgfile, False, testreportlog)
        usercluster_list.append(usercluster)
    

    return admincluster, usercluster_list


def user_cluster_tests(userclusterlist, testreportlog):
    # User Cluster Sanity Check
    testloop = test_cfg['testloop']
    lbsvcip = test_cfg['lbsvcip']
    lightmode = test_cfg['lightmode']
    abortonfailure = test_cfg['abortonfailure']

    for i in range(testloop, 0, -1):
        print "Start Testing Loop {}".format(i)
        for usercluster in userclusterlist:
            print "Start testing for cluster defined by {}".format(usercluster.clustercfgfile)
            if usercluster.reachable:
                user_cluster_test(usercluster, testreportlog)
            elif abortonfailure:
                test_abort(testreportlog, usercluster)
            countdown(1)
        countdown(1)


def upload_testlog_to_bucket(logfile, testreportlog):
    print "Uploading file to gcs bucket"
    gcsbucket = test_cfg['gcsbucket']
    serviceacct = test_cfg['serviceacct']
    if gcsbucket:
        if serviceacct:
            if gcp_auth(serviceacct) == 1:
                print "Fail to activate GCP service acct {}.".format(serviceacct)
        (retcode, retmsg) = upload_testlog(logfile, gcsbucket)
        if retcode:
            testreportlog.info2file("Test log {} is uploaded to GCS bucket {}.".format(logfile, gcsbucket))
        else:
            testreportlog.info2file("Test log {} fails to be uploaded to GCS bucket {}.".format(logfile, gcsbucket))
            testreportlog.info2file("Please upload test log {} manually.".format(logfile))
    return


def cleanup_all_userclusters(userclusters):
    for usercluster in userclusters:
        if usercluster.reachable:
            workloadyaml = "{}.nginx.yaml".format(usercluster.clustername)
            cluster_cleanup(usercluster, workloadyaml)


def gkectl_diag_all_clusters(admincluster, userclusters, testreportlog):
    if not test_cfg['lightmode']:
        if admincluster.reachable:
            test_cluster_sanity(admincluster, testreportlog)
        for usercluster in userclusters:
            if usercluster.reachable:
                test_cluster_sanity(usercluster, testreportlog)


def get_platform_detail(cfgfile):
    platform_info_list = []
    try:
        with open(cfgfile, 'r') as reader:
            platform_info_list = reader.readlines()
            #print platform_info_list
    except:
        print 'Oops: open file {} for read fails.\n'.format(cfgfile)
    return platform_info_list

def get_all_user_cluster(adminclustercfg):
    clusterlist = []
    namespacelist = []
    usercluster = ""
    cmdline = "kubectl --kubeconfig {} get cluster --all-namespaces".format(adminclustercfg)
    (retcode, retOutput) = RunCmd(cmdline, 15, None, wait=2, counter=3)
#    ubuntu@admin-ga-3:~/anthos_ready$ kubectl --kubeconfig kubecfg/kubeconfig get cluster --all-namespaces
#NAMESPACE      NAME              AGE
#cpe-user-3-1   cpe-user-3-1      4d
#cpe-user-3-2   cpe-user-3-2      4d
#cpe-user-3-3   cpe-user-3-3      4d
#cpe-user-3-4   cpe-user-3-4      4d
#default        gke-admin-qw9jb   4d
    
    print retOutput
    for eachline in retOutput.splitlines():
        search_pattern = re.compile(r"([a-zA-Z0-9\-]+)\s+([a-zA-Z0-9\-]+)\s+\d+[d|m|s]")
        print search_pattern.search(eachline)
        if search_pattern.search(eachline):
            search_result = search_pattern.search(eachline)
            print search_result
            if not "default" in search_result.group():
                print search_result.group()
                print "ddd"
                print search_result.group(1), search_result.group(2)
                print "dddd", search_result.group(0)
                clusterlist.append(search_result.group(2))
                namespacelist.append(search_result.group(1))
                usercluster = "{},{}-kubeconfig".format(usercluster, clusterlist.append(search_result.group(2)))
    print clusterlist, namespacelist            
            

def de_register_user_cluster_from_hub(usercluster, testreportlog):
    #kubectl config current-context.
    cmdline = "kubectl --kubeconfig {} config current-context".format(usercluster.clustercfgfile)
    testreportlog.info2file(cmdline)
    (retcode, retOutput) = RunCmd(cmdline, 60, None, wait=2, counter=3)
    # ubuntu@admin-ga:~/anthos_ready$ kubectl --kubeconfig kubecfg/cpe-user-1-2-kubeconfig config current-context
    # cluster
    search_pattern = re.compile(r"([a-zA-Z0-9\-]+)")
    if search_pattern.search(retOutput):
        connectcontext = search_pattern.search(retOutput).group(1)
    else:
        connectcontext = 'cluster'

    cmdline = "gcloud alpha container hub unregister-cluster --kubeconfig-file={} --context={}".format(usercluster.clustercfgfile, connectcontext)
    testreportlog.info2file(cmdline)
    (retcode, retOutput) = RunCmd(cmdline, 60, None, wait=2, counter=3)

    return retcode


def generate_user_cluster_create_yaml(source_create_yaml_file, clustername, partition, controlplanevip, ingressvip, ipblock):
    clustercfgpath = test_cfg['clustercfgpath']
    user_cluster_create_yaml = "{}/{}_create.yaml".format(clustercfgpath, clustername)

    print source_create_yaml_file, clustername, partition, controlplanevip, ingressvip, ipblock
    with open(source_create_yaml_file) as f:
        output = f.read()

    admin_index = output.find('admincluster:')
    user_index = output.find('usercluster:')
    if not admin_index == -1 and not user_index == -1:
        first_part = output[:admin_index-1]
        user_part = output[user_index:]
    patterncontrol = re.compile(r'controlplanevip: \"([\d.]*)\"')
    if patterncontrol.search(user_part):
        control_ip = patterncontrol.search(user_part).group(1)
    else:
        print "Fail to create create yaml file for new user cluster"
        return False 
    user_part = user_part.replace(control_ip, controlplanevip)
    patterningress = re.compile(r'ingressvip: \"([\d.]*)\"')
    if patterningress.search(user_part):
        ingress_ip = patterningress.search(user_part).group(1)
    else:
        print "Fail to create create yaml file for new user cluster"
        return False
    user_part = user_part.replace(ingress_ip, ingressvip)

    patternname = re.compile(r'clustername:\s\"([A-Za-z0-9\-]+)\"')
    if patternname.search(user_part):
        oldclustername = patternname.search(user_part).group(1)
        user_part = user_part.replace(oldclustername, clustername)
    else:
        print "Fail to create create yaml file for new user cluster"
        return False

    patternipblock = re.compile(r'ipblockfilepath:\s*\"(.+)\"')
    old_ip_block = ""
    if patternipblock.search(user_part):
        old_ip_block = patternipblock.search(user_part).group(1)
    if ipblock:
        if len(old_ip_block) > 4:
            user_part = user_part.replace(old_ip_block, "{}/{}".format(clustercfgpath, ipblock))
        else:
            print "The reference yaml file {} does not use static ip.".format(source_create_yaml_file)
            print "To use static ip for user cluster, the original admin/user cluster needs to use static ip too.".format(source_create_yaml_file)
            return False
    else:
        if len(old_ip_block) > 4:
            patternipblock = re.compile(r'ipblockfilepath:\s*\"(.+)\"')
            user_part = user_part.replace('ipblockfilepath:','# ipblockfilepath:')
            print "Note: The reference yaml file {} use static ip.".format(source_create_yaml_file)
            print "User cluster is going to use DHCP.".format(source_create_yaml_file)

    with open(user_cluster_create_yaml, 'w') as f:
         f.write(first_part)
         f.write("\n\n")
         f.write(user_part)
    return user_cluster_create_yaml 


def generate_test_summary(testreportlog, admincluster, usercluster):
    platformcfgfile = test_cfg['platformcfgfile']
    partner = test_cfg['partner']
    passed_tests = 0
    failed_tests = 0
    gkectl_ver = get_gkectl_version()
    for each in test_results:
        if each[1] == "PASS":
            passed_tests += 1
        else:
            failed_tests += 1

    padding = '=' * 175
    testreportlog.info2file('\n\n')
    testreportlog.changeformat()
    if not "unknown" in platformcfgfile:
        platform_info_list = get_platform_detail(platformcfgfile)
    else:
        platform_info_list = []
    testreportlog.info2file("Summary:")
    testreportlog.info2file("    gkectl version: {}, gke_onprem_test version: {}".format(gkectl_ver, VERSION))
    testreportlog.info2file("    partner: {}, platform detail: {}".format(partner, platformcfgfile))
    if len(platform_info_list) > 0:
        for eachline in platform_info_list:
            testreportlog.info2file("      {}".format(eachline.strip()))

    testreportlog.info2file("    Traffic Test Profile: Total Requests: {}".format(test_cfg['totalreq']))
    testreportlog.info2file("                          Concurrent Sessions: {}".format(test_cfg['concurrent']))
    testreportlog.info2file("                          Expect to finish all requests in {} seconds".format(test_cfg['maxtime']))

    if usercluster and admincluster:
        testreportlog.info2file("    admin cluster version: {}, user cluster version: {}".format(admincluster.control_version, usercluster.control_version))
    elif admincluster and not usercluster:
        testreportlog.info2file("    admin cluster version: {}".format(admincluster.control_version))
    elif not admincluster and usercluster:
        testreportlog.info2file("    user cluster version: {}".format(usercluster.control_version))

    testreportlog.info2file("    Total Tests: {}, Passed Tests: {}, Failed Tests: {}".format(len(test_results), passed_tests, failed_tests))
    testreportlog.info2file(padding)
    for eachcase in test_results:
        testreportlog.info2file("      {}".format(':'.join(eachcase)))
    testreportlog.info2file(padding)


def testargparser():
    parser = argparse.ArgumentParser()
    parser.add_argument('-cfgpath', '--clustercfgpath', dest='clustercfgpath', help='Absolute Path for cluster kubeconfig files', type=str, required=True, default=None)
    parser.add_argument('-admin', '--adminclustercfg', dest='admcfg', type=str, help='Admin Cluster kubeconfig file', default=None)
    parser.add_argument('-user', '--userclustercfg', dest='usercfg', type=str, help='User Cluster kubeconfig files', default=None)
    parser.add_argument('-lbip', '--lbsvcip', dest='lbsvcip', help='IP Address for Load-balancer for service to be deployed', type=str, required=True, default=None)
    parser.add_argument('-testlog', '--testlog', dest='anthostestlog', help='Prefix for test log file', type=str, default='gkeonprem.test')
    parser.add_argument('-gcs', '--gcsbucket', dest='gcsbucket', help='GCS bucket where file is to be uploaded to', type=str, default=None)
    parser.add_argument('-serviceacct', '--serviceacct', dest='serviceacct', help='Google Cloud service account', type=str, default=None)
    parser.add_argument('-loop', '--testloop', dest='testloop', help='number of loops test cases to be run', type=int, default=1)
    parser.add_argument('-abort', '--abortonfailure', dest='abortonfailure', help='flag to set whether to abort test if failure occures', action='store_true', default=False)
    parser.add_argument('-partner', '--partner', dest='partner', type=str, help='Anthos Partner', default='unknown')
    parser.add_argument('-platformcfg', '--platformcfgfile', dest='platformcfgfile', help='Partner provided file for platform detail information', type=str, default='unknown')
    parser.add_argument('-lightmode', '--lightmode', dest='lightmode', action='store_true', help=argparse.SUPPRESS,  default=False)
    parser.add_argument('-request', '--totalrequest', dest='totalreq', type=int, help='Total Requests sent to the deployed service', default=10000)
    parser.add_argument('-concurrent', '--concurrent', dest='concurrent', type=int, help='Number of concurrent sessions initialized with the deployed service', default=100)
    parser.add_argument('-maxtime', '--maxtime', dest='maxtime', type=int, help='Maximum time required to finish all requests in second', default=5)

    parser.add_argument('-createcluster', '--createcluster', dest='createcluster',  action='store_true', help="Flag whether to create a new GKE On-Prem cluster or not, by default flag is set as False ", default=False)
    parser.add_argument('-createyamlfile', '--createyamlfile', dest='createyamlfile',type=str,  help="yaml file to create a new gke on-prem cluster including admin and user cluster", default=None)

    parser.add_argument('-newusercluster', '--newusercluster', dest='newusercluster',  action='store_true', help='flag whether to create a new user cluster or not, by default, flag is set to False', default=False)
    parser.add_argument('-clusteryaml', '--clusteryaml', dest='clusteryaml', type=str, help='Yaml file used to create the admin and the first user cluster', default='cpe-user-3.yaml')
    parser.add_argument('-userclustername', '--userclustername', dest='userclustername', type=str, help='New User Cluster name', default='cpe-user-3-2')
    parser.add_argument('-userpartition', '--userpartition', dest='userpartition', type=str, help='Partition on BIGIP', default='cpe-user-3-2')
    parser.add_argument('-controlplanevip', '--controlplanevip', dest='controlplanevip', type=str, help='Control Plan VIP for new cluster', default='100.115.253.93')
    parser.add_argument('-staticipblock', '--staticipblock', dest='staticipblock', type=str, help='Yaml file for static ip block', default=None)
    parser.add_argument('-ingressvip', '--ingressvip', dest='ingressvip', type=str, help='Ingress VIP for new cluster', default='100.115.253.94')

    parser.add_argument('-deleteusercluster', '--deleteusercluster', dest='deleteusercluster', help='flag to set whether to delete user cluser', action='store_true', default=False)
    parser.add_argument('-skipimageprepare', '--skipimageprepare', dest='skipimageprepare', help='flag to skip image preparation', action='store_true', default=False)
    parser.add_argument('-saveusercluster', '--saveusercluster', dest='saveusercluster', help='flag to save user cluster created by the script', action='store_true', default=False)



    args = parser.parse_args()

    test_cfg['clustercfgpath'] = args.clustercfgpath
    test_cfg['admcfg'] = args.admcfg
    test_cfg['usercfg'] = args.usercfg
    test_cfg['lbsvcip'] = args.lbsvcip
    test_cfg['anthostestlog'] = args.anthostestlog
    test_cfg['gcsbucket'] = args.gcsbucket
    test_cfg['serviceacct'] = args.serviceacct
    test_cfg['testloop'] = args.testloop
    test_cfg['abortonfailure'] = args.abortonfailure
    test_cfg['lightmode'] = args.lightmode
    test_cfg['platformcfgfile'] = args.platformcfgfile
    test_cfg['partner'] = args.partner
    test_cfg['totalreq'] = args.totalreq
    test_cfg['concurrent'] = args.concurrent
    test_cfg['maxtime'] = args.maxtime
    test_cfg['createcluster'] = args.createcluster
    test_cfg['createyamlfile'] = args.createyamlfile
    test_cfg['newusercluster'] = args.newusercluster
    if test_cfg['createcluster']:
        test_cfg['clusteryaml'] = test_cfg['createyamlfile']
    else:
        test_cfg['clusteryaml'] = args.clusteryaml
    test_cfg['userclustername'] = args.userclustername
    test_cfg['userpartition'] = args.userpartition
    test_cfg['controlplanevip'] = args.controlplanevip
    test_cfg['staticipblock'] = args.staticipblock
    test_cfg['ingressvip'] = args.ingressvip
    test_cfg['deleteusercluster'] = args.deleteusercluster
    test_cfg['skipimageprepare'] = args.skipimageprepare
    test_cfg['saveusercluster'] = args.saveusercluster



#runInParallel(func1, func2, func3)
#time.sleep(10000)

####### Starts
# variables
workloadyaml = 'nginx.yaml'
patchnodeyaml = "patch.node.yaml"

# Test Arg Parser
testargparser()

# install package if needed
env_prepare()

# pre-check
env_check()

# set output to stdout
send_log_to_stdout()

# prepare logging file
anthosreportlog, testreportlog = prepare_logging()

if test_cfg['createcluster']:
    testreportlog.info2file('Script is going to create a new GKE OnPrem Cluster defined by yaml file {} specified by argument "-createyamlfile"'.format(test_cfg['createyamlfile']))
    testreportlog.info2file('Please make sure vcenter and F5 are ready for gke onprem cluster creation, If not you can press Ctl+C to abort.')
    testreportlog.info2file('If the GKE OnPrem image is already uploaded to vcenter please use "-skipimageprepare" to skip "gkectl prepare".')
    testreportlog.info2file('Please make sure vmdk specified in cluster creation ymal file is deleted. If the specified file is under directory please make sure the directory is created.')
    countdown(20)
    test_create_gke_on_prem_cluster(testreportlog)

# create admin cluster object and user cluster objects
admincluster, userclusters = get_cluster_list(testreportlog)

# delete user cluster
if test_cfg['deleteusercluster']:
    for usercluster in userclusters:
        delete_user_cluster(admincluster, usercluster, testreportlog)
    generate_test_summary(testreportlog, admincluster, userclusters[0])
    sys.exit(0)


# gkectl diag test
gkectl_diag_all_clusters(admincluster, userclusters, testreportlog)

# Cluster Test
user_cluster_tests(userclusters, testreportlog)

# gkectl diag test
gkectl_diag_all_clusters(admincluster, userclusters, testreportlog)

# Cleanup user cluster
cleanup_all_userclusters(userclusters)

# Cleanup the newly created user cluster by default.
# adding -saveusercluster if user cluster needs to be kept.
if not test_cfg['saveusercluster']:
    for usercluster in userclusters:
        if usercluster.clustername == test_cfg['userclustername']:
            delete_user_cluster(admincluster, usercluster, testreportlog)
            continue 

# Cleanup yaml file generated during test
delete_yaml_files(userclusters)

# upload test log to gcs bucket
upload_testlog_to_bucket(anthosreportlog, testreportlog)

# generate test summary
generate_test_summary(testreportlog, admincluster, userclusters[0])
