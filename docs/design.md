# Design notes

## How it's meant to be used
1. **Weekly, read-only** (the Lambda in `deploy/terraform`): every account and region, a Markdown
   and JSON report in S3, and a summary with the top findings on SNS (email / Slack / Teams).
2. **Owners act**: each finding carries the resource's `owner` tag, so it can go to the team that
   owns the spend, not to a central queue.
3. **Safe fixes in bulk**: someone reviews the JSON, then runs `costopt fix findings.json --apply`
   with that account's credentials. Everything else (resizing, deleting load balancers) goes
   through the owning team's normal change process.

Scanning never needs write access; only the person running `fix` does. That split is deliberate:
a scheduled job with delete rights across all accounts is a large blast radius for a few dollars.

## Why these checks
They're the findings that come up in almost every account, are cheap to detect from APIs that
already exist, and have clear fixes:

| Lever | Typical size | Why it happens |
|---|---|---|
| Rightsizing EC2 / Kubernetes requests | Largest | Sized for a launch-day guess or a one-off peak, never revisited; Kubernetes requests copied between services |
| Unattached volumes, old snapshots | Steady leak | `DeleteOnTermination=false`, manual backups, AMIs deregistered without their snapshots |
| gp2 -> gp3 | 20% of EBS | gp2 was the default for years; gp3 is cheaper with better baseline performance |
| Idle LBs, NAT gateways, EIPs | Small each, many of them | Leftovers from finished projects and test environments; every public IPv4 costs money since 2024 |
| Tagging | Enabler | Without an owner, nobody can be asked; tag policies stop new untagged spend |

## Estimating, not billing
A static price table keeps scans offline, fast and testable. Savings are for ranking work, which
needs to be roughly right, not exact. Known simplifications, each stated in the finding:
- snapshot cost is an upper bound (incremental storage isn't visible through the API),
- load balancer savings are the hourly charge only (LCUs vary),
- rightsizing assumes one size down halves the price (true within these families),
- Kubernetes savings are freed requests priced per vCPU/GiB; they turn into money only when the
  autoscaler (Karpenter consolidation / Cluster Autoscaler scale-down) removes nodes.

For exact figures: Cost Explorer's rightsizing recommendations and the Pricing API, or the Cost
and Usage Report in Athena.

## Rightsizing rules, and why
- **EC2**: p95 of *hourly maximum* CPU over 14 days below 40%. Using hourly maxima (not averages)
  keeps short bursts visible; halving vCPUs at p95 < 40% leaves peaks below ~80%. Under a week of
  data is not judged. Memory isn't in CloudWatch without the agent, so the finding says to check.
- **Kubernetes CPU**: p95 + 20% headroom. CPU is compressible: going over a request means sharing,
  not crashing.
- **Kubernetes memory**: peak + 20%. Memory isn't compressible: over the limit is an OOM kill, so
  a percentile would undersize.
- **Throttling**: a container throttled by its CPU limit uses less than it wants, so its usage is
  a floor, not its demand. Over 10% of periods throttled means never cutting its CPU request and
  saying to raise the limit. (This came up in the demo: the "worker" measured 56m against a 50m
  request, but was throttled in every period.)

## Safety of `costopt fix`
| Rule | Prevents |
|---|---|
| Dry run by default | Surprises |
| Only four actions, all reversible or recoverable | Irreversible mistakes (no instance termination, no LB deletion) |
| Snapshot before deleting a volume, tags carried over | Losing data someone still needed |
| Live state re-read before each action | Acting on a days-old finding: a volume attached since, an EIP associated since, a snapshot now behind a new AMI |
| `costopt:ignore=true` honoured at scan and at fix time | Overriding an owner's decision |
| `--max-actions` (default 20) | A bad findings file deleting hundreds of resources |
| Findings from another account refused | Running with the wrong credentials |
| Audit log (JSON lines) | "Who deleted my volume?" |

## Not covered yet, and how they'd fit
- **Commitments**: Savings Plans / RI coverage and utilisation (Cost Explorer API) - usually the
  biggest single lever after rightsizing.
- **S3**: storage class analysis, lifecycle to Intelligent-Tiering / Glacier, incomplete multipart uploads.
- **RDS**: idle instances (connections), oversized instances, old manual snapshots.
- **Data transfer**: cross-AZ traffic, NAT data processing vs gateway endpoints.
- **Spot / Graviton**: suggested for stateless and Arm-compatible workloads.
Each is a new function in `CHECKS` plus the IAM actions it needs (the test keeps the two in sync).
