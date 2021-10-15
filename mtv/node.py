"""
Node objects for Mininet.

Nodes provide a simple abstraction for interacting with hosts, switches
and controllers. Local nodes are simply one or more processes on the local
machine.

Node: superclass for all (primarily local) network nodes.

Host: a virtual host. By default, a host is simply a shell; commands
    may be sent using Cmd (which waits for output), or using sendCmd(),
    which returns immediately, allowing subsequent monitoring using
    monitor(). Examples of how to run experiments using this
    functionality are provided in the examples/ directory. By default,
    hosts share the root file system, but they may also specify private
    directories.

CPULimitedHost: a virtual host whose CPU bandwidth is limited by
    RT or CFS bandwidth limiting.

Switch: superclass for switch nodes.

UserSwitch: a switch using the user-space switch from the OpenFlow
    reference implementation.

OVSSwitch: a switch using the Open vSwitch OpenFlow-compatible switch
    implementation (openvswitch.org).

OVSBridge: an Ethernet bridge implemented using Open vSwitch.
    Supports STP.

IVSSwitch: OpenFlow switch using the Indigo Virtual Switch.

Controller: superclass for OpenFlow controllers. The default controller
    is controller(8) from the reference implementation.

OVSController: The test controller from Open vSwitch.

NOXController: a controller node using NOX (noxrepo.org).

Ryu: The Ryu controller (https://osrg.github.io/ryu/)

RemoteController: a remote controller node, which may use any
    arbitrary OpenFlow-compatible controller, and which is not
    created or managed by Mininet.

Future enhancements:

- Possibly make Node, Switch and Controller more abstract so that
  they can be used for both local and remote nodes

- Create proxy objects for remote nodes (Mininet: Cluster Edition)
"""

import io
import inspect
import os
import sys
import pty
import re
import signal
import select
import libvirt
import socket
import docker
import subprocess
import ipaddress
import tempfile
import shutil
import atexit
import xml.etree.ElementTree as ET
from distutils.version import StrictVersion
from subprocess import Popen, PIPE
from subprocess import run as sp_run
from time import sleep
from itertools import count

from mtv.log import info, error, warn, debug
from mtv.util import (quietRun, errRun, errFail, moveIntf, isShellBuiltin,
                      numCores, retry, mountCgroups, BaseString, decode,
                      encode, getincrementaldecoder, Python3, which)
from mtv.moduledeps import moduleDeps, pathCheck, TUN
from mtv.link import Link, Intf, TCIntf, OVSIntf


# pylint: disable=too-many-arguments


class Node(object):
    """A virtual network node is simply a shell in a network namespace.
       We communicate with it using pipes."""

    portBase = 0  # Nodes always start with eth0/port0, even in OF 1.0

    def __init__(self, name, inNamespace=True, **params):
        """name: name of node
           inNamespace: in network namespace?
           privateDirs: list of private directory strings or tuples
           params: Node parameters (see config() for details)"""

        # Make sure class actually works
        self.checkSetup()

        self.name = params.get('name', name)
        self.privateDirs = params.get('privateDirs', [])
        self.inNamespace = params.get('inNamespace', inNamespace)

        # Python 3 complains if we don't wait for shell exit
        self.waitExited = params.get('waitExited', Python3)

        # Stash configuration parameters for future reference
        self.params = params

        # dict of port numbers to interfacse
        self.intfs = {}

        # dict of interfaces to port numbers
        # todo: replace with Port objects, eventually ?
        self.ports = {}

        self.nameToIntf = {}  # dict of interface names to Intfs

        # Make pylint happy
        (self.shell, self.execed, self.pid, self.stdin, self.stdout,
            self.lastPid, self.lastCmd, self.pollOut) = (
                None, None, None, None, None, None, None, None)
        self.waiting = False
        self.readbuf = ''

        # Incremental decoder for buffered reading
        self.decoder = getincrementaldecoder()

        # Start command interpreter shell
        self.master, self.slave = None, None  # pylint
        self.startShell()
        self.mountPrivateDirs()

    # File descriptor to node mapping support
    # Class variables and methods

    inToNode = {}  # mapping of input fds to nodes
    outToNode = {}  # mapping of output fds to nodes

    @classmethod
    def fdToNode(cls, fd):
        """Return node corresponding to given file descriptor.
           fd: file descriptor
           returns: node"""
        node = cls.outToNode.get(fd)
        return node or cls.inToNode.get(fd)

    # Command support via shell process in namespace
    def startShell(self, mnopts=None):
        "Start a shell process for running commands"
        if self.shell:
            error("%s: shell is already running\n" % self.name)
            return
        # mnexec: (c)lose descriptors, (d)etach from tty,
        # (p)rint pid, and run in (n)amespace
        opts = '-cd' if mnopts is None else mnopts
        if self.inNamespace:
            opts += 'n'
        # bash -i: force interactive
        # -s: pass $* to shell, and make process easy to find in ps
        # prompt is set to sentinel chr( 127 )
        cmd = ['mnexec', opts, 'env', 'PS1=' + chr(127),
               'bash', '--norc', '--noediting',
               '-is', 'mtv:' + self.name]

        # Spawn a shell subprocess in a pseudo-tty, to disable buffering
        # in the subprocess and insulate it from signals (e.g. SIGINT)
        # received by the parent
        self.master, self.slave = pty.openpty()
        self.shell = self._popen(cmd, stdin=self.slave, stdout=self.slave,
                                 stderr=self.slave, close_fds=False)
        # XXX BL: This doesn't seem right, and we should also probably
        # close our files when we exit...
        self.stdin = os.fdopen(self.master, 'r')
        self.stdout = self.stdin
        self.pid = self.shell.pid
        self.pollOut = select.poll()
        self.pollOut.register(self.stdout)
        # Maintain mapping between file descriptors and nodes
        # This is useful for monitoring multiple nodes
        # using select.poll()
        self.outToNode[self.stdout.fileno()] = self
        self.inToNode[self.stdin.fileno()] = self
        self.execed = False
        self.lastCmd = None
        self.lastPid = None
        self.readbuf = ''
        # Wait for prompt
        while True:
            data = self.read(1024)
            if data[-1] == chr(127):
                break
            self.pollOut.poll()
        self.waiting = False
        # +m: disable job control notification
        self.cmd('unset HISTFILE; stty -echo; set +m')

    def mountPrivateDirs(self):
        "mount private directories"
        # Avoid expanding a string into a list of chars
        assert not isinstance(self.privateDirs, BaseString)
        for directory in self.privateDirs:
            if isinstance(directory, tuple):
                # mount given private directory
                privateDir = directory[1] % self.__dict__
                mountPoint = directory[0]
                self.cmd('mkdir -p %s' % privateDir)
                self.cmd('mkdir -p %s' % mountPoint)
                self.cmd('mount --bind %s %s' %
                         (privateDir, mountPoint))
            else:
                # mount temporary filesystem on directory
                self.cmd('mkdir -p %s' % directory)
                self.cmd('mount -n -t tmpfs tmpfs %s' % directory)

    def unmountPrivateDirs(self):
        "mount private directories"
        for directory in self.privateDirs:
            if isinstance(directory, tuple):
                self.cmd('umount ', directory[0])
            else:
                self.cmd('umount ', directory)

    def _popen(self, cmd, **params):
        """Internal method: spawn and return a process
            cmd: command to run (list)
            params: parameters to Popen()"""
        # Leave this is as an instance method for now
        assert self
        popen = Popen(cmd, **params)
        debug('_popen', cmd, popen.pid)
        return popen

    def cleanup(self):
        "Help python collect its garbage."
        # We used to do this, but it slows us down:
        # Intfs may end up in root NS
        # for intfName in self.intfNames():
        # if self.name in intfName:
        # quietRun( 'ip link del ' + intfName )
        if self.shell:
            # Close ptys
            self.stdin.close()
            os.close(self.slave)
            if self.waitExited:
                debug('waiting for', self.pid, 'to terminate\n')
                self.shell.wait()
        self.shell = None

    # Subshell I/O, commands and control

    def read(self, size=1024):
        """Buffered read from node, potentially blocking.
           size: maximum number of characters to return"""
        count = len(self.readbuf)
        if count < size:
            data = os.read(self.stdout.fileno(), size - count)
            self.readbuf += self.decoder.decode(data)
        if size >= len(self.readbuf):
            result = self.readbuf
            self.readbuf = ''
        else:
            result = self.readbuf[:size]
            self.readbuf = self.readbuf[size:]
        return result

    def readline(self):
        """Buffered readline from node, potentially blocking.
           returns: line (minus newline) or None"""
        self.readbuf += self.read(1024)
        if '\n' not in self.readbuf:
            return None
        pos = self.readbuf.find('\n')
        line = self.readbuf[0: pos]
        self.readbuf = self.readbuf[pos + 1:]
        return line

    def write(self, data):
        """Write data to node.
           data: string"""
        os.write(self.stdin.fileno(), encode(data))

    def terminate(self):
        "Send kill signal to Node and clean up after it."
        self.unmountPrivateDirs()
        if self.shell:
            if self.shell.poll() is None:
                os.killpg(self.shell.pid, signal.SIGHUP)
        self.cleanup()

    def stop(self, deleteIntfs=False):
        """Stop node.
           deleteIntfs: delete interfaces? (False)"""
        if deleteIntfs:
            self.deleteIntfs()
        self.terminate()

    def waitReadable(self, timeoutms=None):
        """Wait until node's output is readable.
           timeoutms: timeout in ms or None to wait indefinitely.
           returns: result of poll()"""
        if len(self.readbuf) == 0:
            return self.pollOut.poll(timeoutms)
        return None

    def sendCmd(self, *args, **kwargs):
        """Send a command, followed by a command to echo a sentinel,
           and return without waiting for the command to complete.
           args: command and arguments, or string
           printPid: print command's PID? (False)"""
        assert self.shell and not self.waiting
        printPid = kwargs.get('printPid', False)
        # Allow sendCmd( [ list ] )
        if len(args) == 1 and isinstance(args[0], list):
            cmd = args[0]
        # Allow sendCmd( cmd, arg1, arg2... )
        elif len(args) > 0:
            cmd = args
        # Convert to string
        if not isinstance(cmd, str):
            cmd = ' '.join([str(c) for c in cmd])
        if not re.search(r'\w', cmd):
            # Replace empty commands with something harmless
            cmd = 'echo -n'
        self.lastCmd = cmd
        # if a builtin command is backgrounded, it still yields a PID
        if len(cmd) > 0 and cmd[-1] == '&':
            # print ^A{pid}\n so monitor() can set lastPid
            cmd += ' printf "\\001%d\\012" $! '
        elif printPid and not isShellBuiltin(cmd):
            cmd = 'mnexec -p ' + cmd
        self.write(cmd + '\n')
        self.lastPid = None
        self.waiting = True

    def sendInt(self, intr=chr(3)):
        "Interrupt running command."
        debug('sendInt: writing chr(%d)\n' % ord(intr))
        self.write(intr)

    def monitor(self, timeoutms=None, findPid=True):
        """Monitor and return the output of a command.
           Set self.waiting to False if command has completed.
           timeoutms: timeout in ms or None to wait indefinitely
           findPid: look for PID from mnexec -p"""
        ready = self.waitReadable(timeoutms)
        if not ready:
            return ''
        data = self.read(1024)
        pidre = r'\[\d+\] \d+\r\n'
        # Look for PID
        marker = chr(1) + r'\d+\r\n'
        if findPid and chr(1) in data:
            # suppress the job and PID of a backgrounded command
            if re.findall(pidre, data):
                data = re.sub(pidre, '', data)
            # Marker can be read in chunks; continue until all of it is read
            while not re.findall(marker, data):
                data += self.read(1024)
            markers = re.findall(marker, data)
            if markers:
                self.lastPid = int(markers[0][1:])
                data = re.sub(marker, '', data)
        # Look for sentinel/EOF
        if len(data) > 0 and data[-1] == chr(127):
            self.waiting = False
            data = data[:-1]
        elif chr(127) in data:
            self.waiting = False
            data = data.replace(chr(127), '')
        return data

    def waitOutput(self, verbose=False, findPid=True):
        """Wait for a command to complete.
           Completion is signaled by a sentinel character, ASCII(127)
           appearing in the output stream.  Wait for the sentinel and return
           the output, including trailing newline.
           verbose: print output interactively"""
        log = info if verbose else debug
        output = ''
        while self.waiting:
            data = self.monitor(findPid=findPid)
            output += data
            log(data)
        return output

    def cmd(self, *args, **kwargs):
        """Send a command, wait for output, and return it.
           cmd: string"""
        verbose = kwargs.get('verbose', False)
        log = info if verbose else debug
        log('*** %s : %s\n' % (self.name, args))
        if self.shell:
            self.sendCmd(*args, **kwargs)
            return self.waitOutput(verbose)
        else:
            warn('(%s exited - ignoring cmd%s)\n' % (self, args))
        return None

    def cmdPrint(self, *args):
        """Call cmd and printing its output
           cmd: string"""
        return self.cmd(*args, **{'verbose': True})

    def popen(self, *args, **kwargs):
        """Return a Popen() object in our namespace
           args: Popen() args, single list, or string
           kwargs: Popen() keyword args"""
        defaults = {'stdout': PIPE, 'stderr': PIPE,
                    'mncmd':
                    ['mnexec', '-da', str(self.pid)]}
        defaults.update(kwargs)
        shell = defaults.pop('shell', False)
        if len(args) == 1:
            if isinstance(args[0], list):
                # popen([cmd, arg1, arg2...])
                cmd = args[0]
            elif isinstance(args[0], BaseString):
                # popen("cmd arg1 arg2...")
                cmd = [args[0]] if shell else args[0].split()
            else:
                raise Exception('popen() requires a string or list')
        elif len(args) > 0:
            # popen( cmd, arg1, arg2... )
            cmd = list(args)
        if shell:
            cmd = [os.environ['SHELL'], '-c'] + [' '.join(cmd)]
        # Attach to our namespace  using mnexec -a
        cmd = defaults.pop('mncmd') + cmd
        popen = self._popen(cmd, **defaults)
        return popen

    def pexec(self, *args, **kwargs):
        """Execute a command using popen
           returns: out, err, exitcode"""
        popen = self.popen(*args, stdin=PIPE, stdout=PIPE, stderr=PIPE,
                           **kwargs)
        # Warning: this can fail with large numbers of fds!
        out, err = popen.communicate()
        exitcode = popen.wait()
        return decode(out), decode(err), exitcode

    # Interface management, configuration, and routing

    # BL notes: This might be a bit redundant or over-complicated.
    # However, it does allow a bit of specialization, including
    # changing the canonical interface names. It's also tricky since
    # the real interfaces are created as veth pairs, so we can't
    # make a single interface at a time.

    def newPort(self):
        "Return the next port number to allocate."
        if len(self.ports) > 0:
            return max(self.ports.values()) + 1
        return self.portBase

    def addIntf(self, intf, port=None, moveIntfFn=moveIntf):
        """Add an interface.
           intf: interface
           port: port number (optional, typically OpenFlow port number)
           moveIntfFn: function to move interface (optional)"""
        if port is None:
            port = self.newPort()
        self.intfs[port] = intf
        self.ports[intf] = port
        self.nameToIntf[intf.name] = intf
        debug('\n')
        debug('added intf %s (%d) to node %s\n' % (
            intf, port, self.name))
        if self.inNamespace:
            debug('moving', intf, 'into namespace for', self.name, '\n')
            moveIntfFn(intf.name, self)

    def delIntf(self, intf):
        """Remove interface from Node's known interfaces
           Note: to fully delete interface, call intf.delete() instead"""
        port = self.ports.get(intf)
        if port is not None:
            del self.intfs[port]
            del self.ports[intf]
            del self.nameToIntf[intf.name]

    def defaultIntf(self):
        "Return interface for lowest port"
        ports = self.intfs.keys()
        if ports:
            return self.intfs[min(ports)]
        else:
            warn('*** defaultIntf: warning:', self.name,
                 'has no interfaces\n')
        return None

    def intf(self, intf=None):
        """Return our interface object with given string name,
           default intf if name is falsy (None, empty string, etc).
           or the input intf arg.

        Having this fcn return its arg for Intf objects makes it
        easier to construct functions with flexible input args for
        interfaces (those that accept both string names and Intf objects).
        """
        if not intf:
            return self.defaultIntf()
        elif isinstance(intf, BaseString):
            return self.nameToIntf[intf]
        else:
            return intf

    def connectionsTo(self, node):
        "Return [ intf1, intf2... ] for all intfs that connect self to node."
        # We could optimize this if it is important
        connections = []
        for intf in self.intfList():
            link = intf.link
            if link:
                node1, node2 = link.intf1.node, link.intf2.node
                if node1 == self and node2 == node:
                    connections += [(intf, link.intf2)]
                elif node1 == node and node2 == self:
                    connections += [(intf, link.intf1)]
        return connections

    def deleteIntfs(self, checkName=True):
        """Delete all of our interfaces.
           checkName: only delete interfaces that contain our name"""
        # In theory the interfaces should go away after we shut down.
        # However, this takes time, so we're better off removing them
        # explicitly so that we won't get errors if we run before they
        # have been removed by the kernel. Unfortunately this is very slow,
        # at least with Linux kernels before 2.6.33
        for intf in list(self.intfs.values()):
            # Protect against deleting hardware interfaces
            if (self.name in intf.name) or (not checkName):
                intf.delete()
                info('.')

    # Routing support

    def setARP(self, ip, mac):
        """Add an ARP entry.
           ip: IP address as string
           mac: MAC address as string"""
        result = self.cmd('arp', '-s', ip, mac)
        return result

    def setHostRoute(self, ip, intf):
        """Add route to host.
           ip: IP address as dotted decimal
           intf: string, interface name"""
        return self.cmd('route add -host', ip, 'dev', intf)

    def setDefaultRoute(self, intf=None):
        """Set the default route to go through intf.
           intf: Intf or {dev <intfname> via <gw-ip> ...}"""
        # Note setParam won't call us if intf is none
        if isinstance(intf, BaseString) and ' ' in intf:
            params = intf
        else:
            params = 'dev %s' % intf
        # Do this in one line in case we're messing with the root namespace
        self.cmd('ip route del default; ip route add default', params)

    # Convenience and configuration methods

    def setMAC(self, mac, intf=None):
        """Set the MAC address for an interface.
           intf: intf or intf name
           mac: MAC address as string"""
        return self.intf(intf).setMAC(mac)

    def setIP(self, ip, prefixLen=8, intf=None, **kwargs):
        """Set the IP address for an interface.
           intf: intf or intf name
           ip: IP address as a string
           prefixLen: prefix length, e.g. 8 for /8 or 16M addrs
           kwargs: any additional arguments for intf.setIP"""
        return self.intf(intf).setIP(ip, prefixLen, **kwargs)

    def IP(self, intf=None):
        "Return IP address of a node or specific interface."
        return self.intf(intf).IP()

    def MAC(self, intf=None):
        "Return MAC address of a node or specific interface."
        return self.intf(intf).MAC()

    def intfIsUp(self, intf=None):
        "Check if an interface is up."
        return self.intf(intf).isUp()

    # The reason why we configure things in this way is so
    # That the parameters can be listed and documented in
    # the config method.
    # Dealing with subclasses and superclasses is slightly
    # annoying, but at least the information is there!

    def setParam(self, results, method, **param):
        """Internal method: configure a *single* parameter
           results: dict of results to update
           method: config method name
           param: arg=value (ignore if value=None)
           value may also be list or dict"""
        name, value = list(param.items())[0]
        if value is None:
            return None
        f = getattr(self, method, None)
        if not f:
            return None
        if isinstance(value, list):
            result = f(*value)
        elif isinstance(value, dict):
            result = f(**value)
        else:
            result = f(value)
        results[name] = result
        return result

    def config(self, mac=None, ip=None,
               defaultRoute=None, lo='up', **_params):
        """Configure Node according to (optional) parameters:
           mac: MAC address for default interface
           ip: IP address for default interface
           ifconfig: arbitrary interface configuration
           Subclasses should override this method and call
           the parent class's config(**params)"""
        # If we were overriding this method, we would call
        # the superclass config method here as follows:
        # r = Parent.config( **_params )
        r = {}
        self.setParam(r, 'setMAC', mac=mac)
        self.setParam(r, 'setIP', ip=ip)
        self.setParam(r, 'setDefaultRoute', defaultRoute=defaultRoute)
        # This should be examined
        self.cmd('ifconfig lo ' + lo)
        return r

    def configDefault(self, **moreParams):
        "Configure with default parameters"
        self.params.update(moreParams)
        self.config(**self.params)

    # This is here for backward compatibility
    def linkTo(self, node, link=Link):
        """(Deprecated) Link to another node
           replace with Link( node1, node2)"""
        return link(self, node)

    # Other methods

    def intfList(self):
        "List of our interfaces sorted by port number"
        return [self.intfs[p] for p in sorted(self.intfs.keys())]

    def intfNames(self):
        "The names of our interfaces sorted by port number"
        return [str(i) for i in self.intfList()]

    def __repr__(self):
        "More informative string representation"
        intfs = (','.join(['%s:%s' % (i.name, i.IP())
                           for i in self.intfList()]))
        return '<%s %s: %s pid=%s> ' % (
            self.__class__.__name__, self.name, intfs, self.pid)

    def __str__(self):
        "Abbreviated string representation"
        return self.name

    # Automatic class setup support

    isSetup = False

    @classmethod
    def checkSetup(cls):
        "Make sure our class and superclasses are set up"

        # @simmsb: this never worked before, hopefully fixing it doesn't break things?
        for klass in cls.mro():
            if klass.__dict__.get("isSetup", False) or not issubclass(klass, Node):
                continue

            klass.setup()
            klass.isSetup = True

    @classmethod
    def setup(cls):
        "Make sure our class dependencies are available"
        pathCheck('mnexec', 'ifconfig', moduleName='Mininet')


class Host(Node):
    "A host is simply a Node"
    pass


class CPULimitedHost(Host):

    "CPU limited host"

    def __init__(self, name, sched='cfs', **kwargs):
        Host.__init__(self, name, **kwargs)
        # Initialize class if necessary
        if not CPULimitedHost.inited:
            CPULimitedHost.init()
        # Create a cgroup and move shell into it
        self.cgroup = 'cpu,cpuacct,cpuset:/' + self.name
        errFail('cgcreate -g ' + self.cgroup)
        # We don't add ourselves to a cpuset because you must
        # specify the cpu and memory placement first
        errFail('cgclassify -g cpu,cpuacct:/%s %s' % (self.name, self.pid))
        # BL: Setting the correct period/quota is tricky, particularly
        # for RT. RT allows very small quotas, but the overhead
        # seems to be high. CFS has a mininimum quota of 1 ms, but
        # still does better with larger period values.
        self.period_us = kwargs.get('period_us', 100000)
        self.sched = sched
        if sched == 'rt':
            self.checkRtGroupSched()
            self.rtprio = 20

    def cgroupSet(self, param, value, resource='cpu'):
        "Set a cgroup parameter and return its value"
        cmd = 'cgset -r %s.%s=%s /%s' % (
            resource, param, value, self.name)
        quietRun(cmd)
        nvalue = int(self.cgroupGet(param, resource))
        if nvalue != value:
            error('*** error: cgroupSet: %s set to %s instead of %s\n'
                  % (param, nvalue, value))
        return nvalue

    def cgroupGet(self, param, resource='cpu'):
        "Return value of cgroup parameter"
        cmd = 'cgget -r %s.%s /%s' % (
            resource, param, self.name)
        return int(quietRun(cmd).split()[-1])

    def cgroupDel(self):
        "Clean up our cgroup"
        # info( '*** deleting cgroup', self.cgroup, '\n' )
        _out, _err, exitcode = errRun('cgdelete -r ' + self.cgroup)
        # Sometimes cgdelete returns a resource busy error but still
        # deletes the group; next attempt will give "no such file"
        return exitcode == 0 or ('no such file' in _err.lower())

    def popen(self, *args, **kwargs):
        """Return a Popen() object in node's namespace
           args: Popen() args, single list, or string
           kwargs: Popen() keyword args"""
        # Tell mnexec to execute command in our cgroup
        mncmd = kwargs.pop('mncmd', ['mnexec', '-g', self.name,
                                     '-da', str(self.pid)])
        # if our cgroup is not given any cpu time,
        # we cannot assign the RR Scheduler.
        if self.sched == 'rt':
            if int(self.cgroupGet('rt_runtime_us', 'cpu')) <= 0:
                mncmd += ['-r', str(self.rtprio)]
            else:
                debug('*** error: not enough cpu time available for %s.' %
                      self.name, 'Using cfs scheduler for subprocess\n')
        return Host.popen(self, *args, mncmd=mncmd, **kwargs)

    def cleanup(self):
        "Clean up Node, then clean up our cgroup"
        super(CPULimitedHost, self).cleanup()
        retry(retries=3, delaySecs=.1, fn=self.cgroupDel)

    _rtGroupSched = False   # internal class var: Is CONFIG_RT_GROUP_SCHED set?

    @classmethod
    def checkRtGroupSched(cls):
        "Check (Ubuntu,Debian) kernel config for CONFIG_RT_GROUP_SCHED for RT"
        if not cls._rtGroupSched:
            release = quietRun('uname -r').strip('\r\n')
            output = quietRun('grep CONFIG_RT_GROUP_SCHED /boot/config-%s' %
                              release)
            if output == '# CONFIG_RT_GROUP_SCHED is not set\n':
                error('\n*** error: please enable RT_GROUP_SCHED '
                      'in your kernel\n')
                exit(1)
            cls._rtGroupSched = True

    def chrt(self):
        "Set RT scheduling priority"
        quietRun('chrt -p %s %s' % (self.rtprio, self.pid))
        result = quietRun('chrt -p %s' % self.pid)
        firstline = result.split('\n')[0]
        lastword = firstline.split(' ')[-1]
        if lastword != 'SCHED_RR':
            error('*** error: could not assign SCHED_RR to %s\n' % self.name)
        return lastword

    def rtInfo(self, f):
        "Internal method: return parameters for RT bandwidth"
        pstr, qstr = 'rt_period_us', 'rt_runtime_us'
        # RT uses wall clock time for period and quota
        quota = int(self.period_us * f)
        return pstr, qstr, self.period_us, quota

    def cfsInfo(self, f):
        "Internal method: return parameters for CFS bandwidth"
        pstr, qstr = 'cfs_period_us', 'cfs_quota_us'
        # CFS uses wall clock time for period and CPU time for quota.
        quota = int(self.period_us * f * numCores())
        period = self.period_us
        if f > 0 and quota < 1000:
            debug('(cfsInfo: increasing default period) ')
            quota = 1000
            period = int(quota / f / numCores())
        # Reset to unlimited on negative quota
        if quota < 0:
            quota = -1
        return pstr, qstr, period, quota

    # BL comment:
    # This may not be the right API,
    # since it doesn't specify CPU bandwidth in "absolute"
    # units the way link bandwidth is specified.
    # We should use MIPS or SPECINT or something instead.
    # Alternatively, we should change from system fraction
    # to CPU seconds per second, essentially assuming that
    # all CPUs are the same.

    def setCPUFrac(self, f, sched=None):
        """Set overall CPU fraction for this host
           f: CPU bandwidth limit (positive fraction, or -1 for cfs unlimited)
           sched: 'rt' or 'cfs'
           Note 'cfs' requires CONFIG_CFS_BANDWIDTH,
           and 'rt' requires CONFIG_RT_GROUP_SCHED"""
        if not sched:
            sched = self.sched
        if sched == 'rt':
            if not f or f < 0:
                raise Exception('Please set a positive CPU fraction'
                                ' for sched=rt\n')
            pstr, qstr, period, quota = self.rtInfo(f)
        elif sched == 'cfs':
            pstr, qstr, period, quota = self.cfsInfo(f)
        else:
            return
        # Set cgroup's period and quota
        setPeriod = self.cgroupSet(pstr, period)
        setQuota = self.cgroupSet(qstr, quota)
        if sched == 'rt':
            # Set RT priority if necessary
            sched = self.chrt()
        info('(%s %d/%dus) ' % (sched, setQuota, setPeriod))

    def setCPUs(self, cores, mems=0):
        "Specify (real) cores that our cgroup can run on"
        if not cores:
            return
        if isinstance(cores, list):
            cores = ','.join([str(c) for c in cores])
        self.cgroupSet(resource='cpuset', param='cpus',
                       value=cores)
        # Memory placement is probably not relevant, but we
        # must specify it anyway
        self.cgroupSet(resource='cpuset', param='mems',
                       value=mems)
        # We have to do this here after we've specified
        # cpus and mems
        errFail('cgclassify -g cpuset:/%s %s' % (
            self.name, self.pid))

    # pylint: disable=arguments-differ
    def config(self, cpu=-1, cores=None, **params):
        """cpu: desired overall system CPU fraction
           cores: (real) core(s) this host can run on
           params: parameters for Node.config()"""
        r = Node.config(self, **params)
        # Was considering cpu={'cpu': cpu , 'sched': sched}, but
        # that seems redundant
        self.setParam(r, 'setCPUFrac', cpu=cpu)
        self.setParam(r, 'setCPUs', cores=cores)
        return r

    inited = False

    @classmethod
    def init(cls):
        "Initialization for CPULimitedHost class"
        mountCgroups()
        cls.inited = True


# Some important things to note:
#
# The "IP" address which setIP() assigns to the switch is not
# an "IP address for the switch" in the sense of IP routing.
# Rather, it is the IP address for the control interface,
# on the control network, and it is only relevant to the
# controller. If you are running in the root namespace
# (which is the only way to run OVS at the moment), the
# control interface is the loopback interface, and you
# normally never want to change its IP address!
#
# In general, you NEVER want to attempt to use Linux's
# network stack (i.e. ifconfig) to "assign" an IP address or
# MAC address to a switch data port. Instead, you "assign"
# the IP and MAC addresses in the controller by specifying
# packets that you want to receive or send. The "MAC" address
# reported by ifconfig for a switch data port is essentially
# meaningless. It is important to understand this if you
# want to create a functional router using OpenFlow.

class Switch(Node):
    """A Switch is a Node that is running (or has execed?)
       an OpenFlow switch."""

    portBase = 1  # Switches start with port 1 in OpenFlow
    dpidLen = 16  # digits in dpid passed to switch

    def __init__(self, name, dpid=None, opts='', listenPort=None, **params):
        """dpid: dpid hex string (or None to derive from name, e.g. s1 -> 1)
           opts: additional switch options
           listenPort: port to listen on for dpctl connections"""
        Node.__init__(self, name, **params)
        self.dpid = self.defaultDpid(dpid)
        self.opts = opts
        self.listenPort = listenPort
        if not self.inNamespace:
            self.controlIntf = Intf('lo', self, port=0)

    def defaultDpid(self, dpid=None):
        "Return correctly formatted dpid from dpid or switch name (s1 -> 1)"
        if dpid:
            # Remove any colons and make sure it's a good hex number
            dpid = dpid.replace(':', '')
            assert len(dpid) <= self.dpidLen and int(dpid, 16) >= 0
        else:
            # Use hex of the first number in the switch name
            nums = re.findall(r'\d+', self.name)
            if nums:
                dpid = hex(int(nums[0]))[2:]
            else:
                self.terminate()  # Python 3.6 crash workaround
                raise Exception('Unable to derive default datapath ID - '
                                'please either specify a dpid or use a '
                                'canonical switch name such as s23.')
        return '0' * (self.dpidLen - len(dpid)) + dpid

    def defaultIntf(self):
        "Return control interface"
        if self.controlIntf:
            return self.controlIntf
        else:
            return Node.defaultIntf(self)

    def sendCmd(self, *cmd, **kwargs):
        """Send command to Node.
           cmd: string"""
        kwargs.setdefault('printPid', False)
        if not self.execed:
            return Node.sendCmd(self, *cmd, **kwargs)
        else:
            error('*** Error: %s has execed and cannot accept commands' %
                  self.name)
        return None

    def connected(self):
        "Is the switch connected to a controller? (override this method)"
        # Assume that we are connected by default to whatever we need to
        # be connected to. This should be overridden by any OpenFlow
        # switch, but not by a standalone bridge.
        debug('Assuming', repr(self), 'is connected to a controller\n')
        return True

    def stop(self, deleteIntfs=True):
        """Stop switch
           deleteIntfs: delete interfaces? (True)"""
        if deleteIntfs:
            self.deleteIntfs()

    def __repr__(self):
        "More informative string representation"
        intfs = (','.join(['%s:%s' % (i.name, i.IP())
                           for i in self.intfList()]))
        return '<%s %s: %s pid=%s> ' % (
            self.__class__.__name__, self.name, intfs, self.pid)



class UserSwitch(Switch):
    "User-space switch."

    dpidLen = 12

    def __init__(self, name, dpopts='--no-slicing', **kwargs):
        """Init.
           name: name for the switch
           dpopts: additional arguments to ofdatapath (--no-slicing)"""
        Switch.__init__(self, name, **kwargs)
        pathCheck('ofdatapath', 'ofprotocol',
                  moduleName='the OpenFlow reference user switch' +
                  '(openflow.org)')
        if self.listenPort:
            self.opts += ' --listen=ptcp:%i ' % self.listenPort
        else:
            self.opts += ' --listen=punix:/tmp/%s.listen' % self.name
        self.dpopts = dpopts

    @classmethod
    def setup(cls):
        "Ensure any dependencies are loaded; if not, try to load them."
        if not os.path.exists('/dev/net/tun'):
            moduleDeps(add=TUN)

    def dpctl(self, *args):
        "Run dpctl command"
        listenAddr = None
        if not self.listenPort:
            listenAddr = 'unix:/tmp/%s.listen' % self.name
        else:
            listenAddr = 'tcp:127.0.0.1:%i' % self.listenPort
        return self.cmd('dpctl ' + ' '.join(args) +
                        ' ' + listenAddr)

    def connected(self):
        "Is the switch connected to a controller?"
        status = self.dpctl('status')
        return ('remote.is-connected=true' in status and
                'local.is-connected=true' in status)

    @staticmethod
    def TCReapply(intf):
        """Unfortunately user switch and Mininet are fighting
           over tc queuing disciplines. To resolve the conflict,
           we re-create the user switch's configuration, but as a
           leaf of the TCIntf-created configuration."""
        if isinstance(intf, TCIntf):
            ifspeed = 10000000000  # 10 Gbps
            minspeed = ifspeed * 0.001

            res = intf.config(**intf.params)

            if res is None:  # link may not have TC parameters
                return

            # Re-add qdisc, root, and default classes user switch created, but
            # with new parent, as setup by Mininet's TCIntf
            parent = res['parent']
            intf.tc("%s qdisc add dev %s " + parent +
                    " handle 1: htb default 0xfffe")
            intf.tc("%s class add dev %s classid 1:0xffff parent 1: htb rate "
                    + str(ifspeed))
            intf.tc("%s class add dev %s classid 1:0xfffe parent 1:0xffff " +
                    "htb rate " + str(minspeed) + " ceil " + str(ifspeed))

    def start(self, controllers):
        """Start OpenFlow reference user datapath.
           Log to /tmp/sN-{ofd,ofp}.log.
           controllers: list of controller objects"""
        # Add controllers
        clist = ','.join(['tcp:%s:%d' % (c.IP(), c.port)
                          for c in controllers])
        ofdlog = '/tmp/' + self.name + '-ofd.log'
        ofplog = '/tmp/' + self.name + '-ofp.log'
        intfs = [str(i) for i in self.intfList() if not i.IP()]
        self.cmd('ofdatapath -i ' + ','.join(intfs) +
                 ' punix:/tmp/' + self.name + ' -d %s ' % self.dpid +
                 self.dpopts +
                 ' 1> ' + ofdlog + ' 2> ' + ofdlog + ' &')
        self.cmd('ofprotocol unix:/tmp/' + self.name +
                 ' ' + clist +
                 ' --fail=closed ' + self.opts +
                 ' 1> ' + ofplog + ' 2>' + ofplog + ' &')
        if "no-slicing" not in self.dpopts:
            # Only TCReapply if slicing is enable
            sleep(1)  # Allow ofdatapath to start before re-arranging qdisc's
            for intf in self.intfList():
                if not intf.IP():
                    self.TCReapply(intf)

    def stop(self, deleteIntfs=True):
        """Stop OpenFlow reference user datapath.
           deleteIntfs: delete interfaces? (True)"""
        self.cmd('kill %ofdatapath')
        self.cmd('kill %ofprotocol')
        super(UserSwitch, self).stop(deleteIntfs)


class OVSSwitch(Switch):
    "Open vSwitch switch. Depends on ovs-vsctl."

    def __init__(self, name, failMode='secure', datapath='kernel',
                 inband=False, protocols=None,
                 reconnectms=1000, stp=False, batch=False, **params):
        """name: name for switch
           failMode: controller loss behavior (secure|standalone)
           datapath: userspace or kernel mode (kernel|user)
           inband: use in-band control (False)
           protocols: use specific OpenFlow version(s) (e.g. OpenFlow13)
                      Unspecified (or old OVS version) uses OVS default
           reconnectms: max reconnect timeout in ms (0/None for default)
           stp: enable STP (False, requires failMode=standalone)
           batch: enable batch startup (False)"""
        Switch.__init__(self, name, **params)
        self.failMode = failMode
        self.datapath = datapath
        self.inband = inband
        self.protocols = protocols
        self.reconnectms = reconnectms
        self.stp = stp
        self._uuids = []  # controller UUIDs
        self.batch = batch
        self.commands = []  # saved commands for batch startup

    @classmethod
    def setup(cls):
        "Make sure Open vSwitch is installed and working"
        pathCheck('ovs-vsctl',
                  moduleName='Open vSwitch (openvswitch.org)')
        # This should no longer be needed, and it breaks
        # with OVS 1.7 which has renamed the kernel module:
        #  moduleDeps( subtract=OF_KMOD, add=OVS_KMOD )
        out, err, exitcode = errRun('ovs-vsctl -t 1 show')
        if exitcode:
            error( out + err +
                   'ovs-vsctl exited with code %d\n' % exitcode +
                   '*** Error connecting to ovs-db with ovs-vsctl\n'
                   'Make sure that Open vSwitch is installed, '
                   'that ovsdb-server is running, and that\n'
                   '"ovs-vsctl show" works correctly.\n'
                   'You may wish to try '
                   '"service openvswitch-switch start".\n' )
            exit( 1 )
        version = quietRun( 'ovs-vsctl --version' )
        cls.OVSVersion = re.findall( r'\d+\.\d+', version )[ 0 ]

    @classmethod
    def isOldOVS(cls):
        "Is OVS ersion < 1.10?"
        return (StrictVersion(cls.OVSVersion) <
                StrictVersion('1.10'))

    def dpctl(self, *args):
        "Run ovs-ofctl command"
        return self.cmd('ovs-ofctl', args[0], self, *args[1:])

    def vsctl(self, *args, **kwargs):
        "Run ovs-vsctl command (or queue for later execution)"
        if self.batch:
            cmd = ' '.join(str(arg).strip() for arg in args)
            self.commands.append(cmd)
            return None
        else:
            return self.cmd('ovs-vsctl', *args, **kwargs)

    @staticmethod
    def TCReapply(intf):
        """Unfortunately OVS and Mininet are fighting
           over tc queuing disciplines. As a quick hack/
           workaround, we clear OVS's and reapply our own."""
        if isinstance(intf, TCIntf):
            intf.config(**intf.params)

    def attach(self, intf):
        "Connect a data port"
        self.vsctl('add-port', self, intf)
        self.cmd('ifconfig', intf, 'up')
        self.TCReapply(intf)

    def detach(self, intf):
        "Disconnect a data port"
        self.vsctl('del-port', self, intf)

    def controllerUUIDs(self, update=False):
        """Return ovsdb UUIDs for our controllers
           update: update cached value"""
        if not self._uuids or update:
            controllers = self.cmd('ovs-vsctl -- get Bridge', self,
                                   'Controller').strip()
            if controllers.startswith('[') and controllers.endswith(']'):
                controllers = controllers[1: -1]
                if controllers:
                    self._uuids = [c.strip()
                                   for c in controllers.split(',')]
        return self._uuids

    def connected(self):
        "Are we connected to at least one of our controllers?"
        for uuid in self.controllerUUIDs():
            if 'true' in self.vsctl('-- get Controller',
                                    uuid, 'is_connected'):
                return True
        return self.failMode == 'standalone'

    def intfOpts(self, intf):
        "Return OVS interface options for intf"
        opts = ''
        if not self.isOldOVS():
            # ofport_request is not supported on old OVS
            opts += ' ofport_request=%s' % self.ports[intf]
            # Patch ports don't work well with old OVS
            if isinstance(intf, OVSIntf):
                intf1, intf2 = intf.link.intf1, intf.link.intf2
                peer = intf1 if intf1 != intf else intf2
                opts += ' type=patch options:peer=%s' % peer
        return '' if not opts else ' -- set Interface %s' % intf + opts

    def bridgeOpts(self):
        "Return OVS bridge options"
        opts = (' other_config:datapath-id=%s' % self.dpid +
                ' fail_mode=%s' % self.failMode)
        if not self.inband:
            opts += ' other-config:disable-in-band=true'
        if self.datapath == 'user':
            opts += ' datapath_type=netdev'
        if self.protocols and not self.isOldOVS():
            opts += ' protocols=%s' % self.protocols
        if self.stp and self.failMode == 'standalone':
            opts += ' stp_enable=true'
        opts += ' other-config:dp-desc=%s' % self.name
        return opts

    def start(self, controllers):
        "Start up a new OVS OpenFlow switch using ovs-vsctl"
        if self.inNamespace:
            raise Exception(
                'OVS kernel switch does not work in a namespace')
        int(self.dpid, 16)  # DPID must be a hex string
        # Command to add interfaces
        intfs = ''.join(' -- add-port %s %s' % (self, intf) +
                        self.intfOpts(intf)
                        for intf in self.intfList()
                        if self.ports[intf] and not intf.IP())
        # Command to create controller entries
        clist = [(self.name + c.name, '%s:%s:%d' %
                  (c.protocol, c.IP(), c.port))
                 for c in controllers]
        if self.listenPort:
            clist.append((self.name + '-listen',
                          'ptcp:%s' % self.listenPort))
        ccmd = '-- --id=@%s create Controller target=\\"%s\\"'
        if self.reconnectms:
            ccmd += ' max_backoff=%d' % self.reconnectms
        cargs = ' '.join(ccmd % (name, target)
                         for name, target in clist)
        # Controller ID list
        cids = ','.join('@%s' % name for name, _target in clist)
        # Try to delete any existing bridges with the same name
        if not self.isOldOVS():
            cargs += ' -- --if-exists del-br %s' % self
        # One ovs-vsctl command to rule them all!
        self.vsctl(cargs +
                   ' -- add-br %s' % self +
                   ' -- set bridge %s controller=[%s]' % (self, cids) +
                   self.bridgeOpts() +
                   intfs)
        # If necessary, restore TC config overwritten by OVS
        if not self.batch:
            for intf in self.intfList():
                self.TCReapply(intf)

    # This should be ~ int( quietRun( 'getconf ARG_MAX' ) ),
    # but the real limit seems to be much lower
    argmax = 128000

    @classmethod
    def batchStartup(cls, switches, run=errRun):
        """Batch startup for OVS
           switches: switches to start up
           run: function to run commands (errRun)"""
        info('...')
        cmds = 'ovs-vsctl'
        for switch in switches:
            if switch.isOldOVS():
                # Ideally we'd optimize this also
                run('ovs-vsctl del-br %s' % switch)
            for cmd in switch.commands:
                cmd = cmd.strip()
                # Don't exceed ARG_MAX
                if len(cmds) + len(cmd) >= cls.argmax:
                    run(cmds, shell=True)
                    cmds = 'ovs-vsctl'
                cmds += ' ' + cmd
                switch.cmds = []
                switch.batch = False
        if cmds:
            run(cmds, shell=True)
        # Reapply link config if necessary...
        for switch in switches:
            for intf in switch.intfs.values():
                if isinstance(intf, TCIntf):
                    intf.config(**intf.params)
        return switches

    def stop(self, deleteIntfs=True):
        """Terminate OVS switch.
           deleteIntfs: delete interfaces? (True)"""
        self.cmd('ovs-vsctl del-br', self)
        if self.datapath == 'user':
            self.cmd('ip link del', self)
        super(OVSSwitch, self).stop(deleteIntfs)

    @classmethod
    def batchShutdown(cls, switches, run=errRun):
        "Shut down a list of OVS switches"
        delcmd = 'del-br %s'
        if switches and not switches[0].isOldOVS():
            delcmd = '--if-exists ' + delcmd
        # First, delete them all from ovsdb
        run('ovs-vsctl ' +
            ' -- '.join(delcmd % s for s in switches))
        # Next, shut down all of the processes
        pids = ' '.join(str(switch.pid) for switch in switches)
        run('kill -HUP ' + pids)
        for switch in switches:
            switch.terminate()
        return switches


OVSKernelSwitch = OVSSwitch


class OVSBridge(OVSSwitch):
    "OVSBridge is an OVSSwitch in standalone/bridge mode"

    def __init__(self, *args, **kwargs):
        """stp: enable Spanning Tree Protocol (False)
           see OVSSwitch for other options"""
        kwargs.update(failMode='standalone')
        OVSSwitch.__init__(self, *args, **kwargs)

    def start(self, controllers):
        "Start bridge, ignoring controllers argument"
        OVSSwitch.start(self, controllers=[])

    def connected(self):
        "Are we forwarding yet?"
        if self.stp:
            status = self.dpctl('show')
            return 'STP_FORWARD' in status and 'STP_LEARN' not in status
        else:
            return True


class IVSSwitch(Switch):
    "Indigo Virtual Switch"

    def __init__(self, name, verbose=False, **kwargs):
        Switch.__init__(self, name, **kwargs)
        self.verbose = verbose

    @classmethod
    def setup(cls):
        "Make sure IVS is installed"
        pathCheck('ivs-ctl', 'ivs',
                  moduleName="Indigo Virtual Switch (projectfloodlight.org)")
        out, err, exitcode = errRun('ivs-ctl show')
        if exitcode:
            error(out + err +
                  'ivs-ctl exited with code %d\n' % exitcode +
                  '*** The openvswitch kernel module might '
                  'not be loaded. Try modprobe openvswitch.\n')
            exit(1)

    @classmethod
    def batchShutdown(cls, switches):
        "Kill each IVS switch, to be waited on later in stop()"
        for switch in switches:
            switch.cmd('kill %ivs')
        return switches

    def start(self, controllers):
        "Start up a new IVS switch"
        args = ['ivs']
        args.extend(['--name', self.name])
        args.extend(['--dpid', self.dpid])
        if self.verbose:
            args.extend(['--verbose'])
        for intf in self.intfs.values():
            if not intf.IP():
                args.extend(['-i', intf.name])
        for c in controllers:
            args.extend(['-c', '%s:%d' % (c.IP(), c.port)])
        if self.listenPort:
            args.extend(['--listen', '127.0.0.1:%i' % self.listenPort])
        args.append(self.opts)

        logfile = '/tmp/ivs.%s.log' % self.name

        self.cmd(' '.join(args) + ' >' + logfile + ' 2>&1 </dev/null &')

    def stop(self, deleteIntfs=True):
        """Terminate IVS switch.
           deleteIntfs: delete interfaces? (True)"""
        self.cmd('kill %ivs')
        self.cmd('wait')
        super(IVSSwitch, self).stop(deleteIntfs)

    def attach(self, intf):
        "Connect a data port"
        self.cmd('ivs-ctl', 'add-port', '--datapath', self.name, intf)

    def detach(self, intf):
        "Disconnect a data port"
        self.cmd('ivs-ctl', 'del-port', '--datapath', self.name, intf)

    def dpctl(self, *args):
        "Run dpctl command"
        if not self.listenPort:
            return "can't run dpctl without passive listening port"
        return self.cmd('ovs-ofctl ' + ' '.join(args) +
                        ' tcp:127.0.0.1:%i' % self.listenPort)


class Controller(Node):
    """A Controller is a Node that is running (or has execed?) an
       OpenFlow controller."""

    def __init__(self, name, inNamespace=False, command='controller',
                 cargs='ptcp:%d', cdir=None, ip="127.0.0.1",
                 port=6653, protocol='tcp', verbose=False, **params):
        self.command = command
        self.cargs = cargs
        if verbose:
            cargs = '-v ' + cargs
        self.cdir = cdir
        # Accept 'ip:port' syntax as shorthand
        if ':' in ip:
            ip, port = ip.split(':')
            port = int(port)
        self.ip = ip
        self.port = port
        self.protocol = protocol
        Node.__init__(self, name, inNamespace=inNamespace,
                      ip=ip, **params)
        self.checkListening()

    def checkListening(self):
        "Make sure no controllers are running on our port"
        # Verify that Telnet is installed first:
        out, _err, returnCode = errRun("which telnet")
        if 'telnet' not in out or returnCode != 0:
            raise Exception("Error running telnet to check for listening "
                            "controllers; please check that it is "
                            "installed.")
        listening = self.cmd("echo A | telnet -e A %s %d" %
                             (self.ip, self.port))
        if 'Connected' in listening:
            servers = self.cmd('netstat -natp').split('\n')
            pstr = ':%d ' % self.port
            clist = servers[0:1] + [s for s in servers if pstr in s]
            raise Exception("Please shut down the controller which is"
                            " running on port %d:\n" % self.port +
                            '\n'.join(clist))

    def start(self):
        """Start <controller> <args> on controller.
           Log to /tmp/cN.log"""
        pathCheck(self.command)
        cout = '/tmp/' + self.name + '.log'
        if self.cdir is not None:
            self.cmd('cd ' + self.cdir)
        self.cmd(self.command + ' ' + self.cargs % self.port +
                 ' 1>' + cout + ' 2>' + cout + ' &')
        self.execed = False

    # pylint: disable=arguments-differ,signature-differs
    def stop(self, *args, **kwargs):
        "Stop controller."
        self.cmd('kill %' + self.command)
        self.cmd('wait %' + self.command)
        super(Controller, self).stop(*args, **kwargs)

    def IP(self, intf=None):
        "Return IP address of the Controller"
        if self.intfs:
            ip = Node.IP(self, intf)
        else:
            ip = self.ip
        return ip

    def __repr__(self):
        "More informative string representation"
        return '<%s %s: %s:%s pid=%s> ' % (
            self.__class__.__name__, self.name,
            self.IP(), self.port, self.pid)

    @classmethod
    def isAvailable(cls):
        "Is controller available?"
        return which('controller')


class OVSController(Controller):
    "Open vSwitch controller"

    def __init__(self, name, **kwargs):
        kwargs.setdefault('command', self.isAvailable() or
                          'ovs-controller')
        Controller.__init__(self, name, **kwargs)

    @classmethod
    def isAvailable(cls):
        return (which('ovs-controller') or
                which('test-controller') or
                which('ovs-testcontroller'))


class NOX(Controller):
    "Controller to run a NOX application."

    def __init__(self, name, *noxArgs, **kwargs):
        """Init.
           name: name to give controller
           noxArgs: arguments (strings) to pass to NOX"""
        if not noxArgs:
            warn('warning: no NOX modules specified; '
                 'running packetdump only\n')
            noxArgs = ['packetdump']
        elif not isinstance(noxArgs, (list, tuple)):
            noxArgs = [noxArgs]

        if 'NOX_CORE_DIR' not in os.environ:
            exit('exiting; please set missing NOX_CORE_DIR env var')
        noxCoreDir = os.environ['NOX_CORE_DIR']

        Controller.__init__(self, name,
                            command=noxCoreDir + '/nox_core',
                            cargs='--libdir=/usr/local/lib -v '
                            '-i ptcp:%s ' +
                            ' '.join(noxArgs),
                            cdir=noxCoreDir,
                            **kwargs)


class Ryu(Controller):
    "Ryu OpenFlow Controller"

    def __init__(self, name, ryuArgs='ryu.app.simple_switch',
                 command='ryu run', **kwargs):
        """Init.
           name: name to give controller.
           ryuArgs: modules to pass to Ryu (ryu.app.simple_switch)
           command: comand to run Ryu ('ryu run')"""
        if isinstance(ryuArgs, (list, tuple)):
            ryuArgs = ' '.join(ryuArgs)
        cargs = kwargs.pop(
            'cargs', ryuArgs + ' --ofp-tcp-listen-port %s')
        Controller.__init__(self, name, command=command,
                            cargs=cargs, **kwargs)


class RemoteController(Controller):
    "Controller running outside of Mininet's control."

    def __init__( self, name, ip='127.0.0.1',
                  port=None, **kwargs ):
        """Init.
           name: name to give controller
           ip: the IP address where the remote controller is
           listening
           port: the port where the remote controller is listening"""
        Controller.__init__(self, name, ip=ip, port=port, **kwargs)

    def start(self):
        "Overridden to do nothing."
        return

    # pylint: disable=arguments-differ
    def stop(self):
        "Overridden to do nothing."
        return

    def checkListening(self):
        "Warn if remote controller is not accessible"
        if self.port is not None:
            self.isListening(self.ip, self.port)
        else:
            for port in 6653, 6633:
                if self.isListening(self.ip, port):
                    self.port = port
                    info("Connecting to remote controller"
                         " at %s:%d\n" % (self.ip, self.port))
                    break

        if self.port is None:
            self.port = 6653
            warn("Setting remote controller"
                 " to %s:%d\n" % (self.ip, self.port))

    def isListening(self, ip, port):
        "Check if a remote controller is listening at a specific ip and port"
        listening = self.cmd("echo A | telnet -e A %s %d" % (ip, port))
        if 'Connected' not in listening:
            warn("Unable to contact the remote controller"
                 " at %s:%d\n" % (ip, port))
            return False
        else:
            return True


DefaultControllers = (Controller, OVSController)


def findController(controllers=DefaultControllers):
    "Return first available controller from list, if any"
    for controller in controllers:
        if controller.isAvailable():
            return controller
    return None


def DefaultController(name, controllers=DefaultControllers, **kwargs):
    "Find a controller that is available and instantiate it"
    controller = findController(controllers)
    if not controller:
        raise Exception('Could not find a default OpenFlow controller')
    return controller(name, **kwargs)


def NullController(*_args, **_kwargs):
    "Nonexistent controller - simply returns None"
    return None

class VNode( object ):
    """ A vNode (Virtualized Node) is a Virtual Machine, connected to 
    mininet via an OpenVSwitch bridge. """

    def __init__( self, name, domTree, **kwargs ):
        self.name = name
        self.domID = None
        self.hyperv = self.getHypervisor( domTree )
        self.mac = kwargs.get( 'mac' )
        self.links = kwargs.get( 'links' )
        self.docker = kwargs.get( 'docker' )
        self.switches = kwargs.get( 'switches' )
        self.container_id = kwargs.get( 'container_id' )
        self.dockerswitches = kwargs.get( 'dockerswitches' )
        if self.container_id is not None:
            self.rename( domTree, self.container_id )
        if self.links is not None:
            for i in self.links:
                if self.docker == True:
                    i = self.container_id[0:6] + '-' + i + '-' + self.name
                self.insertInterface( domTree, mac_addr=self.mac, switch=i )

        self.XML = ET.tostring( domTree.getroot(), encoding="utf-8", method="xml").decode("utf8")
        self.hasStarted = False

    def start( self ):
        conn = self.getLibvirtConn()
        dom = conn.createXML( self.XML, 0 )
        "Start the VM using libvirt api"
        if dom == None and dom.ID() != -1:
            return None
        else:
            self.domID = dom.ID()
            self.hasStarted = True        
            if self.dockerswitches is not None:
                self.attachVethHost()

    def terminate( self ):
        "Terminate the VM using libvirt api"
        conn = self.getLibvirtConn()
        dom = conn.lookupByID( self.domID )
        if dom != None:
            dom.destroy()
        else:
            debug( "Unable to find VM: {}".format( self.name ) )
        conn.close()

    def getHypervisor( self, domTree ):
        "Return hypervisor name: kvm or xen"
        root = domTree.getroot()
        hypervisor = root.get( 'type' )
        if hypervisor.upper() == 'XEN':
            return 'xen'
        elif hypervisor.upper() == 'KVM':
            return 'kvm'
        return None

    def rename( self, domTree, container_id ):
        root = domTree.getroot()
        name = root.find( 'name' )
        name.text= 'mtv-' + container_id + '-' + self.name

    def insertInterface( self, domTree, switch, mac_addr=None, docker_prefix=None ):
        root = domTree.getroot()
        devices = root.find( 'devices' )
        l = ET.SubElement( devices, 'interface' )
        l.set( 'type', 'bridge' )
        source = ET.SubElement( l, 'source' )
        source.set( 'bridge', switch )
        #target = ET.SubElement( l, 'target' )
        #target.set( 'dev', self.name )
        virtualport = ET.SubElement( l, 'virtualport' )
        virtualport.set( 'type', 'openvswitch' )
        if mac_addr is not None:
            mac = ET.SubElement( l, 'mac' )
            mac.set( 'address', str( mac_addr ) )
        ET.dump(domTree.getroot())

    def _is_machine_running( self ):
        "Returns machine state using libvirt api"
        conn = self.getLibvirtConn()
        if conn == None:
            debug( 'Failed to open connection to libvirtd' )
        else:
            dom = conn.lookupByID( self.domID )
            flag = dom.isActive()
            if dom == None:
                debug( 'Failed to find domain' )
            else:
                if flag == True:
                    debug( 'The domain is active' )
                else:
                    debug( 'The domain is not active')
        conn.close()

    def getLibvirtConn( self ):
        "Return connection to hypervisor with libvirt api"
        hyperv = self.hyperv
        if self.hyperv == None:
            debug('Error: Hypervisor info was not extracted')
            return False
        try:
            if hyperv == 'kvm':
                hyperv = 'qemu'
            conn = libvirt.open( '{}+unix:///'.format( hyperv ) )
            return conn
        except:
            debug( 'Failed to acquire connection to hypervisor' )
            sys.exit(1)

    def attachVethHost( self ):
        for i in self.switches:
            vethname = i + "." + self.name
            disappointment = subprocess.run(['ovs-vsctl', 'add-port', i, vethname], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def attachVethSwitch( self, switch_name ):
        vethname = switch_name + "." + self.name
        connect = subprocess.run(['ovs-vsctl', 'add-port', switch_name, vethname])

    def attachVeth( self, host_name ):
        vethname = host_name + "." + self.name
        connect = subprocess.run([''])

class DHCPNode( Node ):

    def __init__( self, mac, ipaddr, ipnet, switch, **params ):
        """
        """
        self.iprange = str(ipnet[2])+','+str(ipnet[-2])
        self.mac = mac
        self.ipaddr = ipaddr
        self.switch = switch
        self.netmask = str(ipnet.netmask)
        self.defaults = {
            'leasetime': '1h',
            'conffile': '/etc/mtv/dnsconf',
            'hostsfile': '/etc/mtv/hosts',
            'leasefile': '/etc/mtv/leases',
            'switchname': 's1'
        }
        self.defaults.update( params )
        nodeopts = {
            'ip': ipaddr,
            'mac': mac
        }
        Node.__init__( self, name='dnsmasq', **nodeopts )
        self.dnsmasqPopen = None
        self.dhcptable = {}

        #Conffile Configuration
        if os.path.exists( self.defaults[ 'conffile' ] ):
            os.remove( self.defaults[ 'conffile' ] )
        f = open( self.defaults[ 'conffile' ], 'a+' )
        f.close()

    def start( self, **params ):
        "Start dnsmasq instance inside namespace"
        opts = [ 'dnsmasq' ]
        opts.append( '--port=0' )
        opts.append( '--dhcp-range={},{}'.format( self.iprange, self.netmask ) )
        opts.append( '--conf-file={}'.format( self.defaults[ 'conffile' ] ) )
        opts.append( '--dhcp-leasefile={}'.format( self.defaults[ 'leasefile' ] ) )
        self.dnsmasqPopen = self.popen( opts )
        out, err = self.dnsmasqPopen.communicate()
        debug(out)
        exitcode = self.dnsmasqPopen.wait()

    def update( self ):
        if self.dnsmasqPopen != None:
            self.dnsmasqPopen.kill()
            self.start()

    def addHost( self, mac, ip, expiry=1, hostname="*", clientID="*" ):
        ipnocidr = ip.split('/')[0] 
        with open( self.defaults[ 'conffile' ], 'r+' ) as hostsconf:
            text = hostsconf.read()
        with open( self.defaults[ 'conffile' ], 'a' ) as conf:
            if not text.endswith( '\n' ):
                conf.write( '\n' )
            conf.write( 'dhcp-host={},{}'.format( mac, ipnocidr ) )
        self.dhcptable[ self.name ] = {
            'mac': mac,
            'ip': ip
        }
        self.update()

    def getIP( self, mac ):
        with open( self.defaults[ 'leasefile' ], 'r+' ) as hostsconf:
            for line in hostsconf:
                if mac in line:
                    return line.split(" ")[2]
        return None


class DynamipsRouter(Switch):
    """A router that runs an instance of dynamip.

    This inhertits from :class:`Switch` so it requires having its interfaces
    assigned IP addresses
    """

    def __init__(
        self,
        *args,
        dynamips_platform,
        dynamips_image,
        dynamips_args,
        dynamips_config=None,
        dynamips_port_driver=None,
        dynamips_ports=None,
        **kwargs
    ):
        """
        dynamips_platform: (str) The type of platform to emulate.
        dynamips_image: (str | file like) The file path, or file of the Cisco IOS to use.
        dynamips_port_driver: (Optional[tuple[str, str, int]]) The port adapter / network
            module driver to use, it's name when configuring, and how many ports it has.
            Mutually exclusive with `dynamips_config` and `dynamips_ports`.
        dynamips_args: (list[str]) Parameters to pass to dynamips.
        dynamips_config: (Optional[str]) The config file to pass to dynamips.
            If this is passed, no config will be auto-generated.
            Mutually exclusive with `dynamips_port_driver`.
            `dynamips_ports` must also be set if this is used.
        dynamips_ports: (list[tuple[str, str, int, list[tuple[int, str]]]]) The ports that will exist
            on this dynamips instance, as a tuple of (Driver Name, Interface Name, Slot, [port number, link dst])
            These should be given if the ports/interfaces for the instance have been decided
            beforehand.
            Mutually exclusive with `dynamips_port_driver`.
            `dynamips_config` must also be set if this is used.
        """

        self.dynamips_platform = dynamips_platform
        try:
            self.dynamips_image = self._process_image(dynamips_image)
        except Exception as e:
            raise Exception(
                "We failed to process the provided dynamips image: {}".format(
                    dynamips_image
                )
            ) from e

        self.dynamips_args = dynamips_args

        # type: dict[tuple[int, int], str]
        # map of (slot_idx, port_idx) -> tap device name
        self.emu_port_taps = dict()

        # type: dict[str, str]
        # map of tap device name -> veth device name
        # self.tap_veths = dict()

        # type: dict[tuple[int, int], Intf]
        # map of (slot_idx, port_idx) -> mininet interface
        self.emu_port_intfs = dict()

        # type: list[tuple[str, int]]
        # the port drivers used, a list of pairs of the driver name and slot number
        self.port_drivers = []

        # type: list[str]
        # The names of bridges used by this instance
        self.bridges = []

        assert (dynamips_ports is not None) == (
            dynamips_config is not None
        ), "Setting one of these parameters requires setting the other"
        assert ((dynamips_ports is not None) or (dynamips_config is not None)) ^ (
            dynamips_port_driver is not None
        ), "These should be mutually exclusive"

        self.dynamips_config = dynamips_config
        self.dynamips_ports = dynamips_ports
        if dynamips_ports is not None:
            self.dynamips_link_to_port = {
                other_side: (pd, intf, slot, port)
                for pd, intf, slot, other_sides in dynamips_ports
                for (port, other_side) in other_sides
            }
            self.port_drivers = [(pd, slot) for pd, _, slot, _ in dynamips_ports]

        if dynamips_port_driver is not None:
            self.dynamips_port_driver_key = dynamips_port_driver[0]
            self.dynamips_port_driver_conf_key = dynamips_port_driver[1]
            self.dynamips_port_driver_ports = dynamips_port_driver[2]

        super().__init__(*args, **kwargs)

    _mapped_images = {}
    _tmp_image_files = []
    _tap_count = 0
    _bridge_count = 0

    @classmethod
    def get_tap(cls):
        n = cls._tap_count
        cls._tap_count += 1

        return "tap-dyna-{}".format(n)

    @classmethod
    def get_bridge(cls):
        n = cls._bridge_count
        cls._bridge_count += 1

        return "br-dyna-{}".format(n)

    @classmethod
    def _cleanup_on_exit(cls):
        for file in cls._tmp_image_files:
            file.close()

    @classmethod
    def _process_image(cls, dynamips_image):
        image = cls._mapped_images.get(dynamips_image)
        if image is not None:
            return image

        if isinstance(dynamips_image, io.BufferedIOBase):
            tmp_file = tempfile.NamedTemporaryFile()
            dynamips_image.seek(0)
            shutil.copyfileobj(dynamips_image, tmp_file)

            cls._mapped_images[dynamips_image] = tmp_file.name
            cls._tmp_image_files.append(tmp_file)

            return tmp_file.name

        # assume file name/ path
        with open(dynamips_image) as _:
            # just checking it exists
            cls._mapped_images[dynamips_image] = dynamips_image
            return dynamips_image

    @staticmethod
    def _find_free_port():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("", 0))
        _, port = s.getsockname()
        s.close()
        return port

    def port_gen(self):
        """A generator that yields tuples of (slot_number, port_number) while
        keeping track of how many port drivers have been used.
        """
        for pd_idx in count(1):
            self.port_drivers.append((self.dynamips_port_driver_key, pd_idx))
            for port_idx in range(self.dynamips_port_driver_ports):
                yield (pd_idx, port_idx)

    # def port_process(self):
    #     """A generator that yields tuples of (slot_number, port_number).
    #     This is used when the router config is provided
    #     """
    #     for pd, intf, slot, links in self.dynamips_ports:

    def emu_port_for_intf(self, intf):
        if intf.link.intf1.node is self:
            other_side = intf.link.intf2.node
        else:
            other_side = intf.link.intf1.node

        _, _, slot, port = self.dynamips_link_to_port[other_side.name]
        return (slot, port)

    def start(self, controllers):
        if self.dynamips_config is None:
            pg = self.port_gen()
        else:
            pg = None

        for intf, port in self.ports.items():
            if intf.name == "lo":
                continue

            if pg is not None:
                emu_port = next(pg)
            else:
                emu_port = self.emu_port_for_intf(intf)

            tap_name = self.get_tap()
            bridge_name = self.get_bridge()

            intf.cmd("ip tuntap add mode tap {}".format(tap_name))
            intf.cmd("ip link set dev {} up".format(tap_name))
            intf.cmd("ip link add {} type bridge".format(bridge_name))
            intf.cmd("ip link set {} master {}".format(tap_name, bridge_name))
            intf.cmd("ip link set {} master {}".format(intf.name, bridge_name))
            intf.cmd("ip link set dev {} up".format(bridge_name))

            # sometimes iptables likes to hurt us
            intf.cmd("iptables -I FORWARD -p all -i {} -j ACCEPT".format(bridge_name))

            # self.tap_veths[tap_name] = intf.name
            self.emu_port_intfs[emu_port] = intf
            self.emu_port_taps[emu_port] = tap_name
            self.bridges.append(bridge_name)

        self._output_file = tempfile.NamedTemporaryFile()
        self._config_file = tempfile.NamedTemporaryFile(mode="w+")

        if self.dynamips_config:
            self._config_file.write(self.dynamips_config)
        else:
            self._config_file.write(self.make_config())
        self._config_file.flush()

        self._working_dir = tempfile.TemporaryDirectory()

        # print(self.config_file.name)

        self._console_port = self._find_free_port()

        args = " ".join(self.dynamips_args)
        port_drivers = " ".join(
            "-p {}:{}".format(n, name) for name, n in self.port_drivers
        )
        taps = " ".join(
            "-s {}:{}:tap:{}".format(slot, port, tap_name)
            for (slot, port), tap_name in self.emu_port_taps.items()
        )
        cmd = "dynamips -T {} -C {} -P {} {} {} {} {}".format(
            self._console_port,
            self._config_file.name,
            self.dynamips_platform,
            args,
            port_drivers,
            taps,
            self.dynamips_image,
        )

        self._cmd = cmd
        print(cmd)

        self.process = self.popen(
            cmd,
            cwd=self._working_dir.name,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    @staticmethod
    def _buffer_socket_to_lines(s: socket.socket):
        s.settimeout(0.1)
        buf = io.BytesIO()

        while True:
            try:
                chunk = s.recv(2048)
                buf.write(chunk)
                buf.seek(0)

                lines = buf.readlines()
                last = lines.pop() if lines else b""
                buf = io.BytesIO(last)

                for line in lines:
                    yield line.strip()
            except socket.timeout:
                for line in buf:
                    yield line.strip()
                break

    def connected(self):
        try:
            with socket.create_connection(("127.0.0.1", self._console_port)) as s:
                # with open(self._output_file.name) as f:
                for line in self._buffer_socket_to_lines(s):
                    # this seems like a bit of a hack. but the router is likely ready
                    # as soon as it prints the welcome message.
                    if b"Press RETURN to get started" in line:
                        return True
        except ConnectionRefusedError:
            pass
        return False

    def make_config(self) -> str:
        out = [
            "hostname {}".format(self.name),
            "ip routing",
        ]

        out.extend(["router rip", "version 2"])

        for (slot, port), intf in self.emu_port_intfs.items():
            if intf.ip is None or intf.prefixLen is None:
                raise Exception(
                    "The interface {} of node {} is lacking an assigned ip address.".format(
                        intf, self
                    )
                )
            net = ipaddress.ip_interface("{}/{}".format(intf.ip, intf.prefixLen))
            out.append("network {}".format(net.network.network_address))

        for (slot, port), intf in self.emu_port_intfs.items():
            (intf_mask, _) = ipaddress.IPv4Network._make_netmask(intf.prefixLen)
            out.extend(
                [
                    "interface {} {}/{}".format(
                        self.dynamips_port_driver_conf_key, slot, port
                    ),
                    "ip address {} {}".format(intf.ip, intf_mask),
                    "no shut",
                ]
            )

        out.append("end")

        return "\n".join(out)

    @classmethod
    def setup(cls):
        "Ensure any dependencies are loaded; if not, try to load them."
        if not os.path.exists("/dev/net/tun"):
            moduleDeps(add=TUN)

    def stop(self, deleteIntfs=True):
        print("bye")

        try:
            self.process.kill()
            self._config_file.close()
            self._output_file.close()
            self._working_dir.cleanup()
        except AttributeError:
            pass

        if deleteIntfs:
            for bridge_name in self.bridges:
                self.cmd("ip link delete {}".format(bridge_name))

            for tap_name in self.emu_port_taps.values():
                self.cmd("ip link delete {}".format(tap_name))

        return super().stop(deleteIntfs=deleteIntfs)


atexit.register(DynamipsRouter._cleanup_on_exit)

class Docker():

    def __init__( self, name, dimage=None, dcmd=None, build_params={}, **kwargs):
        self.name = name
        self.dimage = dimage
        self.dnameprefix = "mtv"
        self.dcmd = dcmd if dcmd is not None else ""
        self.build_params = build_params
        self.dcinfo = None
        self.did = None
        self.pid = None
        self.ip = kwargs.get("ip")
        self.switches = kwargs.get("switches")
        self.container_id = kwargs.get("container_id")
        self.dc = docker.DockerClient(base_url='unix://var/run/docker.sock')
        self.d_api = self.dc.api
        self.config = {
            'cpu_quota': -1,
            'cpu_period': None,
            'cpu_shares': None,
            'cpuset_cpus': None,
            'mem_limit': None,
            'memswap_limit': None,
            'environment': {},
            'volumes': [],
            'tmpfs': [],
            'network_mode': None,
            'publish_all_ports': True,
            'port_bindings': {},
            'ports': [],
            'dns': [],
            'ipc_mode': None,
            'devices': [],
            'cap_add': ['net_admin'],  # we need this to allow mininet network setup
            'storage_opt': None,
            'sysctls': {},
            'docker_sock': None
        }
        self.config.update( kwargs )

        if 'net_admin' not in self.config['cap_add']:
            self.config['cap_add'] += ['net_admin']

        if self.config['docker_sock'] is None:
            self.config['docker_sock'] = '/var/run/docker.sock'
            debug("Warning: Docker Sock not specified. Using default location")
            debug("If you're running MTV inside a docker container, you MUST pass the docker socket")

        _id = None
        if self.build_params.get("path", None):
            if not self.build_params.get("tag", None):
                if self.dimage:
                    self.build_params["tag"] = dimage
            _id, output = self.build(**self.build_params)
            self.dimage = _id
            info("Docker image built: id: {},  {}. Output:\n".format(
                _id, self.build_params.get("tag", None)))
            info(output)

        hc = self.d_api.create_host_config(
            network_mode=self.config.get('network_mode'),
            privileged=False,
            binds=self.config.get('volumes'),
            tmpfs=self.config.get('tmpfs'),
            publish_all_ports=self.config.get('publish_all_ports'),
            port_bindings=self.config.get('port_bindings'),
            mem_limit=self.config.get('mem_limit'),
            cpuset_cpus=self.config.get('cpuset_cpus'),
            dns=self.config.get('dns'),
            ipc_mode=self.config.get('ipc_mode'),
            devices=self.config.get('devices'),
            cap_add=self.config.get('cap_add'),  
            sysctls=self.config.get('sysctls'),
            storage_opt=self.config.get('storage_opt')
        )

        self.dc = self.d_api.create_container(
            name="%s.%s-%s" % (self.dnameprefix, name, self.container_id),
            image=self.dimage,
            command=self.dcmd,
            entrypoint=list(), 
            stdin_open=True, 
            tty=True,
            environment=self.config.get('environment'),
            network_disabled=True,
            host_config=hc,
            ports=self.config.get('ports'),
            labels=[],
            volumes=None,
            hostname=name
        )

    def build( self ):
        if self.config.get("rm", False):
            container_list = self.d_api.containers(all=True)
            for container in container_list:
                for container_name in container.get("Names", []):
                    if "%s.%s" % (self.dnameprefix, self.name) in container_name:
                        self.d_api.remove_container(container="%s.%s" % (self.dnameprefix, name), force=True)
                        break
        return None

    def start( self ):
        res = self.d_api.start( container=self.dc.get( 'Id' ) )
        debug( "Docker container %s started\n" % self.name )
        self.dcinfo = self.d_api.inspect_container( self.dc )
        self.did = self.dcinfo.get( "Id" )
        pid = self.docker_pid()
        sylink = sp_run( [ "ln", "-sf", "/proc/%s/ns/net" % (pid), "/var/run/netns/%s" % (self.did) ], 
            stdout=PIPE, stderr=PIPE )
        debug(sylink)
        if self.switches is not None:
            for switch_name in self.switches:
                veth_docker = "%s-%s" % ( self.did[0:4], switch_name )
                veth_switch= "%s-%s" % ( switch_name, self.did[0:4] )
                addVeth = sp_run( [ "ip", "link", "add", veth_docker, "type", "veth", "peer", 
                                  "name", veth_switch], stdout=PIPE, stderr=PIPE )
                debug(addVeth)
                sp_run(["ip", "link", "set", veth_switch, "up"])
                setDVeth = sp_run( [ "ip", "link", "set", veth_docker, "netns", str(self.did) ], 
                    stdout=PIPE, stderr=PIPE )
                debug(setDVeth)
                sp_run(["ip", "netns", "exec", str(self.did), "ip", "link", "set", veth_docker, "up"])
                vethaddr = sp_run(["ip", "netns", "exec", str(self.did), "ip", "addr", "add", str(self.ip), "dev", veth_docker], stdout=PIPE, stderr=PIPE)
                debug(vethaddr)
                setSVeth = sp_run( [ "ovs-vsctl", "add-port", switch_name, veth_switch ], 
                    stdout=PIPE, stderr=PIPE )
                debug(setSVeth)
        return None

    def terminate( self ):
        self.dc.stop()
        self.dc.remove()
        return None

    def docker_pid( self ):
        debug("\n\nPID {}\n\n".format(self.did))
        inspection = self.d_api.inspect_container(self.did)['State']['Pid']
        if inspection is not None:
            debug("INSPECTION\n\n {}".format(inspection))
            return inspection

    def sendCmd( self, string ):
        return None

        
