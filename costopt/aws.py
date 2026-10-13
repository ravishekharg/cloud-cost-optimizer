"""AWS waste checks. Each check takes a Context (one account + region) and returns Findings.
Read-only: every call here is a Describe/List/GetMetricStatistics (see deploy/iam-readonly.json).

A resource tagged `costopt:ignore=true` is skipped by every check.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import math
import re

import boto3

from . import pricing
from .models import Finding

IGNORE_TAG = "costopt:ignore"


@dataclass
class Config:
    min_age_days: int = 7            # leave resources younger than this alone (deploys in flight)
    snapshot_age_days: int = 90
    stopped_days: int = 30
    lookback_days: int = 14          # CloudWatch window for utilisation checks
    cpu_p95_threshold: float = 40.0  # rightsizing: p95 of hourly max CPU below this -> one size down
    nat_idle_gb: float = 1.0
    min_saving: float = 1.0          # drop findings worth less than this per month (saving-0 findings like untagged stay)
    required_tags: list = field(default_factory=lambda: ["owner", "cost-center"])


@dataclass
class Context:
    session: boto3.session.Session
    region: str
    account: str
    config: Config
    now: datetime

    def client(self, name):
        return self.session.client(name, region_name=self.region)

    def finding(self, **kw) -> Finding:
        return Finding(region=self.region, account=self.account, **kw)


def tags_of(resource) -> dict:
    return {t["Key"]: t["Value"] for t in resource.get("Tags", []) or []}


def ignored(tags: dict) -> bool:
    return tags.get(IGNORE_TAG, "").lower() == "true"


def paginate(client, op, key, **kw):
    for page in client.get_paginator(op).paginate(**kw):
        yield from page.get(key, [])


def age_days(ctx, when: datetime) -> float:
    return (ctx.now - when).total_seconds() / 86400


def metric(ctx, namespace, name, dimensions, stat, period=3600):
    """Datapoints over the lookback window, oldest first."""
    r = ctx.client("cloudwatch").get_metric_statistics(
        Namespace=namespace, MetricName=name, Dimensions=[{"Name": k, "Value": v} for k, v in dimensions.items()],
        StartTime=ctx.now - timedelta(days=ctx.config.lookback_days), EndTime=ctx.now, Period=period, Statistics=[stat])
    return [p[stat] for p in sorted(r["Datapoints"], key=lambda p: p["Timestamp"])]


def percentile(values, q):
    s = sorted(values)
    return s[min(len(s) - 1, max(0, math.ceil(q / 100 * len(s)) - 1))]


# --- EBS -----------------------------------------------------------------------------------

def ebs_unattached(ctx):
    for v in paginate(ctx.client("ec2"), "describe_volumes", "Volumes", Filters=[{"Name": "status", "Values": ["available"]}]):
        tags = tags_of(v)
        if ignored(tags) or age_days(ctx, v["CreateTime"]) < ctx.config.min_age_days:
            continue
        cost = pricing.ebs_monthly(v["VolumeType"], v["Size"], ctx.region)
        yield ctx.finding(
            check="ebs-unattached", resource_id=v["VolumeId"], monthly_cost=cost, monthly_saving=cost, tags=tags,
            recommendation="Snapshot, then delete the volume (restore from the snapshot if it turns out to be needed)",
            detail=f"{v['Size']} GiB {v['VolumeType']}, unattached, created {v['CreateTime']:%Y-%m-%d}",
            action="snapshot-and-delete-volume")


def gp3_extra_monthly(size_gb: int) -> float:
    """What gp3 needs on top of its baseline (3000 IOPS, 125 MB/s) to match a gp2 volume."""
    gp2_iops = min(max(100, 3 * size_gb), 16000)
    gp2_throughput = 125 if size_gb <= 170 else 250   # gp2's 128 MB/s at <=170 GiB ~ gp3's 125 baseline
    return round(max(0, gp2_iops - 3000) * 0.005 + max(0, gp2_throughput - 125) * 0.04, 2)


def ebs_gp2(ctx):
    for v in paginate(ctx.client("ec2"), "describe_volumes", "Volumes", Filters=[{"Name": "volume-type", "Values": ["gp2"]}]):
        tags = tags_of(v)
        if ignored(tags) or v["State"] == "available":   # unattached ones are reported for deletion instead
            continue
        cost = pricing.ebs_monthly("gp2", v["Size"], ctx.region)
        extra = gp3_extra_monthly(v["Size"]) * pricing.multiplier(ctx.region)
        saving = round(cost - pricing.ebs_monthly("gp3", v["Size"], ctx.region) - extra, 2)
        if saving <= 0:
            continue
        yield ctx.finding(
            check="ebs-gp2-to-gp3", resource_id=v["VolumeId"], monthly_cost=cost, monthly_saving=saving, tags=tags,
            recommendation="Modify to gp3 in place (no downtime, no detach); gp3 baseline is 3000 IOPS / 125 MB/s",
            detail=f"{v['Size']} GiB gp2" + (f", gp3 needs ${extra:.2f}/mo of extra IOPS/throughput to match" if extra else ""),
            action="modify-volume-gp3")


def snapshots_old(ctx):
    ec2 = ctx.client("ec2")
    in_use = {bdm["Ebs"]["SnapshotId"]
              for image in ec2.describe_images(Owners=["self"])["Images"]
              for bdm in image.get("BlockDeviceMappings", []) if "Ebs" in bdm and "SnapshotId" in bdm["Ebs"]}
    for s in paginate(ec2, "describe_snapshots", "Snapshots", OwnerIds=["self"]):
        tags = tags_of(s)
        days = age_days(ctx, s["StartTime"])
        if ignored(tags) or days < ctx.config.snapshot_age_days or s["SnapshotId"] in in_use:
            continue
        cost = round(s["VolumeSize"] * pricing.SNAPSHOT_GB_MONTH * pricing.multiplier(ctx.region), 2)
        yield ctx.finding(
            check="snapshot-old", resource_id=s["SnapshotId"], monthly_cost=cost, monthly_saving=cost, tags=tags,
            recommendation=f"Delete, or move to a retention policy (Data Lifecycle Manager / AWS Backup); archive tier for long-term keeps",
            detail=f"{s['VolumeSize']} GiB source volume, {days:.0f} days old, not used by any AMI. "
                   "Snapshots are incremental, so this is an upper bound",
            action="delete-snapshot")


# --- EC2 and networking --------------------------------------------------------------------

def eip_unassociated(ctx):
    for a in ctx.client("ec2").describe_addresses()["Addresses"]:
        tags = tags_of(a)
        if ignored(tags) or a.get("AssociationId"):
            continue
        cost = pricing.monthly(pricing.PUBLIC_IPV4_HOUR, ctx.region)
        yield ctx.finding(
            check="eip-unassociated", resource_id=a["AllocationId"], monthly_cost=cost, monthly_saving=cost, tags=tags,
            recommendation="Release the Elastic IP (check nothing allow-lists it first)",
            detail=f"{a['PublicIp']} not associated with anything", action="release-address")


def instances(ctx, state):
    for r in paginate(ctx.client("ec2"), "describe_instances", "Reservations",
                      Filters=[{"Name": "instance-state-name", "Values": [state]}]):
        yield from r["Instances"]


def ec2_rightsize(ctx):
    for i in instances(ctx, "running"):
        tags, itype = tags_of(i), i["InstanceType"]
        smaller = pricing.one_size_down(itype)
        if ignored(tags) or not smaller or itype not in pricing.EC2_HOUR:
            continue
        hourly_max = metric(ctx, "AWS/EC2", "CPUUtilization", {"InstanceId": i["InstanceId"]}, "Maximum")
        if len(hourly_max) < 24 * 7:      # under a week of data: don't judge
            continue
        p95 = percentile(hourly_max, 95)
        if p95 >= ctx.config.cpu_p95_threshold:
            continue
        cost = pricing.monthly(pricing.EC2_HOUR[itype], ctx.region)
        saving = round(cost - pricing.monthly(pricing.EC2_HOUR[smaller], ctx.region), 2)
        yield ctx.finding(
            check="ec2-rightsize", resource_id=i["InstanceId"], monthly_cost=cost, monthly_saving=saving, tags=tags,
            recommendation=f"Resize {itype} -> {smaller} (needs a stop/start; check memory first: CloudWatch has no memory "
                           "metric without the agent). Or move to Graviton / a Savings Plan",
            detail=f"p95 of hourly max CPU {p95:.1f}% over {len(hourly_max) // 24} days; one size down halves vCPUs")


TRANSITION_DATE = re.compile(r"\((\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (?:GMT|UTC)\)")  # AWS says GMT


def ec2_stopped(ctx):
    ec2 = ctx.client("ec2")
    for i in instances(ctx, "stopped"):
        tags = tags_of(i)
        m = TRANSITION_DATE.search(i.get("StateTransitionReason", ""))
        if ignored(tags) or not m:
            continue
        stopped_at = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        days = age_days(ctx, stopped_at)
        if days < ctx.config.stopped_days:
            continue
        vol_ids = [b["Ebs"]["VolumeId"] for b in i.get("BlockDeviceMappings", []) if "Ebs" in b]
        vols = ec2.describe_volumes(VolumeIds=vol_ids)["Volumes"] if vol_ids else []
        cost = round(sum(pricing.ebs_monthly(v["VolumeType"], v["Size"], ctx.region) for v in vols), 2)
        if cost == 0:
            continue
        yield ctx.finding(
            check="ec2-stopped", resource_id=i["InstanceId"], monthly_cost=cost, monthly_saving=cost, tags=tags,
            recommendation="Stopped instances still pay for their volumes: create an AMI and terminate, or delete if unneeded",
            detail=f"{i['InstanceType']} stopped {days:.0f} days, {len(vols)} volume(s), {sum(v['Size'] for v in vols)} GiB")


def lb_idle(ctx):
    elb = ctx.client("elbv2")
    groups_by_lb = {}
    for g in paginate(elb, "describe_target_groups", "TargetGroups"):
        for arn in g.get("LoadBalancerArns", []):
            groups_by_lb.setdefault(arn, []).append(g["TargetGroupArn"])
    for lb in paginate(elb, "describe_load_balancers", "LoadBalancers"):
        if age_days(ctx, lb["CreatedTime"]) < ctx.config.min_age_days:
            continue
        tags = {t["Key"]: t["Value"] for d in elb.describe_tags(ResourceArns=[lb["LoadBalancerArn"]])["TagDescriptions"] for t in d["Tags"]}
        if ignored(tags):
            continue
        healthy = sum(1 for g in groups_by_lb.get(lb["LoadBalancerArn"], [])
                      for t in elb.describe_target_health(TargetGroupArn=g)["TargetHealthDescriptions"]
                      if t["TargetHealth"]["State"] == "healthy")
        dim = lb["LoadBalancerArn"].split(":loadbalancer/")[1]
        name, namespace = ("RequestCount", "AWS/ApplicationELB") if lb["Type"] == "application" else ("NewFlowCount", "AWS/NetworkELB")
        traffic = sum(metric(ctx, namespace, name, {"LoadBalancer": dim}, "Sum", period=86400))
        if healthy and traffic > 0:
            continue
        cost = pricing.monthly(pricing.LOAD_BALANCER_HOUR.get(lb["Type"], 0.0225), ctx.region)
        yield ctx.finding(
            check="lb-idle", resource_id=lb["LoadBalancerName"], monthly_cost=cost, monthly_saving=cost, tags=tags,
            recommendation="Delete the load balancer (and its DNS records) if nothing should be behind it",
            detail=f"{lb['Type']} LB: {healthy} healthy targets, {traffic:.0f} {name} in {ctx.config.lookback_days} days; "
                   "base hourly charge only (LCUs extra)")


def nat_idle(ctx):
    for n in ctx.client("ec2").describe_nat_gateways(Filter=[{"Name": "state", "Values": ["available"]}])["NatGateways"]:
        tags = tags_of(n)
        if ignored(tags) or age_days(ctx, n["CreateTime"]) < ctx.config.min_age_days:
            continue
        sent = sum(metric(ctx, "AWS/NATGateway", "BytesOutToDestination", {"NatGatewayId": n["NatGatewayId"]}, "Sum", period=86400))
        if sent >= ctx.config.nat_idle_gb * 1024**3:
            continue
        cost = pricing.monthly(pricing.NAT_GATEWAY_HOUR + pricing.PUBLIC_IPV4_HOUR, ctx.region)
        yield ctx.finding(
            check="nat-idle", resource_id=n["NatGatewayId"], monthly_cost=cost, monthly_saving=cost, tags=tags,
            recommendation="Delete it (and release its EIP); for S3/DynamoDB traffic use free gateway endpoints instead of NAT",
            detail=f"{sent / 1024**2:.1f} MiB sent in {ctx.config.lookback_days} days")


def untagged(ctx):
    """Not waste itself, but spend nobody can be asked about. Reported with saving 0."""
    required = ctx.config.required_tags
    for i in instances(ctx, "running"):
        tags = tags_of(i)
        missing = [t for t in required if t not in tags]
        if ignored(tags) or not missing:
            continue
        cost = pricing.monthly(pricing.EC2_HOUR.get(i["InstanceType"], 0), ctx.region)
        yield ctx.finding(
            check="untagged", resource_id=i["InstanceId"], monthly_cost=cost, monthly_saving=0.0, tags=tags,
            recommendation=f"Add tags {', '.join(missing)} (enforce with a tag policy / SCP so new resources can't skip them)",
            detail=f"{i['InstanceType']} running without {', '.join(missing)}")


CHECKS = {
    "ebs-unattached": ebs_unattached, "ebs-gp2-to-gp3": ebs_gp2, "snapshot-old": snapshots_old,
    "eip-unassociated": eip_unassociated, "ec2-rightsize": ec2_rightsize, "ec2-stopped": ec2_stopped,
    "lb-idle": lb_idle, "nat-idle": nat_idle, "untagged": untagged,
}


def sessions(role_arns=None, base=None):
    """(session, account id) for the current credentials, or for each role assumed from them."""
    base = base or boto3.session.Session()
    if not role_arns:
        yield base, base.client("sts").get_caller_identity()["Account"]
        return
    sts = base.client("sts")
    for arn in role_arns:
        c = sts.assume_role(RoleArn=arn, RoleSessionName="costopt-scan")["Credentials"]
        s = boto3.session.Session(aws_access_key_id=c["AccessKeyId"], aws_secret_access_key=c["SecretAccessKey"],
                                  aws_session_token=c["SessionToken"])
        yield s, arn.split(":")[4]


def scan(regions, checks=None, config=None, role_arns=None, base_session=None, now=None, errors=None):
    """Run the checks in every account x region. A check that fails (e.g. AccessDenied) is recorded
    in `errors` and the scan carries on."""
    config, now = config or Config(), now or datetime.now(timezone.utc)
    findings = []
    for session, account in sessions(role_arns, base_session):
        for region in regions:
            ctx = Context(session, region, account, config, now)
            for name in checks or CHECKS:
                try:
                    findings.extend(CHECKS[name](ctx))
                except Exception as e:  # one broken check must not hide the others' findings
                    if errors is not None:
                        errors.append(f"{account}/{region}/{name}: {e.__class__.__name__}: {e}")
                    else:
                        raise
    findings = [f for f in findings if f.monthly_saving == 0 or f.monthly_saving >= config.min_saving]
    return sorted(findings, key=lambda f: f.monthly_saving, reverse=True)
