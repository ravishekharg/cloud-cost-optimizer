"""Kubernetes request rightsizing from Prometheus (cAdvisor + kube-state-metrics, as installed by
kube-prometheus-stack).

Requests, not usage, decide how many nodes a cluster needs: the scheduler packs pods by what
they ask for. A workload asking for 1 CPU and using 50m holds 950m of a node that nobody else can
use. For each workload/container we compare:
  CPU     p95 over the window   -> recommended request = p95 x (1 + headroom)
  memory  peak over the window  -> recommended request = peak x (1 + headroom)   (memory isn't
          compressible: going over the limit is an OOM kill, so size from the peak, not a percentile)
and price the difference x replicas. Containers using more than they request (noisy neighbours,
eviction risk) and containers with no requests at all are reported too, with saving 0.

CPU throttling: a container held back by its CPU limit uses less than it wants, so its usage
understates demand. If more than THROTTLED of its CFS periods were throttled, the CPU request is
never lowered and the finding says to raise the limit instead.
"""
import math
from collections import defaultdict

import requests

from . import pricing
from .models import Finding

MIB = 1024**2
THROTTLED = 0.10


class Prometheus:
    def __init__(self, url, timeout=60):
        self.url, self.timeout = url.rstrip("/"), timeout

    def query(self, promql):
        r = requests.get(f"{self.url}/api/v1/query", params={"query": promql}, timeout=self.timeout)
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "success":
            raise RuntimeError(body)
        return [(s["metric"], float(s["value"][1])) for s in body["data"]["result"]]


def queries(window):
    sel = 'container!="",container!="POD"'
    by = "namespace, pod, container"
    return {
        "cpu_p95": f"quantile_over_time(0.95, sum by ({by}) (rate(container_cpu_usage_seconds_total{{{sel}}}[5m]))[{window}:5m])",
        "mem_peak": f"max_over_time(sum by ({by}) (container_memory_working_set_bytes{{{sel}}})[{window}:5m])",
        "throttled": f"max_over_time((sum by ({by}) (rate(container_cpu_cfs_throttled_periods_total{{{sel}}}[5m])) / "
                     f"sum by ({by}) (rate(container_cpu_cfs_periods_total{{{sel}}}[5m])))[{window}:5m])",
        "cpu_req": f'sum by ({by}) (kube_pod_container_resource_requests{{resource="cpu"}})',
        "mem_req": f'sum by ({by}) (kube_pod_container_resource_requests{{resource="memory"}})',
        "containers": f"sum by ({by}) (kube_pod_container_info)",
        "running": 'kube_pod_status_phase{phase="Running"} == 1',   # completed Job pods hold no capacity
        "owners": "kube_pod_owner",
    }


def workload_of(owner_kind, owner_name):
    """Pods of a Deployment are owned by a ReplicaSet named <deployment>-<pod-template-hash>."""
    if owner_kind == "ReplicaSet":
        return "deployment", owner_name.rsplit("-", 1)[0]
    return (owner_kind or "pod").lower(), owner_name


def collect(prom, window="7d", exclude_namespaces=(), namespaces=()):
    """{(namespace, kind, workload, container): {replicas, cpu_p95, mem_peak, cpu_req, mem_req}}
    for containers of currently running pods."""
    q = {name: prom.query(promql) for name, promql in queries(window).items()}
    running = {(m["namespace"], m["pod"]) for m, _ in q["running"]}
    owner = {(m["namespace"], m["pod"]): workload_of(m.get("owner_kind"), m.get("owner_name", m["pod"])) for m, _ in q["owners"]}
    per_pod = defaultdict(dict)
    for name in ("containers", "cpu_p95", "mem_peak", "throttled", "cpu_req", "mem_req"):
        for m, v in q[name]:
            ns = m.get("namespace")
            if "container" in m and ns not in exclude_namespaces and (not namespaces or ns in namespaces)                     and (ns, m["pod"]) in running:
                per_pod[(m["namespace"], m["pod"], m["container"])][name] = v
    workloads = defaultdict(lambda: {"replicas": 0, "cpu_p95": 0.0, "mem_peak": 0.0, "throttled": 0.0, "cpu_req": None, "mem_req": None})
    for (ns, pod, container), v in per_pod.items():
        if "containers" not in v:      # history of a pod that no longer exists
            continue
        kind, name = owner.get((ns, pod), ("pod", pod))
        w = workloads[(ns, kind, name, container)]
        w["replicas"] += 1
        w["cpu_p95"] = max(w["cpu_p95"], v.get("cpu_p95", 0.0))
        w["mem_peak"] = max(w["mem_peak"], v.get("mem_peak", 0.0))
        w["throttled"] = max(w["throttled"], v.get("throttled", 0.0))
        for r in ("cpu_req", "mem_req"):
            if r in v:
                w[r] = max(w[r] or 0.0, v[r])
    return dict(workloads)


def recommend(w, headroom=0.2):
    cpu = max(0.01, math.ceil(w["cpu_p95"] * (1 + headroom) * 200) / 200)            # round up to 5m, at least 10m
    mem = max(32 * MIB, math.ceil(w["mem_peak"] * (1 + headroom) / MIB) * MIB)       # round up to 1 MiB, at least 32 MiB
    return cpu, mem


def fmt_cpu(cores):
    return f"{cores * 1000:.0f}m"


def fmt_mem(b):
    return f"{b / MIB:.0f}Mi"


def hourly(cpu_cores, mem_bytes):
    return cpu_cores * pricing.K8S_VCPU_HOUR + mem_bytes / 1024**3 * pricing.K8S_GIB_HOUR


def rightsize(prom, cluster="cluster", window="7d", headroom=0.2, min_saving=1.0, exclude_namespaces=("kube-system",),
              namespaces=()):
    findings = []
    for (ns, kind, name, container), w in collect(prom, window, exclude_namespaces, namespaces).items():
        rid = f"{ns}/{kind}/{name}/{container}"
        base = dict(check="k8s-rightsize", resource_id=rid, region=cluster, account="kubernetes")
        cpu_rec, mem_rec = recommend(w, headroom)
        usage = f"p95 CPU {fmt_cpu(w['cpu_p95'])}, peak memory {fmt_mem(w['mem_peak'])}, {w['replicas']} replica(s), window {window}"
        if w["cpu_req"] is None and w["mem_req"] is None:
            findings.append(Finding(
                **base, monthly_cost=0.0, monthly_saving=0.0,
                recommendation=f"Set requests (suggested cpu {fmt_cpu(cpu_rec)}, memory {fmt_mem(mem_rec)}): without them the pod "
                               "is BestEffort, first to be evicted, and invisible to capacity planning",
                detail=usage))
            continue
        cpu_req, mem_req = w["cpu_req"] or 0.0, w["mem_req"] or 0.0
        detail = f"requests cpu {fmt_cpu(cpu_req)}, memory {fmt_mem(mem_req)}; {usage}"
        cost = round(w["replicas"] * hourly(cpu_req, mem_req) * pricing.HOURS_PER_MONTH, 2)
        saving = round(w["replicas"] * hourly(max(0, cpu_req - cpu_rec), max(0, mem_req - mem_rec)) * pricing.HOURS_PER_MONTH, 2)
        fixes = []
        if w["throttled"] > THROTTLED:   # usage is capped by the limit, so real demand is unknown: never cut CPU
            fixes.append(f"CPU is throttled by its limit in {w['throttled']:.0%} of periods, so usage understates demand: "
                         "raise or remove the CPU limit, then re-measure")
            detail += f", throttled in {w['throttled']:.0%} of periods"
        elif cpu_req and w["cpu_p95"] > cpu_req:
            fixes.append(f"raise the CPU request to {fmt_cpu(cpu_rec)} (usage is above it: the node is overcommitted)")
        if mem_req and w["mem_peak"] > mem_req:
            fixes.append(f"raise the memory request to {fmt_mem(mem_rec)} (usage is above it: first to be evicted under memory pressure)")
        if fixes:
            text = "; ".join(fixes)
            findings.append(Finding(**base, monthly_cost=cost, monthly_saving=0.0, recommendation=text[0].upper() + text[1:], detail=detail))
        elif saving >= min_saving:
            findings.append(Finding(
                **base, monthly_cost=cost, monthly_saving=saving,
                recommendation=f"Lower requests to cpu {fmt_cpu(min(cpu_req, cpu_rec))}, memory {fmt_mem(min(mem_req, mem_rec))} "
                               f"(+{headroom:.0%} headroom); saving is realised when the autoscaler consolidates nodes",
                detail=detail))
    return sorted(findings, key=lambda f: f.monthly_saving, reverse=True)
