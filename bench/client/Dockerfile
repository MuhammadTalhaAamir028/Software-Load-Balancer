# Client image for c1-c4 in the topology: load generators and network tools.
# Build:  docker build -t cn-lb-client:latest bench/client

# Stage 1: build hey from its pinned release tag. (The prebuilt release URL named in the
# project brief now answers 403, and a source build at a fixed tag is reproducible.)
FROM golang:1.22 AS hey
ARG HEY_VERSION=v0.1.4
RUN CGO_ENABLED=0 GOBIN=/out go install github.com/rakyll/hey@${HEY_VERSION}

# Stage 2: the image the topology runs.
FROM ubuntu:22.04

# One layer: tools from apt, then the apt lists are removed to keep the image small.
#   curl, iproute2, iputils-ping, traceroute, tcpdump : debugging and the verify scripts
#   wrk                                               : fallback load generator (whole-run numbers only)
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates curl iproute2 iputils-ping traceroute tcpdump wrk \
 && rm -rf /var/lib/apt/lists/*

# hey is the primary load generator: its CSV output has one row per request with an offset,
# which lets collect.py cut a run into pre-kill and post-kill windows.
COPY --from=hey /out/hey /usr/local/bin/hey

# containerlab starts the node with this command; the experiments use `docker exec`.
CMD ["sleep", "infinity"]
