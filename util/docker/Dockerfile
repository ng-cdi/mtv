ARG MNEXEC_IMAGE=ghcr.io/ng-cdi/mnexec:latest
ARG OPENFLOW_IMAGE=ghcr.io/ng-cdi/mnof:latest
FROM ${MNEXEC_IMAGE} as mnexec
FROM ${OPENFLOW_IMAGE} as openflow


FROM debian:stretch-slim

RUN sed -i -e 's/ main/ main contrib non-free/g' /etc/apt/sources.list && \
    apt-get update -q && \
    apt-get install --no-install-recommends -yq \
    dynamips \
    libpcap0.8 \
    arping \
    hping3 \
    iproute2 \
    iputils-ping \
    net-tools \
    openvswitch-common \
    openvswitch-switch \
    openvswitch-testcontroller \
    python3 \
    python3-bottle \
    python3-pip \
    python3-setuptools \
    telnet \
    python-libvirt \
    python3-libvirt \
    virtinst \
    libvirt-clients \
    dnsmasq \
    traceroute && \
    rm -rf /var/lib/apt/lists/*

COPY --from=openflow /usr/local/bin/ /usr/local/bin/
COPY --from=mnexec /build/mnexec /usr/bin/mnexec
WORKDIR /mtv
COPY setup.py .
COPY mtv/ mtv/
COPY bin/ bin/
RUN mkdir -p /etc/mtv
RUN mkdir -p /var/run/netns

# Unsure why necessary
RUN pip3 install docker

RUN pip3 install .

COPY util/docker/entrypoint.sh .
RUN chmod +x entrypoint.sh

ENTRYPOINT [ "./entrypoint.sh" ]
