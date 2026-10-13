"""Write config.<algorithm>.json for the own load balancer from one template.

    python bench/make_configs.py [--template config.example.json] [--out-dir DIR]
                                 [--log-path logs/lb.jsonl]

The template supplies everything (listen, backends, health, timeouts, stats); only the
algorithm and the log path change between the three files. Each file is checked with
lb.config.load_config, so a bad template fails here and not in the middle of a run.
"""
import argparse
import json
import os
import sys

ALGORITHMS = ("round_robin", "least_conn", "ip_hash")
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_configs(template, out_dir, log_path):
    """Write one config per algorithm into out_dir and return the list of paths."""
    with open(template, encoding="utf-8") as fh:
        base = json.load(fh)
    sys.path.insert(0, REPO_DIR)
    from lb.config import load_config

    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for algo in ALGORITHMS:
        cfg = dict(base)
        cfg["algorithm"] = algo
        cfg["logging"] = dict(base.get("logging", {}), path=log_path)
        path = os.path.join(out_dir, "config.%s.json" % algo)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
            fh.write("\n")
        load_config(path)  # raises with a clear message if the config is invalid
        paths.append(path)
    return paths


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--template", default=os.path.join(REPO_DIR, "config.example.json"))
    ap.add_argument("--out-dir", default=os.path.join(REPO_DIR, "results", "configs"))
    ap.add_argument("--log-path", default="logs/lb.jsonl",
                    help="path inside the LB container (cwd is /app)")
    args = ap.parse_args(argv)
    for path in make_configs(args.template, args.out_dir, args.log_path):
        print(path)


if __name__ == "__main__":
    main()
