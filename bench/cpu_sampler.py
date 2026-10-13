"""Sample container CPU use once per second from the cgroup counters on the host.

    python bench/cpu_sampler.py --target clab-lb-lb --out cpu.csv \
        --clients clab-lb-c1 clab-lb-c2 clab-lb-c3 clab-lb-c4 --clients-out cpu_clients.csv

CPU % = growth of the cgroup's CPU time / wall time x 100, so 100 % is one full core
(cgroup v2 cpu.stat usage_usec, or v1 cpuacct.usage). Runs until SIGTERM/SIGINT.
Reading the counter file from the host is cheap and does not disturb the load, unlike
a `docker exec` per sample.
"""
import argparse
import signal
import subprocess
import time


def counter_path(container):
    """Return (path, scale) of the CPU-time counter of a running container.

    scale converts the counter value to microseconds.
    """
    pid = subprocess.check_output(
        ["docker", "inspect", "-f", "{{.State.Pid}}", container], text=True).strip()
    with open("/proc/%s/cgroup" % pid) as fh:
        lines = [ln.rstrip("\n").split(":", 2) for ln in fh if ln.count(":") >= 2]
    for _, controllers, path in lines:
        if controllers == "":  # cgroup v2 unified hierarchy
            return "/sys/fs/cgroup%s/cpu.stat" % path, 1
    for _, controllers, path in lines:
        if "cpuacct" in controllers.split(","):  # cgroup v1, nanoseconds
            return "/sys/fs/cgroup/cpuacct%s/cpuacct.usage" % path, 1e-3
    raise RuntimeError("no cpu cgroup found for %s" % container)


def read_usage_us(path, scale):
    with open(path) as fh:
        text = fh.read()
    if path.endswith("cpu.stat"):
        for ln in text.splitlines():
            if ln.startswith("usage_usec"):
                return float(ln.split()[1])
        raise RuntimeError("usage_usec missing in %s" % path)
    return float(text.strip()) * scale


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--target", required=True, help="container of the system under test")
    ap.add_argument("--out", required=True, help="csv: t_s,cpu_pct for the target")
    ap.add_argument("--clients", nargs="*", default=[], help="client containers")
    ap.add_argument("--clients-out", help="csv: t_s then one column per client")
    ap.add_argument("--interval", type=float, default=1.0)
    args = ap.parse_args(argv)

    names = [args.target] + args.clients
    counters = [counter_path(n) for n in names]
    stop = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.append(1))

    out = open(args.out, "w")
    out.write("t_s,cpu_pct\n")
    cl_out = open(args.clients_out, "w") if args.clients_out else None
    if cl_out:
        cl_out.write("t_s," + ",".join(n.rsplit("-", 1)[-1] for n in args.clients) + "\n")

    start = prev_t = time.monotonic()
    prev = [read_usage_us(p, s) for p, s in counters]
    while not stop:
        time.sleep(max(0.0, args.interval - (time.monotonic() - prev_t)))
        now = time.monotonic()
        cur = [read_usage_us(p, s) for p, s in counters]
        pct = [(c - p) / ((now - prev_t) * 1e6) * 100 for c, p in zip(cur, prev)]
        t_s = round(now - start, 1)
        out.write("%s,%.1f\n" % (t_s, pct[0]))
        out.flush()
        if cl_out:
            cl_out.write("%s,%s\n" % (t_s, ",".join("%.1f" % v for v in pct[1:])))
            cl_out.flush()
        prev_t, prev = now, cur


if __name__ == "__main__":
    main()
