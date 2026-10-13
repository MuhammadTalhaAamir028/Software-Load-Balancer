#!/usr/bin/env bash
# Apply every static route in the topology, then print the routing tables. Safe to run again.
#   R1: 10.0.3.0/24 via R2     R2: 10.0.1.0/24 via R1
#   clients: default via R1    backends: default via R2
#   LB and HAProxy: clients via R1, backends via R2
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/env.sh"

# Routers: turn on forwarding, then add the static route through FRR's staticd (idempotent).
for r in r1 r2; do nexec "$r" sysctl -qw net.ipv4.ip_forward=1; done
nexec r1 vtysh -c "configure terminal" -c "ip route $NET_BACKENDS $R2_LB_IP"
nexec r2 vtysh -c "configure terminal" -c "ip route $NET_CLIENTS $R1_LB_IP"

# Hosts: `replace` instead of `add`, so running twice does no harm.
for c in $CLIENT_NODES;  do nexec "$c" ip route replace default via "$R1_CLIENT_IP"; done
# b*-net, lb-net and haproxy-net are the network holders (see clab.yml): they own the addresses and routes.
for b in $BACKEND_NODES; do nexec "${b}-net" ip route replace default via "$R2_BACKEND_IP"; done
for n in lb-net haproxy-net; do
  nexec "$n" ip route replace "$NET_CLIENTS"  via "$R1_LB_IP"
  nexec "$n" ip route replace "$NET_BACKENDS" via "$R2_LB_IP"
done

echo "=== R1 (kernel table)";  nexec r1 ip route
echo "=== R2 (kernel table)";  nexec r2 ip route
echo "=== R1 static routes in FRR"; nexec r1 vtysh -c "show ip route static"
echo "=== R2 static routes in FRR"; nexec r2 vtysh -c "show ip route static"
echo "=== c1";  nexec c1 ip route
echo "=== b1";  nexec b1-net ip route
echo "=== lb";  nexec lb-net ip route
