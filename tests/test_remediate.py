import boto3

from costopt import aws, remediate
from conftest import REGION, later


def findings(check):
    return [f.to_dict() for f in aws.scan([REGION], [check], now=later(100))]


def run(fs, **kw):
    return remediate.run(fs, echo=lambda *_: None, **kw)


def test_dry_run_changes_nothing(ec2):
    vid = ec2.create_volume(AvailabilityZone="us-east-1a", Size=20, VolumeType="gp2")["VolumeId"]
    [r] = run(findings("ebs-unattached"))
    assert r["status"] == "planned" and "would snapshot" in r["message"]
    assert ec2.describe_volumes(VolumeIds=[vid])["Volumes"]


def test_snapshot_then_delete(ec2):
    vid = ec2.create_volume(AvailabilityZone="us-east-1a", Size=20, VolumeType="gp2",
                            TagSpecifications=[{"ResourceType": "volume", "Tags": [{"Key": "owner", "Value": "team-a"}]}])["VolumeId"]
    [r] = run(findings("ebs-unattached"), apply=True)
    assert r["status"] == "applied"
    assert not ec2.describe_volumes(Filters=[{"Name": "volume-id", "Values": [vid]}])["Volumes"]
    [snap] = ec2.describe_snapshots(Filters=[{"Name": "tag:costopt:source-volume", "Values": [vid]}])["Snapshots"]
    assert {"Key": "owner", "Value": "team-a"} in snap["Tags"]   # original tags carried over


def test_state_is_rechecked_before_acting(ec2, instance):
    vid = ec2.create_volume(AvailabilityZone="us-east-1a", Size=20, VolumeType="gp2")["VolumeId"]
    fs = findings("ebs-unattached")
    ec2.attach_volume(VolumeId=vid, InstanceId=instance(), Device="/dev/sdf")   # someone attaches it after the scan
    [r] = run(fs, apply=True)
    assert r["status"] == "skipped" and "attached" in r["message"]
    assert ec2.describe_volumes(VolumeIds=[vid])["Volumes"]


def test_ignore_tag_added_after_scan_is_respected(ec2):
    vid = ec2.create_volume(AvailabilityZone="us-east-1a", Size=20, VolumeType="gp2")["VolumeId"]
    fs = findings("ebs-unattached")
    ec2.create_tags(Resources=[vid], Tags=[{"Key": "costopt:ignore", "Value": "true"}])
    assert run(fs, apply=True)[0]["status"] == "skipped"


def test_gp3_modify_matches_gp2_performance(ec2, instance):
    vid = ec2.create_volume(AvailabilityZone="us-east-1a", Size=2000, VolumeType="gp2")["VolumeId"]
    ec2.attach_volume(VolumeId=vid, InstanceId=instance(), Device="/dev/sdf")
    [r] = run([f for f in findings("ebs-gp2-to-gp3") if f["resource_id"] == vid], apply=True)
    assert r["status"] == "applied" and "6000 IOPS, 250 MB/s" in r["message"]
    assert ec2.describe_volumes(VolumeIds=[vid])["Volumes"][0]["VolumeType"] == "gp3"


def test_release_eip_unless_associated_since(ec2, instance):
    ec2.allocate_address(Domain="vpc")
    b = ec2.allocate_address(Domain="vpc")["AllocationId"]
    fs = findings("eip-unassociated")
    ec2.associate_address(AllocationId=b, InstanceId=instance())
    statuses = {r["resource_id"]: r["status"] for r in run(fs, apply=True)}
    assert statuses[b] == "skipped" and list(statuses.values()).count("applied") == 1
    assert [a["AllocationId"] for a in ec2.describe_addresses()["Addresses"]] == [b]


def test_snapshot_used_by_new_ami_is_kept(ec2):
    vid = ec2.create_volume(AvailabilityZone="us-east-1a", Size=20, VolumeType="gp3")["VolumeId"]
    sid = ec2.create_snapshot(VolumeId=vid)["SnapshotId"]
    [f] = [f for f in findings("snapshot-old") if f["resource_id"] == sid]
    # Someone builds an AMI from it after the scan (moto makes its own snapshot for a new AMI, so
    # point the finding at that one).
    image = ec2.register_image(Name="new-ami", RootDeviceName="/dev/xvda")["ImageId"]
    f["resource_id"] = ec2.describe_images(ImageIds=[image])["Images"][0]["BlockDeviceMappings"][0]["Ebs"]["SnapshotId"]
    [r] = run([f], apply=True)
    assert r["status"] == "skipped" and "used by ami-" in r["message"]


def test_max_actions_and_other_accounts(ec2):
    for _ in range(3):
        ec2.create_volume(AvailabilityZone="us-east-1a", Size=20, VolumeType="gp2")
    fs = findings("ebs-unattached")
    fs[0] = {**fs[0], "account": "999999999999"}
    results = run(fs, apply=True, max_actions=1)
    assert [r["status"] for r in results] == ["skipped", "applied", "skipped"]
    assert "account 999999999999" in results[0]["message"] and "max actions" in results[2]["message"]


def test_human_only_findings_are_never_acted_on(instance):
    ec2 = boto3.client("ec2", region_name=REGION)
    iid = instance("t3.large")
    ec2.stop_instances(InstanceIds=[iid])
    assert run(findings("ec2-stopped"), apply=True) == []


def test_audit_log(tmp_path, ec2):
    ec2.allocate_address(Domain="vpc")
    log = tmp_path / "audit.jsonl"
    run(findings("eip-unassociated"), apply=True, audit_path=str(log))
    assert '"status": "applied"' in log.read_text()
