# Cost findings

**Estimated saving: $38.12/month** ($457/year) across 3 findings; $0.00/month can be applied with `costopt fix`.

| Check | Findings | Saving / month |
|---|---:|---:|
| k8s-rightsize | 3 | $38.12 |

| Saving / month | Check | Resource | Owner | Recommendation |
|---:|---|---|---|---|
| $38.12 | k8s-rightsize | `costopt-demo/deployment/reports/reports` (homelab) | unowned | Lower requests to cpu 10m, memory 32Mi (+20% headroom); saving is realised when the autoscaler consolidates nodes. requests cpu 500m, memory 512Mi; p95 CPU 0m, peak memory 1Mi, 3 replica(s), window 20m |
| $0.00 | k8s-rightsize | `costopt-demo/deployment/cache/cache` (homelab) | unowned | Set requests (suggested cpu 10m, memory 32Mi): without them the pod is BestEffort, first to be evicted, and invisible to capacity planning. p95 CPU 0m, peak memory 1Mi, 1 replica(s), window 20m |
| $0.00 | k8s-rightsize | `costopt-demo/deployment/worker/worker` (homelab) | unowned | CPU is throttled by its limit in 100% of periods, so usage understates demand: raise or remove the CPU limit, then re-measure. requests cpu 50m, memory 16Mi; p95 CPU 56m, peak memory 0Mi, 1 replica(s), window 20m, throttled in 100% of periods |
