"""
  python3 -m eval.plot sim  [results/sim.csv]            -> results/sim.png
  python3 -m eval.plot util [logs/util.csv] [--link s1-s2] -> results/util.png
Needs matplotlib (sudo apt install python3-matplotlib).
"""
import argparse
import csv
import os
import statistics
import sys

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError:
    sys.exit("matplotlib missing: sudo apt install python3-matplotlib")


def load(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def series(rows, mp, key):
    """-> (rhos, means, stds) over seeds for one policy."""
    xs = sorted({float(r["rho"]) for r in rows})
    means, stds = [], []
    for x in xs:
        v = [float(r[key]) for r in rows if int(r["max_paths"]) == mp and float(r["rho"]) == x]
        means.append(statistics.mean(v))
        stds.append(statistics.pstdev(v))
    return xs, means, stds


def plot_sim(path, out):
    rows = load(path)
    fig, ax = plt.subplots(2, 2, figsize=(11, 8))
    names = {1: "shortest path only", 2: "any feasible path"}
    for mp in (1, 2):
        for a, key, title in ((ax[0][0], "accept_ratio", "Acceptance ratio (requests)"),
                              (ax[0][1], "util_mean", "Mean core-link utilization (of reservable)")):
            xs, m, s = series(rows, mp, key)
            a.errorbar(xs, [100 * v for v in m], yerr=[100 * v for v in s], marker="o", capsize=3,
                       label=names[mp])
            a.set_title(title)
            a.set_ylabel("%")
        xs, m, s = series(rows, mp, "lat_us_p95")
        ax[1][1].errorbar(xs, m, yerr=s, marker="o", capsize=3, label=names[mp])
    ax[1][1].set_title("Admission compute time, p95 (no network)")
    ax[1][1].set_ylabel("microseconds")

    xs = sorted({float(r["rho"]) for r in rows})                     # reject reasons, 'any path' policy
    bottom = [0.0] * len(xs)
    for key, label in (("rej_capacity", "capacity"), ("rej_conflict", "flow conflict"),
                       ("rej_malformed", "malformed"), ("rej_other", "other")):
        vals = []
        for x in xs:
            g = [r for r in rows if int(r["max_paths"]) == 2 and float(r["rho"]) == x]
            vals.append(100 * statistics.mean(float(r[key]) / float(r["n"]) for r in g))
        ax[1][0].bar([str(x) for x in xs], vals, bottom=bottom, label=label)
        bottom = [b + v for b, v in zip(bottom, vals)]
    ax[1][0].set_title("Why requests were rejected (any feasible path)")
    ax[1][0].set_ylabel("% of requests")
    for a in ax.flat:
        a.set_xlabel("offered load (fraction of cross-section capacity)")
        a.grid(alpha=.3)
        a.legend()
    fig.tight_layout()
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    fig.savefig(out, dpi=130)
    print("wrote", out)


def plot_util(path, link, out):
    rows = [r for r in load(path) if r["link"] == link]
    if not rows:
        sys.exit("no rows for link %s in %s" % (link, path))
    t0 = float(rows[0]["ts"])
    t = [float(r["ts"]) - t0 for r in rows]
    fig, a = plt.subplots(figsize=(9, 4))
    a.step(t, [float(r["reserved_kbps"]) / 1000 for r in rows], where="post", label="reserved")
    meas = [(x, float(r["measured_kbps"]) / 1000) for x, r in zip(t, rows) if r["measured_kbps"]]
    if meas:
        a.plot(*zip(*meas), marker=".", label="measured (port tx)")
    a.axhline(float(rows[0]["cap_kbps"]) / 1000, ls="--", c="gray", label="reservable capacity")
    a.set_xlabel("seconds")
    a.set_ylabel("Mbps")
    a.set_title("Link %s: reserved vs measured" % link)
    a.legend()
    a.grid(alpha=.3)
    fig.tight_layout()
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    fig.savefig(out, dpi=130)
    print("wrote", out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["sim", "util"])
    ap.add_argument("path", nargs="?")
    ap.add_argument("--link", default="s1-s2")
    ap.add_argument("--out")
    a = ap.parse_args()
    if a.what == "sim":
        plot_sim(a.path or "results/sim.csv", a.out or "results/sim.png")
    else:
        plot_util(a.path or "logs/util.csv", a.link, a.out or "results/util.png")
