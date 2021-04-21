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

    def __init__(self, mininet, port=8080, **kwargs):
        """Start and run interactive REST API
           mininet: Mininet network object
           port: port to expose the API"""
        super(REST, self).__init__()
        self.mn = mininet
        self.prefix = kwargs.get("prefix", "/mn/api")
        if not self.prefix.startswith('/'):
            mnerror('the api prefix must start with a /\n')
            return
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
        # Create a new command
        self.route(self.prefix+'/node/<node_name>/process/create',
                   method='POST', callback=self.__nodeCmd)
        # Get command list
        self.route(self.prefix+'/node/<node_name>/processes',
                   callback=self.__nodeCmdList)
        self.route(self.prefix+'/node/<node_name>/process/list',
                   callback=self.__nodeCmdList)

        self.route(self.prefix+'/node/<node_name>/process/<pid>',
                   callback=self.__nodeCmdMonitor)
        self.route(self.prefix+'/node/<node_name>/process/<pid>/terminate',
                   callback=self.__nodeCmdTerminate)
        self.route(self.prefix+'/node/<node_name>/process/<pid>/remove',
                   callback=self.__nodeCmdRemove)

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
                "sender": response[0].name,
                "target": response[1].name,
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
        full = request.query.get("full", False)
        try:
            full = bool(full)
        except:
            data = {"error": "full should be true/false"}
            return self.__build_response(data, code=400)
        if not full:
            data = {"nodes": [node for node in self.mn]}
        else:
            data = {
                "nodes": [
                    {
                        "name": self.mn.get(n).name,
                        "class": self.mn.get(n).__class__.__name__
                    } for n in self.mn
                ]
            }
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
        """ Run an Iperf test bettween 2 hosts and return the results"""
        if str(request.query.get("server", None)) in self.mn:
            server = self.mn.get(str(request.query.server))
        else:
            data = {"error": "server not a valid host"}
            return self.__build_response(data, code=400)
        if str(request.query.get("client", None)) in self.mn:
            client = self.mn.get(str(request.query.client))
        else:
            data = {"error": "client not a valid host"}
            return self.__build_response(data, code=400)
        type_ = str(request.query.get("type", "TCP")).upper()
        if not type_ in ["UDP", "TCP"]:
            data = {"error": "type must be TCP or UDP"}
            return self.__build_response(data, code=400)
        time = request.query.get("time", 5)
        try:
            time = int(time)
        except:
            data = {"error": "time should be a number"}
            return self.__build_response(data, code=400)
        if time <= 0 or time >= 31:
            data = {"error": "time should be between 1 and 30"}
            return self.__build_response(data, code=400)
        try:
            results = self.mn.iperf(
                hosts=[client, server], seconds=time, fmt='m', l4Type=type_)
        except:
            data = {"error": "iperf failed"}
            return self.__build_response(data, code=500)
        data = {
            "server": server.name,
            "client": client.name,
            "traffic_type": type_,
            "seconds": time,
            "client_speed": results[1],
            "server_speed": results[0]
        }
        return self.__build_response(data)

    def __nodeCmd(self, node_name):
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
            self.node_procs.setdefault(node_name, [])
            self.node_procs[node_name].append(
                {
                    "process": popen,
                    "command": body.get("command"),
                    "exit_code": None,
                    "pid": popen.pid,
                    "stdout": "",
                    "stderr": ""
                }
            )
            data = {
                "success": "created process pid",
                "pid": popen.pid
            }
            return self.__build_response(data, code=200)
        except:
            data = {"error": "popen command failed"}
            return self.__build_response(data, code=500)

    def __nodeCmdList(self, node_name):
        if node_name in self.node_procs:
            procs = {
                "processes": [{
                    "pid": p.get("pid", 0),
                    "state": ("running" if p.get("process").poll() == None else "stopped"),
                    "command": p.get("command", "nil")
                } for p in self.node_procs.get(node_name)]
            }
            return self.__build_response(procs, code=200)
        data = {"error": "node command list can not be found"}
        return self.__build_response(data, code=400)

    def __nodeCmdMonitor(self, node_name, pid):
        if not node_name in self.node_procs:
            data = {"error": "node processes can not be found"}
            return self.__build_response(data, code=400)
        for proc in self.node_procs.get(node_name, []):
            if str(proc.get("pid")) == pid:
                popen = proc.get("process")
                proc["exit_code"] = popen.poll()
                try:
                    out, err = popen.communicate(timeout=5)
                    proc["stdout"] = out.decode('utf-8', 'backslashreplace')
                    proc["stderr"] = err.decode('utf-8', 'backslashreplace')
                except:
                    mnerror('Attempted to read streams of non-terminated process\n')
                data = {
                    "pid": popen.pid,
                    "state": ("running" if proc.get("process").poll() == None else "stopped"),
                    "exit_code": proc.get("process").poll(),
                    "stdout": proc["stdout"],
                    "stderr": proc["stderr"],
                    "command": proc.get("command")
                }
                return self.__build_response(data, code=200)
        data = {"error": "process with given id can not be found"}
        return self.__build_response(data, code=400)

    def __nodeCmdTerminate(self, node_name, pid):
        if not node_name in self.node_procs:
            data = {"error": "node processes can not be found"}
            return self.__build_response(data, code=400)
        for proc in self.node_procs.get(node_name, []):
            if str(proc.get("pid")) == pid:
                popen = proc.get("process")
                proc["exit_code"] = popen.poll()
                if proc["exit_code"] == None:
                    popen.terminate()
                    data = {"success": "process termination signal sent"}
                else:
                    data = {"success": "process is already terminated"}
                return self.__build_response(data, code=200)
        data = {"error": "process could not be found"}
        return self.__build_response(data, code=400)

    def __nodeCmdRemove(self, node_name, pid):
        if not node_name in self.node_procs:
            data = {"error": "node processes can not be found"}
            return self.__build_response(data, code=400)
        for proc in self.node_procs.get(node_name, []):
            if str(proc.get("pid")) == pid:
                popen = proc.get("process")
                proc["exit_code"] = popen.poll()
                if proc["exit_code"] == None:
                    data = {"error": "process must be terminated to remove"}
                else:
                    self.node_procs.get(node_name).remove(proc)
                    data = {"success": "process removed"}
                return self.__build_response(data, code=200)
        data = {"error": "process could not be found"}
        return self.__build_response(data, code=400)
