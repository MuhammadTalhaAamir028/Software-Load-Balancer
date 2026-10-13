# Client image for c1-c4 in the topology: load generators and network tools.
# Build:  docker build -t cn-lb-client:latest bench/client
FROM ubuntu:22.04

# One layer: tools from apt, then the apt lists are removed to keep the image small.
#   curl, iproute2, iputils-ping, traceroute, tcpdump : debugging and the verify scripts
#   wrk                                               : fallback load generator (whole-run numbers only)
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates curl iproute2 iputils-ping traceroute tcpdump wrk \
 && rm -rf /var/lib/apt/lists/*

# hey is the primary load generator (its CSV output has one row per request with an
# offset, which lets collect.py cut a run into pre-kill and post-kill windows).
# The release binary has no version in its URL, so the build checks its SHA-256 instead.
ARG HEY_URL=https://hey-release.s3.us-east-2.amazonaws.com/hey_linux_amd64
ARG HEY_SHA256=
RUN curl -fsSL -o /usr/local/bin/hey "$HEY_URL" \
 && if [ -n "$HEY_SHA256" ]; then echo "$HEY_SHA256  /usr/local/bin/hey" | sha256sum -c -; \
    else echo "hey sha256: $(sha256sum /usr/local/bin/hey | cut -d' ' -f1)"; fi \
 && chmod +x /usr/local/bin/hey

# containerlab starts the node with this command; the experiments use `docker exec`.
CMD ["sleep", "infinity"]
