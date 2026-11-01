# cloud-cost-optimizer

`costopt` finds the usual AWS and Kubernetes waste, prices each finding, and applies the safe fixes
only after you've reviewed them:

```
costopt aws --regions us-east-1,eu-west-1 --format markdown --out report.md   # read-only scan
costopt aws --format json --out findings.json && costopt fix findings.json     # plan (dry run)
costopt fix findings.json --apply                                              # snapshot-first, re-checked, audited
costopt k8s --prometheus http://prometheus:9090 --window 7d                    # request rightsizing
costopt commit --from-cost-explorer --discount 0.28                            # Savings Plan size
```

It's a personal project rebuilding, in the open, the kind of tooling behind the resource
optimisation I did in production (Kubernetes and AWS, ~30% infrastructure cost reduction). It
isn't code from a former employer, and the numbers below come from the demo environments
described, not from a real bill.

## Result

**AWS** (`demo/run.sh`: an account seeded with typical waste in a moto server, so no real AWS and
no cost; CI runs it on every push). Full report: [`docs/sample-aws-report.md`](docs/sample-aws-report.md).

| Step | Monthly saving |
|---|---|
| Scan: 17 findings across 9 checks | **$467.14** found |
| `costopt fix --apply`: unattached volumes snapshotted then deleted, gp2 -> gp3, unused snapshots and EIPs removed | **$140.80** fixed automatically |
| Left for a person: 3 oversized instances, a long-stopped instance, an idle ALB and NAT gateway | $326.34 recommended |

The volume tagged `costopt:ignore=true` and the busy instance were correctly left alone.

**Kubernetes** (`demo/k8s/workloads.yaml` in a real kind cluster with kube-prometheus-stack).
Full report: [`docs/sample-k8s-report.md`](docs/sample-k8s-report.md).

| Workload | Found |
|---|---|
| `reports`: 3 replicas requesting 500m / 512Mi, using ~0 | lower to 10m / 32Mi: **$38.12/month** of node capacity freed |
| `worker`: requests 50m, CPU-throttled by its limit in 100% of periods | flagged: usage understates demand, raise the limit; CPU request never cut |
| `cache`: no requests | flagged BestEffort, suggested requests from usage |

**Savings Plans: how much to commit** (`costopt commit`; the sample is a generated month of hourly compute spend
with a $14/h base, weekday daytime peaks and a nightly batch; a real history comes from a CSV or straight from Cost
Explorer with `--from-cost-explorer`). At a 28% plan discount:

| Commitment | $ per hour | Saving / month | Utilisation | Coverage |
|---|---|---|---|---|
| **optimal: cover the 28th percentile of hourly spend** | $10.10 | **$2,734** (22.2%) | 98.7% | 82.1% |
| commit at the median | $10.87 | $2,640 | 96.0% | 85.9% |
| commit at the minimum | $8.51 | $2,416 | 100% | 70.1% |
| commit at the average (a common rule of thumb) | $12.14 | $2,290 | 90.6% | 90.6% |

Why the quantile: a commitment covers on-demand spend up to L = c / (1 − d) each hour and is paid whether used or
not, so total cost is convex in L, and the minimum is where the share of hours above L equals 1 − d (the
newsvendor problem). Committing at the average buys commitment the night hours can't use. The report also shows
what the commitment does if usage then falls 20% (still $1,710/month saved). Property-tested against brute force on
random histories.

## What it checks

| Check | Finds | Fix |
|---|---|---|
| `ebs-unattached` | Volumes in `available` state older than 7 days | `costopt fix`: snapshot (tags carried over), wait, delete |
| `ebs-gp2-to-gp3` | Attached gp2 volumes; saving net of the IOPS/throughput gp3 needs to match | `costopt fix`: modify in place, online |
| `snapshot-old` | Own snapshots older than 90 days not used by any AMI (upper bound: snapshots are incremental) | `costopt fix`: delete (re-checked against AMIs) |
| `eip-unassociated` | Elastic IPs attached to nothing ($3.65/month each) | `costopt fix`: release |
| `ec2-rightsize` | Running instances whose p95 of hourly-max CPU over 14 days is under 40%: one size down | A person (needs stop/start; check memory) |
| `ec2-stopped` | Instances stopped over 30 days still paying for their volumes | A person (AMI and terminate) |
| `lb-idle` | Load balancers with no healthy targets or no requests in 14 days | A person |
| `nat-idle` | NAT gateways that sent under 1 GB in 14 days | A person (and gateway endpoints for S3/DynamoDB) |
| `untagged` | Running instances missing `owner` / `cost-center` (saving 0: unallocatable spend) | Tag policy / SCP |
| `k8s-rightsize` | Requests vs p95 CPU and peak memory per workload; under-requested, throttled and BestEffort containers | A person (or a VPA in recommend mode) |

Safety: read-only scanning (IAM policy in [`deploy/terraform`](deploy/terraform), a test fails if
the code calls an API the policy doesn't grant). Fixes are a dry run unless `--apply`, re-read each
resource's live state first (attached, associated, newly tagged `costopt:ignore=true` or gone since
the scan → skipped), are capped by `--max-actions`, refuse findings from another account, and append
to an audit log. More in [`docs/design.md`](docs/design.md).

## Run it
```bash
pip install -e ".[dev]"
python -m pytest -q tests                 # 87 tests, AWS mocked with moto

# The AWS demo (moto server, nothing real is touched)
docker run -d --name moto -p 5055:5000 -e MOTO_EC2_LOAD_DEFAULT_AMIS=false motoserver/moto:5.2.4
bash demo/run.sh

# The Kubernetes demo (any cluster with kube-prometheus-stack)
kubectl apply -f demo/k8s/workloads.yaml          # wait ~15 min for metrics
kubectl -n monitoring port-forward svc/<prometheus-service> 9090
costopt k8s --prometheus http://localhost:9090 --window 15m --namespaces costopt-demo
```
Against a real account: normal AWS credentials (or `--role-arn` per account), at least a week of
CloudWatch history, and the defaults' age guards left on.

## Layout
| Path | What's in it |
|---|---|
| [`costopt/aws.py`](costopt/aws.py) | The AWS checks, multi-account (assume role) and multi-region scanning |
| [`costopt/k8s.py`](costopt/k8s.py) | Kubernetes request rightsizing from Prometheus |
| [`costopt/remediate.py`](costopt/remediate.py) | `costopt fix`: the four safe actions, state re-checks, audit log |
| [`costopt/pricing.py`](costopt/pricing.py) | Dated us-east-1 price table, region multipliers |
| [`costopt/report.py`](costopt/report.py) | Text, Markdown, JSON, CSV |
| [`deploy/terraform`](deploy/terraform) | Weekly scan: EventBridge -> Lambda -> S3 report + SNS summary; KMS, DLQ, X-Ray; a read-only role for member accounts |
| [`demo/`](demo) | The seeded moto account, the scan-fix-rescan script, the Kubernetes workloads |

## Limits
Prices are a static us-east-1 table with region multipliers (estimates for prioritising, not a
bill). No memory data for EC2 without the CloudWatch agent. `costopt commit` sizes new commitments
but doesn't analyse existing ones (utilisation of plans you already own). S3 storage classes, RDS and data
transfer are not covered yet: see
[`docs/design.md`](docs/design.md).
