"""Parse `iperf3 -u -J` output into received Mbps (handles both JSON layouts)."""
import json


def received_mbps(text):
    try:
        j = json.loads(text)
    except ValueError:
        return None
    if "error" in j:
        return None
    end = j.get("end", {})
    if "sum_received" in end:                                  # newer iperf3
        return end["sum_received"]["bits_per_second"] / 1e6
    s = end.get("sum")
    if s:                                                      # older: server stats folded into sum
        return s["bits_per_second"] * (1 - s.get("lost_percent", 0) / 100.0) / 1e6
    return None
