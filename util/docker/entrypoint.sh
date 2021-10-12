#!/bin/bash

service openvswitch-switch start > /dev/null 2>&1
ovs-vsctl set-manager ptcp:6640 > /dev/null 2>&1

function becho {
  echo -e "\033[1m$1\033[0m"
}

function clean_exit {
  service openvswitch-switch stop > /dev/null 2>&1
  becho "*** Exiting Container... 👋"
  exit $1
}

function file_exists {
  if [ ! -f "$1" ]; then
    becho "*** Error: given topology script does not exist in containers filesystem 🆘"
    clean_exit 1
  fi
}

# Perform a clean if the clean env var is set
if [ ! -z ${CLEAN+x} ]; then
  if [ "y" == "${CLEAN}" ]; then
    becho "*** Running MN clean..."
    mn -c > /dev/null 2>&1
    becho "*** MN clean complete ✅"
  fi
fi

# Run using Python if SCRIPT is given
if [ ! -z ${SCRIPT+x} ]; then
  file_exists $SCRIPT
  becho "*** Running mininet via the Python API..."
  echo "*** Ignoring any command line flags"
  python3 $SCRIPT && clean_exit 0
else
  # Run MN with MN_FLAGS
  becho "*** Running mininet using mn - flags: '$@'"
  mn $@ && clean_exit 0
fi

