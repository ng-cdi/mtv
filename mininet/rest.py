"""
A simple rest api interface for Mininet.

The Mininet REST API provides a selection of endpoints
that enable interaction with an instantated topology.

curl ...

runs 'ifconfig' on host h27.

The REST API automatically substitutes IP addresses for node names,
so commands like

curl ...

should work correctly and allow host h2 to ping host h3
"""

import codecs
import threading
import json
import os.path

from bottle import Bottle, HTTPResponse, request
from subprocess import PIPE

from mininet.log import info, output
from mininet.log import error as mnerror

# TODO: Add common error handlers
# TODO: Graceful exit
# TODO: Custom command endpoint
# TODO: Document
# TODO: Format to the project style (not pep8)


class REST(Bottle):
    "Simple REST API to talk to nodes."

    prefix = '/mn/api'

    def __init__(self, mininet, port=8080, **kwargs):
        """Start and run interactive REST API
           mininet: Mininet network object
           port: port to expose the API"""
        super(REST, self).__init__()
        self.mn = mininet
        if port > 65536 or port < 1:
            mnerror('given port value is not valid for the api\n')
            return
        self.node_procs = {}
        thread = threading.Thread(
            target=self.run_server, args=(port,), daemon=True)
        thread.start()
        info('*** Running API\n')

    def run_server(self, port):
        self.route(self.prefix+'/nodes', callback=self.__getNodes)
        self.route(self.prefix+'/node/<node_name>', callback=self.__getNode)
        self.route(self.prefix+'/stop', callback=self.__stop)
        self.route(self.prefix+'/pingset', callback=self.__pingSet)
        self.route(self.prefix+'/pingall', callback=self.__pingAll)
        self.route(self.prefix+'/iperfpair', callback=self.__iperfPair)
        self.route(self.prefix+'/node/<node_name>/process/create',
                   method='POST', callback=self.__nodePOpen)
        self.route(self.prefix+'/node/<node_name>/process/<pid>',
                   callback=self.__nodePOpenMonitor)
        self.route(self.prefix+'/node/<node_name>/process/<pid>/terminate',
                   callback=self.__nodePOpenTerminate)

        try:
            self.run(host='0.0.0.0', port=port, quiet=True)
        except Exception:
            mnerror('Failed to start REST API\n')

    def __build_response(self, data, code=200):
        if not isinstance(data, dict):
            data = json.dumps({"error": "could not create json response"})
            return HTTPResponse(status=500, body=data)
        return HTTPResponse(status=code, body=data)

    def __getHostSet(self, host_names):
        if host_names == '':
            return None, "no hosts given"
        host_names = host_names.split(',')
        if len(host_names) < 2:
            return None, "not enough hosts"
        for host in host_names:
            host = str(host)
            if not host in self.mn:
                return None, "non-existent host given"
        return self.mn.get(*host_names), ""

    def __returnPingData(self, pingData):
        data = {}
        for response in pingData:
            data[("%s-%s" % (response[0].name, response[1].name))] = {
                "source": response[0].name,
                "destination": response[1].name,
                "sent": response[2][0],
                "received": response[2][1],
                "rtt_avg": response[2][3]
            }
        return self.__build_response(data)

    def __stop(self):
        self.mn.stop()
        self.close()
        return self.__build_response({"success": "stopping network"})

    def __getNodes(self):
        """Get a list of nodes that exist within a topo"""
        data = {"nodes": [node for node in self.mn]}
        return self.__build_response(data)

    def __getNode(self, node_name):
        if not node_name in self.mn:
            data = {"error": "node does not exist"}
            return self.__build_response(data, code=400)
        node = self.mn.get(node_name)
        data = {
            "name": node.name,
            "class": node.__class__.__name__
        }
        return self.__build_response(data, code=200)

    def __pingSet(self):
        hosts, error = self.__getHostSet(request.query.hosts)
        if not hosts:
            data = {"error": error}
            return self.__build_response(data, code=400)
        results = self.mn.pingFull(hosts=hosts)
        return self.__returnPingData(results)

    def __pingAll(self):
        results = self.mn.pingAllFull()
        return self.__returnPingData(results)

    def __iperfPair(self):
        hosts, error = self.__getHostSet(request.query.hosts)
        if not hosts:
            data = {"error": error}
            return self.__build_response(data, code=400)
        if len(hosts) > 2:
            data = {"error": "iperf only supports a set of 2 hosts"}
            return self.__build_response(data, code=400)
        try:
            results = self.mn.iperf(hosts=hosts)
        except:
            data = {"error": "iperf failed"}
            return self.__build_response(data, code=500)
        data = {
            "client_speed": results[1],
            "server_speed": results[0]
        }
        return self.__build_response(data)

    def __nodePOpen(self, node_name):
        if not node_name in self.mn:
            data = {"error": "node does not exist"}
            return self.__build_response(data, code=400)
        node = self.mn.get(node_name)
        try:
            body = request.json
        except:
            data = {"error": "but have json content type"}
            return self.__build_response(data, code=400)
        if not body.get("command", None):
            data = {"error": "a command must be provided in the json"}
            return self.__build_response(data, code=400)
        if not isinstance(body.get("command"), str):
            data = {"error": "command must be a string"}
            return self.__build_response(data, code=400)
        try:
            popen = node.popen(body.get("command"),
                               stdin=PIPE, stdout=PIPE, stderr=PIPE)
            self.node_procs.setdefault(node_name, {})
            self.node_procs[node_name][str(popen.pid)] = popen
            data = {
                "success": ("created process pid:%s" % str(popen.pid))
            }
            return self.__build_response(data, code=200)
        except:
            data = {"error": "popen command failed"}
            return self.__build_response(data, code=500)

    def __nodePOpenMonitor(self, node_name, pid):
        if not self.node_procs.get(node_name, {}).get(pid, None):
            data = {"error": "process does not exist"}
            return self.__build_response(data, code=400)
        proc = (self.node_procs[node_name][pid])
        exit_code = proc.poll()
        if not exit_code == None:
            data = {
                "status": "stopped",
                "exit_code": exit_code,
                "stdout": codecs.decode(proc.stdout.read()),
                "stderr": codecs.decode(proc.stderr.read())
            }
            (self.node_procs[node_name]).pop(pid)
        else:
            data = {"status": "running"}
        return self.__build_response(data, code=200)

    def __nodePOpenTerminate(self, node_name, pid):
        if not self.node_procs.get(node_name, {}).get(pid, None):
            data = {"error": "process does not exist"}
            return self.__build_response(data, code=400)
        proc = (self.node_procs[node_name][pid])
        exit_code = proc.poll()
        if exit_code == None:
            proc.terminate()
            data = {"success": "process termination signal sent"}
        else:
            data = {"success": "process has already terminated"}
        return self.__build_response(data, code=200)
