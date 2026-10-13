# Cost findings

**Estimated saving: $467.14/month** ($5,606/year) across 17 findings; $140.80/month can be applied with `costopt fix`.

| Check | Findings | Saving / month |
|---|---:|---:|
| ec2-rightsize | 3 | $240.61 |
| ebs-unattached | 3 | $80.00 |
| snapshot-old | 3 | $37.50 |
| nat-idle | 1 | $36.50 |
| ec2-stopped | 1 | $32.80 |
| lb-idle | 1 | $16.43 |
| ebs-gp2-to-gp3 | 2 | $16.00 |
| eip-unassociated | 2 | $7.30 |
| untagged | 1 | $0.00 |

| Saving / month | Check | Resource | Owner | Recommendation |
|---:|---|---|---|---|
| $140.16 | ec2-rightsize | `i-b56bf139ef83b0672` (us-east-1) | data-team | Resize m5.2xlarge -> m5.xlarge (needs a stop/start; check memory first: CloudWatch has no memory metric without the agent). Or move to Graviton / a Savings Plan. p95 of hourly max CPU 10.0% over 10 days; one size down halves vCPUs |
| $70.08 | ec2-rightsize | `i-de96245dd16d15e18` (us-east-1) | it | Resize m5.xlarge -> m5.large (needs a stop/start; check memory first: CloudWatch has no memory metric without the agent). Or move to Graviton / a Savings Plan. p95 of hourly max CPU 13.0% over 10 days; one size down halves vCPUs |
| $50.00 | ebs-unattached | `vol-4d1cd75f0f0cf463b` (us-east-1) | data-team | Snapshot, then delete the volume (restore from the snapshot if it turns out to be needed). 500 GiB gp2, unattached, created 2026-10-13 |
| $36.50 | nat-idle | `nat-1b99804cf04e1c3ca` (us-east-1) | unowned | Delete it (and release its EIP); for S3/DynamoDB traffic use free gateway endpoints instead of NAT. 0.0 MiB sent in 14 days |
| $32.80 | ec2-stopped | `i-118dfeb820feb9d1f` (us-east-1) | data-team | Stopped instances still pay for their volumes: create an AMI and terminate, or delete if unneeded. r5.xlarge stopped 0 days, 2 volume(s), 408 GiB |
| $30.37 | ec2-rightsize | `i-302e492b26062d97c` (us-east-1) | unowned | Resize t3.large -> t3.medium (needs a stop/start; check memory first: CloudWatch has no memory metric without the agent). Or move to Graviton / a Savings Plan. p95 of hourly max CPU 34.0% over 10 days; one size down halves vCPUs |
| $20.00 | ebs-unattached | `vol-96d8bcffa14225c50` (us-east-1) | platform | Snapshot, then delete the volume (restore from the snapshot if it turns out to be needed). 200 GiB gp2, unattached, created 2026-10-13 |
| $16.43 | lb-idle | `old-campaign-site` (us-east-1) | marketing | Delete the load balancer (and its DNS records) if nothing should be behind it. application LB: 0 healthy targets, 0 RequestCount in 14 days; base hourly charge only (LCUs extra) |
| $15.00 | ebs-gp2-to-gp3 | `vol-d7ada4712c0dc04b2` (us-east-1) | unowned | Modify to gp3 in place (no downtime, no detach); gp3 baseline is 3000 IOPS / 125 MB/s. 1000 GiB gp2, gp3 needs $5.00/mo of extra IOPS/throughput to match |
| $12.50 | snapshot-old | `snap-a0848b2a8a3751d98` (us-east-1) | unowned | Delete, or move to a retention policy (Data Lifecycle Manager / AWS Backup); archive tier for long-term keeps. 250 GiB source volume, 0 days old, not used by any AMI. Snapshots are incremental, so this is an upper bound |
| $12.50 | snapshot-old | `snap-2f4ec480dc627d990` (us-east-1) | unowned | Delete, or move to a retention policy (Data Lifecycle Manager / AWS Backup); archive tier for long-term keeps. 250 GiB source volume, 0 days old, not used by any AMI. Snapshots are incremental, so this is an upper bound |
| $12.50 | snapshot-old | `snap-d74e62de6af627aed` (us-east-1) | unowned | Delete, or move to a retention policy (Data Lifecycle Manager / AWS Backup); archive tier for long-term keeps. 250 GiB source volume, 0 days old, not used by any AMI. Snapshots are incremental, so this is an upper bound |
| $10.00 | ebs-unattached | `vol-075525d6ca7f4ed68` (us-east-1) | unknown | Snapshot, then delete the volume (restore from the snapshot if it turns out to be needed). 100 GiB gp2, unattached, created 2026-10-13 |
| $3.65 | eip-unassociated | `eipalloc-321dd5e32989f0e11` (us-east-1) | unowned | Release the Elastic IP (check nothing allow-lists it first). 127.189.4.237 not associated with anything |
| $3.65 | eip-unassociated | `eipalloc-2194456274d31822d` (us-east-1) | unowned | Release the Elastic IP (check nothing allow-lists it first). 127.250.148.179 not associated with anything |
| $1.00 | ebs-gp2-to-gp3 | `vol-f790b58ef143f4932` (us-east-1) | unowned | Modify to gp3 in place (no downtime, no detach); gp3 baseline is 3000 IOPS / 125 MB/s. 300 GiB gp2, gp3 needs $5.00/mo of extra IOPS/throughput to match |
| $0.00 | untagged | `i-302e492b26062d97c` (us-east-1) | unowned | Add tags owner, cost-center (enforce with a tag policy / SCP so new resources can't skip them). t3.large running without owner, cost-center |
