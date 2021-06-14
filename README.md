`MTV`: Mininet Testbed for Virtualized Network Functions
========================================================

A mininet fork designed to aide in the emulation of heterogenous VNF-based networks


## What is mininet?

Mininet is a popular python-based network emulation tool. As this fork goes somewhat against the direction planned for Mininet, this fork my behave unlike the upstream tool. However, if you want to know more about Mininet, check out the [upstream readme](https://github.com/mininet/mininet/blob/master/README.md).

## What does the fork do?

To reduce both the extensive human involvement in deploying critical network functions and the risk involved in automating that deployment, this tools is deigned to check VNFs for non-performance related bugs pre-deployment as part of a CI/CD workflow.

### Key Changes

 - Integrated RESTful API for all topologies (`--rest` flag in `mn`)
 - Add LibVirt nodes into a topology

## Usage

The best way to get started with `MTV` is via Docker. To get a topology going with a rest api, just run:
```bash
docker run --rm -it --privileged ghcr.io/ng-cdi/mtv:latest "--rest --switch ovsk"
```
