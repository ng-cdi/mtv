# Using Mininet with Docker

> ⚠️ Be warned, this is largely untested at present!

Docker might be the fastest way to get started with Mininet for Linux and MacOS users (and perhaps WSL2 users?)! However, it does come with more potential issues.

## Why use Dockerized MN

Using Mininet within a Docket container doesn't realise the true potential of containerization. Due to the requirements of Minient, this does not provide much process isolation. The use of Docker here is more of a distribution and deployment mechanism. 

## How to use Mininet in Docker

The easiest way is the use the Dockerized `mn` CLI tool. For basic usage, you can simply replace `sudo mn` with the following command:

```bash
docker run --rm -it --privileged --network host \
-e CLEAN=y \
ghcr.io/ng-cdi/mn:latest "--help"
```
> This is equivalent to running `sudo mn --help`

You can run custom topologies too, provided you mount the topology file:

```bash
docker run --rm -it --privileged --network host \
-e CLEAN=y -v "$(pwd):/custom" \
ghcr.io/ng-cdi/mn:latest "--custom /custom/topology.py --topo mytopo"
```
> This is equivalent to running `sudo mn --custom ./topology.py --topo mytopo`

### Using the Python API

You can use Mininet with the Python API too! Just add the scripts file path to the `SCRIPT` env var in the run command:

```bash
docker run --rm -it --privileged --network host \
-e CLEAN=y -v "$(pwd):/custom" -e SCRIPT="/custom/topology.py" \
ghcr.io/ng-cdi/mininet:latest
```
> This is equivalent to running `sudo python3 topology.py` (and `mn` flags are ignored)

### Using the REST API

You can use this container with the Mininet REST API too. Simply map the port in the run command (default port is `8080`): `-p 8080:8080`.

### Using LibVirt Nodes

You can also create and use LibVirt nodes too provided the base system uses LibVirt and has the required permissions. This just need the LibVirt socket mounted:
```bash
-v "/var/run/libvirt/libvirt-sock:/var/run/libvirt/libvirt-sock"
```
## TODO

 1. Test more...
 2. Populate DockerIgnore in project root
