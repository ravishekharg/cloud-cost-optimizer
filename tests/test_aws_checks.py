from datetime import datetime, timedelta, timezone

import boto3
import pytest

from costopt import aws, pricing
from conftest import REGION, later


def scan(check, days=0, **cfg):
    errors = []
    found = aws.scan([REGION], [check], aws.Config(**cfg), now=later(days), errors=errors)
    assert not errors, errors
    return found


def tag(key, value):
    return [{"Key": key, "Value": value}]


# --- EBS -------------------------------------------------------------------------------------

def test_unattached_volume_priced_and_actionable(ec2):
    vid = ec2.create_volume(AvailabilityZone="us-east-1a", Size=100, VolumeType="gp2")["VolumeId"]
    [f] = scan("ebs-unattached", days=10)
    assert (f.resource_id, f.monthly_saving, f.action) == (vid, 10.0, "snapshot-and-delete-volume")


def test_new_and_ignored_volumes_left_alone(ec2):
    ec2.create_volume(AvailabilityZone="us-east-1a", Size=100, VolumeType="gp3")
    assert scan("ebs-unattached", days=1) == []          # younger than min_age_days
    ec2.create_volume(AvailabilityZone="us-east-1a", Size=50, VolumeType="gp3",
                      TagSpecifications=[{"ResourceType": "volume", "Tags": tag("costopt:ignore", "true")}])
    assert [f.monthly_saving for f in scan("ebs-unattached", days=10)] == [8.0]   # only the untagged 100 GiB gp3


@pytest.mark.parametrize("size, saving", [
    (100, 2.0),      # $10 -> $8, gp3 baseline covers gp2's 300 IOPS
    (1000, 15.0),    # $100 -> $80 + $5 to match gp2's 250 MB/s
    (5000, 35.0),    # $500 -> $400 + $60 (15000 IOPS) + $5
])
def test_gp2_to_gp3_accounts_for_performance(ec2, instance, size, saving):
    iid = instance()
    vid = ec2.create_volume(AvailabilityZone="us-east-1a", Size=size, VolumeType="gp2")["VolumeId"]
    ec2.attach_volume(VolumeId=vid, InstanceId=iid, Device="/dev/sdf")
    found = {f.resource_id: f for f in scan("ebs-gp2-to-gp3")}
    assert found[vid].monthly_saving == saving and found[vid].action == "modify-volume-gp3"


def test_old_snapshots_except_those_behind_an_ami(ec2, instance):
    vid = ec2.create_volume(AvailabilityZone="us-east-1a", Size=40, VolumeType="gp3")["VolumeId"]
    orphan = ec2.create_snapshot(VolumeId=vid)["SnapshotId"]
    image = ec2.register_image(Name="golden", RootDeviceName="/dev/xvda")["ImageId"]
    used = ec2.describe_images(ImageIds=[image])["Images"][0]["BlockDeviceMappings"][0]["Ebs"]["SnapshotId"]
    mine = {orphan, used}   # moto also preloads snapshots of its stock AMIs
    assert [f for f in scan("snapshot-old", days=30) if f.resource_id in mine] == []
    found = {f.resource_id for f in scan("snapshot-old", days=100)} & mine
    assert found == {orphan}


# --- EC2 and networking ----------------------------------------------------------------------

def test_unassociated_eip(ec2, instance):
    free = ec2.allocate_address(Domain="vpc")["AllocationId"]
    used = ec2.allocate_address(Domain="vpc")["AllocationId"]
    ec2.associate_address(AllocationId=used, InstanceId=instance())
    [f] = scan("eip-unassociated")
    assert f.resource_id == free and f.monthly_saving == 3.65


def put_cpu(instance_id, percent, hours):
    cw = boto3.client("cloudwatch", region_name=REGION)
    now = datetime.now(timezone.utc)
    points = [{"MetricName": "CPUUtilization", "Dimensions": [{"Name": "InstanceId", "Value": instance_id}],
               "Timestamp": now - timedelta(hours=h, minutes=30), "Value": percent} for h in range(hours)]
    for i in range(0, len(points), 500):
        cw.put_metric_data(Namespace="AWS/EC2", MetricData=points[i:i + 500])


def test_rightsizing_idle_instance_one_size_down(instance):
    idle, busy, new = instance("m5.xlarge"), instance("m5.xlarge"), instance("m5.xlarge")
    put_cpu(idle, 12.0, 24 * 10)
    put_cpu(busy, 75.0, 24 * 10)
    put_cpu(new, 5.0, 24 * 3)           # not enough history to judge
    [f] = scan("ec2-rightsize")
    assert f.resource_id == idle
    assert f.monthly_saving == round(pricing.monthly(0.192) - pricing.monthly(0.096), 2) == 70.08
    assert "m5.large" in f.recommendation and f.action is None   # resizing is a human decision


def test_long_stopped_instance_pays_for_its_volumes(ec2, instance):
    iid = instance("t3.large")
    data = ec2.create_volume(AvailabilityZone="us-east-1a", Size=100, VolumeType="gp3")["VolumeId"]
    ec2.attach_volume(VolumeId=data, InstanceId=iid, Device="/dev/sdf")
    ec2.stop_instances(InstanceIds=[iid])
    assert scan("ec2-stopped", days=5) == []
    [f] = scan("ec2-stopped", days=40)
    assert f.resource_id == iid and f.monthly_saving == 8.8 and f.action is None   # 100 GiB gp3 + 8 GiB root


def network(ec2):
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnets = [ec2.create_subnet(VpcId=vpc, CidrBlock=f"10.0.{i}.0/24", AvailabilityZone=f"us-east-1{az}")["Subnet"]["SubnetId"]
               for i, az in enumerate("ab")]
    return vpc, subnets


def test_load_balancer_without_targets_is_idle(ec2):
    vpc, subnets = network(ec2)
    elb = boto3.client("elbv2", region_name=REGION)
    elb.create_load_balancer(Name="forgotten", Subnets=subnets, Type="application")
    lb2 = elb.create_load_balancer(Name="kept", Subnets=subnets, Type="application", Tags=tag("costopt:ignore", "true"))
    assert lb2
    [f] = scan("lb-idle", days=10)
    assert f.resource_id == "forgotten" and f.monthly_saving == 16.43


def test_nat_gateway_without_traffic(ec2):
    vpc, subnets = network(ec2)
    eip = ec2.allocate_address(Domain="vpc")["AllocationId"]
    nat = ec2.create_nat_gateway(SubnetId=subnets[0], AllocationId=eip)["NatGateway"]["NatGatewayId"]
    [f] = scan("nat-idle", days=10)
    assert f.resource_id == nat and f.monthly_saving == pricing.monthly(0.045 + 0.005) == 36.5


def test_untagged_reports_cost_but_no_saving(instance):
    bare = instance("t3.medium")
    instance("t3.medium", tags={"owner": "team-a", "cost-center": "cc-1"})
    [f] = scan("untagged")
    assert f.resource_id == bare and f.monthly_saving == 0 and "owner, cost-center" in f.recommendation


# --- scanning --------------------------------------------------------------------------------

def test_failing_check_is_recorded_and_others_still_run(ec2, monkeypatch):
    ec2.create_volume(AvailabilityZone="us-east-1a", Size=10, VolumeType="gp2")

    def denied(ctx):
        raise PermissionError("AccessDenied: elasticloadbalancing:DescribeLoadBalancers")
        yield

    monkeypatch.setitem(aws.CHECKS, "lb-idle", denied)
    errors = []
    found = aws.scan([REGION], ["lb-idle", "ebs-unattached"], now=later(10), errors=errors)
    assert len(found) == 1 and "AccessDenied" in errors[0]


def test_regions_and_sorting(ec2):
    boto3.client("ec2", region_name="eu-west-1").create_volume(AvailabilityZone="eu-west-1a", Size=100, VolumeType="gp2")
    ec2.create_volume(AvailabilityZone="us-east-1a", Size=10, VolumeType="gp2")
    ec2.create_volume(AvailabilityZone="us-east-1a", Size=5, VolumeType="gp2")    # $0.50: below min_saving
    found = aws.scan([REGION, "eu-west-1"], ["ebs-unattached"], now=later(10))
    assert [(f.region, f.monthly_saving) for f in found] == [("eu-west-1", 11.0), (REGION, 1.0)]  # eu-west-1 x1.1


def test_assume_role_per_account():
    accounts = [account for _, account in aws.sessions(["arn:aws:iam::111111111111:role/costopt-read",
                                                        "arn:aws:iam::222222222222:role/costopt-read"])]
    assert accounts == ["111111111111", "222222222222"]
