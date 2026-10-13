"""Aggregate valid runs into tables, plots and a text summary.

    python bench/analyze.py [--results results] [--no-plots]

Groups runs by (exp, system, algorithm, concurrency, rtt_ms). For every metric the table
holds n, mean, sample standard deviation (n-1), min and max over the runs: the mean of
per-run values, never of raw samples. Only runs with valid=true are used.
Outputs: <results>/tables/<exp>.csv and <results>/plots/*.png (needs matplotlib).
"""
import argparse
import csv
import glob
import json
import math
import os
from collections import defaultdict

GROUP_KEYS = ("exp", "system", "algorithm", "concurrency", "rtt_ms")
STATS = ("mean", "std", "min", "max")


def flatten(s):
    """One summary.json -> {metric_name: number}. Missing (None) values are left out."""
    m = {}
    for w in ("pre", "post"):
        for k, v in s["windows"][w].items():
            m["%s_%s" % (w, k)] = v
    m["detection_s"] = s.get("detection_s")
    m["failed_in_detection_window"] = s.get("failed_in_detection_window")
    for k, v in s["cpu_pct"].items():
        m["cpu_%s" % k] = v
    for w in ("pre", "post"):
        for b, v in s.get("per_backend_%s" % w, {}).items():
            m["%s_%s" % (w, b)] = v
    return {k: v for k, v in m.items() if v is not None}


def describe(vals):
    n = len(vals)
    mean = sum(vals) / n
    std = math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1)) if n > 1 else 0.0
    return {"mean": mean, "std": std, "min": min(vals), "max": max(vals)}


def load_runs(results):
    """Return (valid_runs, invalid_count) from every summary.json under results/."""
    runs, invalid = [], 0
    for path in sorted(glob.glob(os.path.join(results, "*", "*", "summary.json"))):
        with open(path, encoding="utf-8") as fh:
            s = json.load(fh)
        if s.get("valid"):
            runs.append(s)
        else:
            invalid += 1
    return runs, invalid


def aggregate(runs):
    """{group key tuple: {"n": runs, metric: {mean, std, min, max}}}"""
    groups = defaultdict(list)
    for s in runs:
        groups[tuple(s[k] for k in GROUP_KEYS)].append(flatten(s))
    out = {}
    for key, flats in groups.items():
        agg = {"n": len(flats)}
        for metric in sorted({m for f in flats for m in f}):
            vals = [f[metric] for f in flats if metric in f]
            agg[metric] = describe(vals)
        out[key] = agg
    return out


def write_tables(agg, results):
    os.makedirs(os.path.join(results, "tables"), exist_ok=True)
    by_exp = defaultdict(dict)
    for key, a in agg.items():
        by_exp[key[0]][key] = a
    paths = []
    for exp, groups in sorted(by_exp.items()):
        metrics = sorted({m for a in groups.values() for m in a if m != "n"})
        header = list(GROUP_KEYS) + ["n"] + ["%s_%s" % (m, s) for m in metrics for s in STATS]
        path = os.path.join(results, "tables", "%s.csv" % exp)
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            for key in sorted(groups, key=lambda k: tuple(str(x) for x in k)):
                a = groups[key]
                row = list(key) + [a["n"]]
                for m in metrics:
                    row += [round(a[m][s], 3) if m in a else "" for s in STATS]
                w.writerow(row)
        paths.append(path)
    return paths


def series(agg, exp, system, metric, xkey, **fixed):
    """[(x, mean, std)] for one system, sorted by x. `fixed` pins other group keys."""
    pts = []
    for key, a in agg.items():
        g = dict(zip(GROUP_KEYS, key))
        if g["exp"] == exp and g["system"] == system and metric in a \
                and all(g[k] == v for k, v in fixed.items()):
            pts.append((g[xkey], a[metric]["mean"], a[metric]["std"]))
    return sorted(pts)


def plots(agg, results):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out = os.path.join(results, "plots")
    os.makedirs(out, exist_ok=True)
    algos = ("round_robin", "least_conn", "ip_hash")
    colours = {"own": "tab:blue", "haproxy": "tab:orange"}
    made = []

    def lines(fname, metric, ylabel, title):
        fig, axes = plt.subplots(1, 3, figsize=(13, 4), sharey=True)
        for ax, algo in zip(axes, algos):
            for system in ("own", "haproxy"):
                pts = series(agg, "exp1", system, metric, "concurrency", algorithm=algo, rtt_ms=0)
                if pts:
                    x, y, e = zip(*pts)
                    ax.errorbar(x, y, yerr=e, marker="o", capsize=3, label=system, color=colours[system])
            ax.set_title(algo)
            ax.set_xlabel("concurrent connections")
            ax.set_xscale("log")
        axes[0].set_ylabel(ylabel)
        axes[0].legend()
        fig.suptitle(title)
        fig.tight_layout()
        fig.savefig(os.path.join(out, fname), dpi=130)
        plt.close(fig)
        made.append(fname)

    lines("1_rps_vs_concurrency.png", "pre_rps", "requests per second", "Throughput before the kill (mean +/- std over runs)")
    lines("2_p95_vs_concurrency.png", "pre_p95_ms", "p95 latency (ms)", "p95 latency before the kill (mean +/- std over runs)")
    lines("5_cpu_vs_concurrency.png", "cpu_pre_mean", "LB CPU (% of one core)", "Load balancer CPU before the kill (mean +/- std over runs)")

    # 3: requests per backend, concurrency 200, pre and post kill, stacked
    fig, axes = plt.subplots(1, 3, figsize=(13, 4), sharey=True)
    for ax, algo in zip(axes, algos):
        labels, bottoms = [], defaultdict(float)
        for system in ("own", "haproxy"):
            for w in ("pre", "post"):
                key = ("exp1", system, algo, 200, 0)
                a = agg.get(key)
                if not a:
                    continue
                i = len(labels)
                labels.append("%s\n%s" % (system, w))
                for b in ("b1", "b2", "b3", "b4"):
                    v = a.get("%s_%s" % (w, b), {"mean": 0})["mean"]
                    ax.bar(i, v, bottom=bottoms[i], label=b if i == 0 else None)
                    bottoms[i] += v
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels)
        ax.set_title("%s, 200 connections" % algo)
    axes[0].set_ylabel("requests per 30 s window")
    axes[0].legend()
    fig.tight_layout()
    fig.savefig(os.path.join(out, "3_requests_per_backend.png"), dpi=130)
    plt.close(fig)
    made.append("3_requests_per_backend.png")

    # 4: failure detection time vs RTT (experiment 2)
    fig, ax = plt.subplots(figsize=(6, 4))
    for system in ("own", "haproxy"):
        pts = series(agg, "exp2", system, "detection_s", "rtt_ms", algorithm="round_robin", concurrency=200)
        if pts:
            x, y, e = zip(*pts)
            ax.errorbar(x, y, yerr=e, marker="o", capsize=3, label=system, color=colours[system])
    ax.set_xlabel("added round-trip time (ms)")
    ax.set_ylabel("failure detection time (s)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(out, "4_detection_time.png"), dpi=130)
    plt.close(fig)
    made.append("4_detection_time.png")
    return made


def text_summary(agg, runs, invalid):
    lines = ["%d valid runs, %d invalid (excluded)" % (len(runs), invalid)]
    for key in sorted(agg, key=lambda k: tuple(str(x) for x in k)):
        a = agg[key]
        if key[1] != "own":
            continue
        other = agg.get((key[0], "haproxy") + key[2:])
        if not other or "pre_rps" not in a or "pre_rps" not in other:
            continue
        lines.append("%s %s c=%s rtt=%s: rps own %.0f vs haproxy %.0f (%.0f%%), p95 own %.1f ms vs haproxy %.1f ms, "
                     "detection own %s s vs haproxy %s s (n=%d/%d)" % (
                         key[0], key[2], key[3], key[4],
                         a["pre_rps"]["mean"], other["pre_rps"]["mean"],
                         100.0 * a["pre_rps"]["mean"] / other["pre_rps"]["mean"] if other["pre_rps"]["mean"] else 0.0,
                         a["pre_p95_ms"]["mean"], other["pre_p95_ms"]["mean"],
                         "%.2f" % a["detection_s"]["mean"] if "detection_s" in a else "n/a",
                         "%.2f" % other["detection_s"]["mean"] if "detection_s" in other else "n/a",
                         a["n"], other["n"]))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--results", default="results")
    ap.add_argument("--no-plots", action="store_true")
    a = ap.parse_args(argv)
    runs, invalid = load_runs(a.results)
    if not runs:
        raise SystemExit("no valid summary.json under %s/<exp>/<run>/" % a.results)
    agg = aggregate(runs)
    for p in write_tables(agg, a.results):
        print("wrote", p)
    if not a.no_plots:
        try:
            for f in plots(agg, a.results):
                print("wrote", os.path.join(a.results, "plots", f))
        except ImportError:
            print("matplotlib not installed: tables written, plots skipped (pip install -r bench/requirements.txt)")
    print()
    print(text_summary(agg, runs, invalid))


if __name__ == "__main__":
    main()
