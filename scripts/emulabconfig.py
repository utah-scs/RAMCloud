#!/usr/bin/env python
"""
This module defines an Emulab specific cluster hooks and exposes configuration information
such as location of RAMCloud binaries and list of hosts. EMULAB_HOST environment must be 
exported to point to node-0 in the Cloudlab experiment where the caller has passwordless
ssh access. Check https://www.cloudlab.us/ssh-keys.php if you didn't export keys already

Sample localconfig.py 
from emulabconfig import *
#For DPDK builds, use dpdk=True and provide --prefix_bin=sudo to clusterperf
hooks = EmulabClusterHooks(dpdk=False, alwaysclean=False, makeflags='-j12 DEBUG=no');
hosts = getHosts()
server_hosts = getHosts(serversOnly=True)
other_hosts = getHosts(othersOnly=True)
"""

import subprocess
import sys
import os
import re
import socket
import xml.etree.ElementTree as ET

__all__ = ['getHosts', 'checkHost', 'local_scripts_path', 'top_path', 'obj_path',
           'default_disk1', 'default_disk2', 'EmulabClusterHooks', 'log'] 

hostname = socket.gethostname()

def log(msg):
    print '[%s] %s' % (hostname, msg)


# If run locally, connects to EMULAB_HOST and gets the manifest from there to
# populate host list, since this is invoked to compile RAMCloud (rawmetrics.py)
# the default is to see if you can get the manifest locally
def getHosts(serversOnly=False, othersOnly=False):
    if serversOnly and othersOnly:
        sys.exit("Can't user serversOnly and othersOnly together")
    nodeId = 0
    serverList = []
    try:
        log("trying to get manifest locally")
        out = captureSh("/usr/bin/geni-get manifest",shell=True, stderr=subprocess.STDOUT)
    except:
        log("trying EMULAB_HOST to get manifest")
        if 'EMULAB_HOST' not in os.environ:
            log("'EMULAB_HOST' not exported")
            sys.exit(1)
        out = subprocess.check_output("ssh %s /usr/bin/geni-get manifest" % os.environ['EMULAB_HOST'],
                                       shell=True, stderr=subprocess.STDOUT)

    root = ET.fromstring(out)
    for child in root.getchildren():
        if child.tag.endswith('node'):
            for host in child.getchildren():
                if host.tag.endswith('host'):
                    serverList.append((host.attrib['name'], host.attrib['ipv4'], nodeId))
                    nodeId += 1
    #for mixed profiles with server-* and client-* nodes
    if serversOnly:
        serverList = [server for server in serverList if server[0].startswith("server")]
    if othersOnly:
        serverList = [client for client in serverList if client[0].startswith("client")]
    return serverList

def checkHost(host):
    serverList = getHosts()
    for server in serverList:
        if host == server[0]:
            return True
    raise Exception("Attempted host %s not found in localconfig" % host)

def ssh(server, cmd, checked=True):
    """ Runs command on a remote machine over ssh.""" 
    if checked:
        return subprocess.check_call('ssh %s "%s"' % (server, cmd),
                                     shell=True, stdout=sys.stdout)
    else:
        return subprocess.call('ssh %s "%s"' % (server, cmd),
                               shell=True, stdout=sys.stdout)


def pdsh(cmd, checked=True):
    """ Runs command on remote hosts using pdsh on remote hosts"""
    log("Running parallely on all hosts")
    if checked:
        return subprocess.check_call('pdsh -w^./.emulab-hosts "%s"' % cmd,
                                     shell=True, stdout=sys.stdout)
    else:
        return subprocess.call('pdsh -w^./.emulab-hosts "%s"' %cmd,
                               shell=True, stdout=sys.stdout)

def captureSh(command, **kwargs):
    """Execute a local command and capture its output."""

    kwargs['shell'] = True
    kwargs['stdout'] = subprocess.PIPE
    p = subprocess.Popen(command, **kwargs)
    output = p.communicate()[0]
    if p.returncode:
        raise subprocess.CalledProcessError(p.returncode, command)
    if output.count('\n') and output[-1] == '\n':
        return output[:-1]
    else:
        return output

try:
    git_branch = re.search('^refs/heads/(.*)$',
                           captureSh('git symbolic-ref -q HEAD 2>/dev/null'))
except subprocess.CalledProcessError:
    git_branch = None
    obj_dir = 'obj'
else:
    git_branch = git_branch.group(1)
    obj_dir = 'obj.%s' % git_branch

# Command-line argument specifying where the server should store the segment
# replicas when one device is used.
default_disk1 = '-f /dev/sda4'

# Command-line argument specifying where the server should store the segment
# replicas when two devices are used.
default_disk2 = '-f /dev/sda4,/dev/sdb'

class EmulabClusterHooks:
    def __init__(self, dpdk=False, alwaysclean=False, makeflags=''):
        log("NOTICE: running with dpdk=%s, alwaysclean=%s, makeflags=%s" % (dpdk, alwaysclean, makeflags))
        self.remotewd = None
        self.hosts = getHosts()
        self.clean = alwaysclean
        self.dpdk = dpdk
        self.server_hosts = getHosts(serversOnly=True)
        self.other_hosts = getHosts(othersOnly=True)
        if dpdk:
            self.makeflags = 'DPDK=yes DPDK_DIR=/local/RAMCloud/deps/dpdk-16.07 ' + makeflags
            self.check_hugepages();
        else:
            self.makeflags = makeflags
        self.parallel = self.cmd_exists("pdsh")
        for host in self.hosts:
            if not self.cmd_exists("numactl",server=host[0]):
                log("WARNING: numactl not installed on %s. install numactl"
                    " and provide --numactl flag for multisocket machines" % host[0])
        if not self.parallel:
            log("NOTICE: Remote commands could be faster if you install and configure pdsh")
            self.remote_func = self.serial
        else:
            with open("./.emulab-hosts",'w') as f:
                for host in self.hosts:
                    f.write(host[0]+'\n')
            self.remote_func = pdsh

    def cmd_exists(self, cmd, server=None):
        if server is None:
            return subprocess.call("type " + cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) == 0
        else:
            return ssh(server, "type %s > /dev/null 2>&1" % cmd, checked=False) == 0
                    
    def check_hugepages(self):
        for host in self.hosts:
            try:
                num_hugepages = subprocess.check_output("ssh %s cat /sys/kernel/mm/hugepages/hugepages-1048576kB/nr_hugepages " % host[0],
                                                        shell=True, stderr=subprocess.STDOUT)
                num_hugepages = int(num_hugepages)
            except subprocess.CalledProcessError:
                num_hugepages = 0
            if num_hugepages < 2:
                sys.exit("At least 2 1GB hugepages required for DPDK. Didn't find enough on %s" % host[0])
            try:
                mounted = subprocess.check_output("ssh %s mount | grep pagesize=1G" % host[0], shell=True, stderr=subprocess.STDOUT)
            except subprocess.CalledProcessError:
                mounted = None
            if mounted is None or "hugetlbfs" not in mounted:
                sys.exit("DPDK requires 1GB hugepages mounted. Couldn't find any on %s" % host[0])

    def serial(self, cmd, checked=True):
        for host in self.hosts:
            log("Running on %s" % host[0])
            ssh(host[0], cmd, checked=checked)

    def get_remote_wd(self):
        if self.remotewd is None:
            self.remotewd = os.path.join(captureSh('ssh %s pwd' % self.hosts[0][0]),
                                                   'RAMCloud')
        return self.remotewd

    def get_remote_scripts_path(self):
        return os.path.join(self.get_remote_wd(), 'scripts')

    def get_remote_obj_path(self):
        return os.path.join(self.get_remote_wd(), obj_dir)

    def send_code(self):
        for host in self.hosts:
            log("Sending code to %s" % host[0])
            subprocess.check_call("rsync -ave ssh --exclude 'logs/*' " +
                              "--exclude 'docs/*' " +
                              "./ %s:%s/ > /dev/null" % (host[0],
                                                         self.get_remote_wd()),
                              shell=True, stdout=sys.stdout)
    
    def compile_code(self, clean=False):
        log("Compiling code")
        clean_cmd = ''
        if clean:
            clean_cmd = 'make clean;'
        self.remote_func('(cd %s; %s make %s  > %s/build.log 2>&1)' % (self.get_remote_wd(),
                          clean_cmd, self.makeflags, self.get_remote_wd()))

    def kill_procs(self):
        log("Killing existing processes")
        #DPDK refuses to die for some reason
        if self.dpdk:
            for i in range(3):
                log("Killing DPDK RAMCloud processes. Try:%s" % str(i+1))
                try:
                    self.remote_func('sudo pkill -f RAMCloud')
                except subprocess.CalledProcessError:
                    pass
        else:
            try:
                self.remote_func('pkill -f RAMCloud')
            except subprocess.CalledProcessError:
                pass

    def create_log_dir(self):
        log("creating log directories")
        self.remote_func(
            '(cd %s; ' % self.get_remote_wd() +
            'mkdir -p $(dirname %s)/shm; ' % self.cluster.log_subdir +
            'mkdir -p %s; ' % self.cluster.log_subdir +
            'rm logs/latest; ' +
            'ln -sf $(basename %s) logs/latest)' % self.cluster.log_subdir)

    def fix_disk_permissions(self):
        log("Fixing disk permissions")
        disks = default_disk2.split(' ')[1].split(',')
        cmds = ['sudo chmod 777 %s' % disk for disk in disks]
        self.remote_func('(%s)' % ';'.join(cmds))

    def cluster_enter(self, cluster):
        self.cluster = cluster
        log('== Connecting to Emulab via %s ==' % self.hosts[0][0])
        self.kill_procs()
        self.send_code()
        self.compile_code(clean=self.clean)
        self.create_log_dir()
        self.fix_disk_permissions()
        log('== Emulab Cluster Configured ==')
        log("If you are running clusterperf, it might take a while!")

    def collect_logs(self):
        log("Collecting logs")
        for host in self.hosts:
            subprocess.check_call("rsync -ave ssh " +
                                  "%s:%s/logs/ logs/> /dev/null" % (host[0],
                                                                    self.get_remote_wd()),
                                                                    shell=True, stdout=sys.stdout)

    def cluster_exit(self):
        log('== Emulab Cluster Tearing Down ==')
        self.collect_logs()
        log('== Emulab Cluster Torn Down ==')
        pass

local_scripts_path = os.path.dirname(os.path.abspath(__file__))
top_path = os.path.abspath(local_scripts_path + '/..')
obj_path = os.path.join(top_path, obj_dir)
