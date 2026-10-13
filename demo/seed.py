"""Create a small, realistically wasteful AWS account in a moto server (no real AWS, no cost).

    AWS_ENDPOINT_URL=http://localhost:5055 python demo/seed.py
"""
import os
from datetime import datetime, timedelta, timezone

import boto3

assert os.environ.get("AWS_ENDPOINT_URL"), "point AWS_ENDPOINT_URL at a moto server; this must never run against real AWS"
REGION = "us-east-1"
ec2 = boto3.client("ec2", region_name=REGION)
cw = boto3.client("cloudwatch", region_name=REGION)
elb = boto3.client("elbv2", region_name=REGION)
now = datetime.now(timezone.utc)


def tags(**kv):
    return [{"Key": k.replace("_", "-"), "Value": v} for k, v in kv.items()]


def spec(kind, **kv):
    return [{"ResourceType": kind, "Tags": tags(**kv)}]


def instance(itype, cpu=None, **kv):
    iid = ec2.run_instances(ImageId=AMI, InstanceType=itype, MinCount=1, MaxCount=1,
                            TagSpecifications=spec("instance", **kv) if kv else [])["Instances"][0]["InstanceId"]
    if cpu is not None:   # 10 days of hourly CPU at roughly this level
        points = [{"MetricName": "CPUUtilization", "Dimensions": [{"Name": "InstanceId", "Value": iid}],
                   "Timestamp": now - timedelta(hours=h, minutes=30), "Value": cpu + (h % 5)} for h in range(24 * 10)]
        for i in range(0, len(points), 500):
            cw.put_metric_data(Namespace="AWS/EC2", MetricData=points[i:i + 500])
    return iid


AMI = ec2.register_image(Name="base-image", RootDeviceName="/dev/xvda")["ImageId"]

# Workloads: two oversized, one busy, one with no owner tags
reporting = instance("m5.2xlarge", cpu=6, Name="reporting", owner="data-team", cost_center="cc-210")
instance("m5.xlarge", cpu=9, Name="wiki", owner="it", cost_center="cc-100")
api = instance("c5.xlarge", cpu=70, Name="api", owner="platform", cost_center="cc-300")
instance("t3.large", cpu=30, Name="someones-test-box")

# Volumes: leftovers from deleted instances, and gp2 still attached to live ones
for size, owner in ((500, "data-team"), (200, "platform"), (100, "unknown")):
    ec2.create_volume(AvailabilityZone="us-east-1a", Size=size, VolumeType="gp2", TagSpecifications=spec("volume", owner=owner))
for iid, size, dev in ((reporting, 1000, "/dev/sdf"), (api, 300, "/dev/sdg")):
    vid = ec2.create_volume(AvailabilityZone="us-east-1a", Size=size, VolumeType="gp2")["VolumeId"]
    ec2.attach_volume(VolumeId=vid, InstanceId=iid, Device=dev)
ec2.create_volume(AvailabilityZone="us-east-1a", Size=2000, VolumeType="st1",   # kept on purpose: the ignore tag
                  TagSpecifications=spec("volume", owner="data-team", **{"costopt:ignore": "true"}))

# An old instance stopped and forgotten
old = instance("r5.xlarge", Name="legacy-batch", owner="data-team", cost_center="cc-210")
data = ec2.create_volume(AvailabilityZone="us-east-1a", Size=400, VolumeType="gp3")["VolumeId"]
ec2.attach_volume(VolumeId=data, InstanceId=old, Device="/dev/sdh")
ec2.stop_instances(InstanceIds=[old])

# Snapshots nobody uses
vol = ec2.create_volume(AvailabilityZone="us-east-1a", Size=250, VolumeType="gp3")["VolumeId"]
for _ in range(3):
    ec2.create_snapshot(VolumeId=vol, Description="nightly-2025")
ec2.delete_volume(VolumeId=vol)

# Elastic IPs: two idle, one in use
for _ in range(2):
    ec2.allocate_address(Domain="vpc")
ec2.associate_address(AllocationId=ec2.allocate_address(Domain="vpc")["AllocationId"], InstanceId=api)

# Networking: a load balancer with nothing behind it, and a NAT gateway nobody routes through
vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
subnets = [ec2.create_subnet(VpcId=vpc, CidrBlock=f"10.0.{i}.0/24", AvailabilityZone=f"us-east-1{az}")["Subnet"]["SubnetId"]
           for i, az in enumerate("ab")]
elb.create_load_balancer(Name="old-campaign-site", Subnets=subnets, Type="application", Tags=tags(owner="marketing"))
ec2.create_nat_gateway(SubnetId=subnets[0], AllocationId=ec2.allocate_address(Domain="vpc")["AllocationId"])

print("seeded: 5 running + 1 stopped instances, 8 volumes, 3 snapshots, 4 EIPs, 1 ALB, 1 NAT gateway")
