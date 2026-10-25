"""
Synthetic reservation workload (open loop, Poisson arrivals).

Requests are cross-switch pairs only (h1/h3 <-> h2/h4), the ones that actually use the core.
Offered load rho = lambda * E[bw] * E[duration] / 32000 kbps, where 32000 = cross-section
reservable capacity (2 directions x 2 paths x 8000 kbps).
"""
import random

LEFT, RIGHT = ["10.0.0.1", "10.0.0.3"], ["10.0.0.2", "10.0.0.4"]
BW_KBPS = [500, 1000, 2000, 4000]
BW_WEIGHTS = [4, 3, 2, 1]
MEAN_BW = sum(b * w for b, w in zip(BW_KBPS, BW_WEIGHTS)) / sum(BW_WEIGHTS)     # 1300
CROSS_CAPACITY_KBPS = 32000
BAD_LINES = ["RESERVE 10.0.0.1 10.0.0.2", "RESERVE 10.0.0.1 10.0.0.2 lots 0 10 udp/9",
             "RESERVE 10.0.0.1 10.0.0.2 1000 0 10 udp", "RESERVE 10.0.0.1 10.0.0.2 nan 0 10 udp/9"]


def rate_for_load(rho, dur=(5, 30)):
    return rho * CROSS_CAPACITY_KBPS / (MEAN_BW * (dur[0] + dur[1]) / 2.0)


def generate(n, rate, seed=1, dur=(5, 30), advance_frac=0.2, dup_frac=0.03, bad_frac=0.02):
    """-> list of dicts: t, line, bad, kbps, dur, src, dst. `rate` = requests per second."""
    rnd = random.Random(seed)
    t, port, recent, out = 0.0, 10000, [], []
    for _ in range(n):
        t += rnd.expovariate(rate)
        if rnd.random() < bad_frac:
            out.append(dict(t=t, line=rnd.choice(BAD_LINES), bad=True, kbps=0, dur=0, src="", dst=""))
            continue
        if recent and rnd.random() < dup_frac:             # reuse a recent flow identity
            src, dst, p = rnd.choice(recent[-5:])
        else:
            a, b = (LEFT, RIGHT) if rnd.random() < 0.5 else (RIGHT, LEFT)
            src, dst, p = rnd.choice(a), rnd.choice(b), port
            port += 1
        recent.append((src, dst, p))
        kbps = rnd.choices(BW_KBPS, BW_WEIGHTS)[0]
        d = rnd.uniform(*dur)
        start_in = rnd.uniform(5, 30) if rnd.random() < advance_frac else 0.0
        out.append(dict(t=t, bad=False, kbps=kbps, dur=d, src=src, dst=dst,
                        line="RESERVE %s %s %d %.2f %.2f udp/%d" % (src, dst, kbps, start_in, d, p)))
    return out
