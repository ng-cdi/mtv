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
        self.route(self.prefix+'/stop', callback=self.__stop)
        self.route(self.prefix+'/pingpair', callback=self.__pingPair)

        try:
            self.run(host='0.0.0.0', port=port, quiet=True)
        except Exception as e:
            print(e)
            mnerror('Failed to start REST API\n')

    def __build_response(self, data, code=200):
        if not isinstance(data, dict):
            data = json.dumps({"error": "could not create json response"})
            return HTTPResponse(status=500, body=data)
        return HTTPResponse(status=code, body=data)

    def __stop(self):
        self.mn.stop()
        self.close()
        return self.__build_response({"success": "stopping network"})

    def __getNodes(self):
        """Get a list of nodes that exist within a topo"""
        data = {"nodes": [node for node in self.mn]}
        return self.__build_response(data)

    def __pingPair(self):
        a = request.query.host_a
        b = request.query.host_b
        if a == '' or b == '':
            data = {"error": "two hosts (host_a, host_b) must be given"}
            return self.__build_response(data, code=400)
        if not self.mn.get(a, b):
            data = {"error": "non-existent host given"}
            return self.__build_response(data, code=400)
        hosts = self.mn.get(a, b)
        results = self.mn.pingFull(hosts=hosts)
        data = {
            results[0][0].name: {
                "destination": results[0][1].name,
                "sent": results[0][2][0],
                "received": results[0][2][1]
            },
            results[1][0].name: {
                "destination": results[1][1].name,
                "sent": results[1][2][0],
                "received": results[1][2][1]
            }
        }
        return self.__build_response(data, code=200)
