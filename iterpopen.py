# Copyright 2008 Google Inc. All Rights Reserved.
r"""An extended subprocess.Popen class that is iterable and supports timeouts.

This is a drop-in replacement for subprocess.Popen that is iterable, supports
timeouts and using ptys for IO, and is slightly easier to use with more useful
default values for initialisation arguments. It also fixes a race condition in
Python 2.4's popen implementations (also fixed in today's Python releases).

The standard library subprocess module (in Python 2.7 and later) fixes many of
the thread safety issues that are found in this module.  However, it does not
support many of the extensions implemented here, such as PTY, iter() and RunCmd.

Example:

  for line in Popen('sort | tail', input=data, timeout=1.0):
    ...

This example passes 'data' through sort and tail, and then processes the last
few lines of sorted data line by line.  If the sort operation takes longer
than 1 second, this will abort and raise TimeoutError.

Example:

  output = ''
  for data in Popen('interact.sh', bufsize=0, stdout=PTY):
    sys.stdout.write(data)
    output += data

This example executes an interactive command using unbuffered I/O via a pty.
This allows a human to directly interact with 'interact.sh' while capturing
all output for later processing.  Using bufsize=0 means it will display
prompts that do not end with '\n' immediately. Using stdout=PTY will use a
pseudo-terminal for stdout, which is sometimes required to convince commands
to correctly flush their interactive prompt output.

There is also a RunCmd() helper function that makes it easy to run a command
and collect stdout and/or stdin, and will raise an exception if the returncode
is non-zero.

Example:

  stdout = RunCmd('sort', input=indata, stdout=PIPE)
  stderr = RunCmd(['/path/mycmd', '--flag=10'], stderr=PIPE)
  stdout, stderr = RunCmd('/path/doit --fast', stdout=PTY, stderr=PIPE)

The main use-case that prompted this module is executing interactive commands
and capturing their output. You want the output to be displayed to the user
immediately so that they can see prompts they must respond to, but you also
want your program to record and/or process them. An example would be running
'g4 submit' with interactive prompting and then parsing the output to find out
what the new CL number is.

Other attempts to solve this failed because they used line-buffering, and
interactive commands typically prompt without a '\n' terminated line and pause
for input. This means you don't see the prompt, and it blocks waiting for user
input.

This solution addresses that main use-case, but also is useful for many
others. Most simplistic solutions for interacting with subprocesses are broken
and the implementer doesn't know it. It's very easy to end up with situations
like subprocesses blocked waiting for input or trying to write to stderr while
the program is blocked trying to read stdout. Have a look at subprocess.py's
implementation of communicate() for an example of how ugly it can get. This
solution provides a simple API to deal safely with subprocesses that gives
more flexibility and control than communicate(), with the same robustness.

WARNING: There is a known issue with using PTYs. They raise OSError for
os.read() or IOError for file.read() at EOF. These methods normally return an
empty string at EOF for files/pipes/sockets/etc. This will have an effect when
reading the stdout/stderr file attributes and using PTYs. In particular the
file.readline() method will raise an exception and throw away any final
non-line terminated characters. This does not happen when iterating with
bufsize=1 and stdout=PTY or stderr=PTY, as the iterator correctly handles this.
"""

__author__ = 'abo@google.com (Donovan Baarda)'

import errno
import os
import pty
import select
import signal
import subprocess
import threading
import tty

import six

# This module uses upstream Popen naming conventions.
# pylint: disable=g-bad-name

# define convenient aliases for subprocess constants
# Note subprocess.PIPE == -1, subprocess.STDOUT = -2
PIPE = subprocess.PIPE
STDOUT = subprocess.STDOUT
PTY = -3


class Error(Exception):
  """Exception when Popen suprocesses fail."""


class TimeoutError(Error):
  """Exception when Popen suprocesses time out."""


# TODO(abo): remove this deprecated alias and update all uses.
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


def call(*args, **kwargs):  # pylint: disable=g-doc-args
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


def RunCmd(args, **kwargs):  # pylint: disable=g-doc-args
  """Run a command using iterpopen and return its output.

  Runs a command and returns its stdout and/or stderr depending on the stdout
  and stderr arguments provided, and raises an exception if the commands
  returncode is non-zero.

  Args:
    See subprocess.Popen and iterpopen.Popen.

  Returns:
    (stdout, stderr) if both stdout and stderr arguments were PIPE or PTY.
    stdout if only the stdout argument was PIPE or PTY.
    stderr if only the stderr argument was PIPE or PTY.
    None if both stdout and stderr were not PIPE or PTY.

  Raises:
    ReturncodeError: if the command returncode was not 0.
  """
  # Make the default stdout=None, not PIPE as for iterpopen.Popen.
  kwargs.setdefault('stdout', None)
  proc = Popen(args, **kwargs)
  stdout, stderr = proc.communicate()
  if proc.returncode:
    raise ReturncodeError(proc.returncode, args)
  if stderr is None:
    return stdout
  if stdout is None:
    return stderr
  return stdout, stderr


class Popen(subprocess.Popen):
  """An extended Popen class that is iterable.

  It behaves exactly the same as subprocess.Popen except some initialisation
  arguments have different defaults, supports using pty's for IO, has
  additional input and timeout arguments, and it is iterable. When iterated it
  will yield stdout, stderr, or (stdout, stderr) tuples based on what was
  initialised as PIPE or PTY. The bufsize affects how much data is processed
  each iteration, where values < 0 mean 'as much as possible', 0 means
  'unbuffered', 1 means whole lines, and positive values mean buffers of that
  size. When bufsize is >=1, IO will buffer to guarantee whole lines or blocks
  per iteration.

  Use bufsize=0 for interactive commands, bufsize=1 to iterate by lines,
  bufsize>1 to iterate by blocks, and bufsize=-1 for performance.

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
          # TODO(abo): Check that this always means poll timed out.
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
    # TODO(csimmons): make self.wait() support a timeout and use it.
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
    if not self.__race_lock.acquire(blocking=False):
      return self.returncode
    try:
      return super(Popen, self).poll(*args, **kwargs)
    finally:
      self.__race_lock.release()

  def wait(self, *args, **kwargs):
    """Work around a known race condition in subprocess fixed in Python 2.5."""
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
