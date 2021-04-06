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

import threading
import json
import os.path

from bottle import Bottle, HTTPResponse, request

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
        self.route(self.prefix+'/node/<node_name>/cmd',
                   method='POST', callback=self.__runCmd)
        self.route(self.prefix+'/node/<node_name>/monitor',
                   callback=self.__monitorNodeCmd)

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

    def __runCmd(self, node_name):
        if not node_name in self.mn:
            data = {"error": "node does not exist"}
            return self.__build_response(data, code=400)
        node = self.mn.get(node_name)
        body = request.json
        commandStr = body.get("command", "")
        node.sendCmd(commandStr)
        data = {
            "sent": "command has been sent to the node"
        }
        return self.__build_response(data)

    def __monitorNodeCmd(self, node_name):
        if not node_name in self.mn:
            data = {"error": "node does not exist"}
            return self.__build_response(data, code=400)
        node = self.mn.get(node_name)
        output = node.monitor(timeoutms=2000)
        data = {
            "node_output": output
        }
        return self.__build_response(data)
