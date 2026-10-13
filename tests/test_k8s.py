from costopt import k8s

MIB = 1024**2


class FakeProm:
    """Answers each of the rightsizer's queries from canned series."""

    def __init__(self, pods):
        # pods: {(ns, pod, container): {"cpu_p95":..,"mem_peak":..,"cpu_req":..,"mem_req":..}}, owner from pod name
        self.pods = pods

    def query(self, promql):
        key = next(k for k, q in k8s.queries("7d").items() if q == promql)
        if key == "owners":
            return [({"namespace": ns, "pod": pod, "owner_kind": "ReplicaSet", "owner_name": pod.rsplit("-", 1)[0]}, 1.0)
                    for ns, pod, _ in self.pods]
        if key == "running":
            return [({"namespace": ns, "pod": pod, "phase": "Running"}, 1.0) for ns, pod, _ in self.pods if not pod.startswith("done-")]
        if key == "containers":
            return [({"namespace": ns, "pod": pod, "container": c}, 1.0) for ns, pod, c in self.pods]
        return [({"namespace": ns, "pod": pod, "container": c}, v[key]) for (ns, pod, c), v in self.pods.items() if key in v]


def pods(n, ns="shop", name="api-7d9f8c6b5", container="api", **v):
    return {(ns, f"{name}-{i:05d}", container): v for i in range(n)}


def test_overprovisioned_deployment():
    prom = FakeProm(pods(4, cpu_p95=0.05, mem_peak=200 * MIB, cpu_req=1.0, mem_req=2048 * MIB))
    [f] = k8s.rightsize(prom)
    assert f.resource_id == "shop/deployment/api/api"           # ReplicaSet hash stripped
    assert "cpu 60m" in f.recommendation and "memory 240Mi" in f.recommendation   # +20% headroom
    expected = 4 * ((1.0 - 0.06) * 0.0316 + (2048 - 240) / 1024 * 0.0041) * 730
    assert abs(f.monthly_saving - expected) < 0.05


def test_under_requested_is_flagged_not_saved():
    [f] = k8s.rightsize(FakeProm(pods(2, cpu_p95=0.9, mem_peak=900 * MIB, cpu_req=0.25, mem_req=512 * MIB)))
    assert f.monthly_saving == 0 and f.recommendation.startswith("Raise the CPU request to 1080m") and "memory request to 1080Mi" in f.recommendation


def test_no_requests_is_best_effort():
    [f] = k8s.rightsize(FakeProm(pods(1, cpu_p95=0.02, mem_peak=50 * MIB)))
    assert "BestEffort" in f.recommendation and "cpu 25m" in f.recommendation


def test_well_sized_and_tiny_savings_not_reported():
    prom = FakeProm(pods(1, cpu_p95=0.2, mem_peak=400 * MIB, cpu_req=0.25, mem_req=512 * MIB))
    assert k8s.rightsize(prom) == []


def test_minimums_and_excluded_namespaces():
    prom = FakeProm({**pods(1, cpu_p95=0.0005, mem_peak=4 * MIB, cpu_req=0.5, mem_req=256 * MIB),
                     **pods(1, ns="kube-system", name="coredns-5d78c9869d", container="coredns", cpu_p95=0.001, mem_peak=10 * MIB,
                            cpu_req=1.0, mem_req=1024 * MIB)})
    [f] = k8s.rightsize(prom)
    assert f.resource_id.startswith("shop/") and "cpu 10m" in f.recommendation and "memory 32Mi" in f.recommendation


def test_replicas_take_the_busiest_pod():
    p = {**pods(1, cpu_p95=0.1, mem_peak=100 * MIB, cpu_req=1.0, mem_req=1024 * MIB)}
    p[("shop", "api-7d9f8c6b5-99999", "api")] = dict(cpu_p95=0.4, mem_peak=300 * MIB, cpu_req=1.0, mem_req=1024 * MIB)
    [f] = k8s.rightsize(FakeProm(p))
    assert "2 replica(s)" in f.detail and "cpu 480m" in f.recommendation and "memory 360Mi" in f.recommendation


def test_workload_names():
    assert k8s.workload_of("ReplicaSet", "web-6b7f9d") == ("deployment", "web")
    assert k8s.workload_of("StatefulSet", "db") == ("statefulset", "db")
    assert k8s.workload_of(None, "bare") == ("pod", "bare")


def test_completed_pods_and_namespace_filter():
    prom = FakeProm({**pods(1, cpu_p95=0.01, mem_peak=50 * MIB, cpu_req=1.0, mem_req=1024 * MIB),
                     **pods(1, name="done-migrate", cpu_p95=0.01, mem_peak=50 * MIB, cpu_req=1.0, mem_req=1024 * MIB),
                     **pods(1, ns="other", cpu_p95=0.01, mem_peak=50 * MIB, cpu_req=1.0, mem_req=1024 * MIB)})
    assert [f.resource_id for f in k8s.rightsize(prom, namespaces=("shop",))] == ["shop/deployment/api/api"]


def test_throttled_container_is_never_cut():
    """Uses 60m against a 50m request, but its limit throttles it in 25% of periods: real demand
    is higher than measured, so the advice is to raise the limit, not to size from usage."""
    prom = FakeProm(pods(1, cpu_p95=0.06, mem_peak=20 * MIB, throttled=0.25, cpu_req=0.05, mem_req=16 * MIB))
    [f] = k8s.rightsize(prom)
    assert f.monthly_saving == 0 and f.recommendation.startswith("CPU is throttled by its limit in 25%") and "memory request to 32Mi" in f.recommendation   # 32Mi floor


def test_throttled_but_overrequested_keeps_cpu_request():
    prom = FakeProm(pods(1, cpu_p95=0.1, mem_peak=100 * MIB, throttled=0.3, cpu_req=1.0, mem_req=2048 * MIB))
    [f] = k8s.rightsize(prom)
    assert "throttled" in f.recommendation and f.monthly_saving == 0
