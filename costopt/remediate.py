"""Apply the safe, reversible-or-recoverable fixes from a findings file. Dry run unless apply=True.

Only four actions exist, all chosen because they're either reversible or recoverable:
  snapshot-and-delete-volume  snapshot first (tagged with the source volume), wait for it, then delete
  modify-volume-gp3           in place, online, with IOPS/throughput set to at least what gp2 gave
  release-address             (re-allocating the *same* IP is not guaranteed: allow-lists are the risk)
  delete-snapshot             re-checked against AMIs at apply time
Rightsizing, stopped instances, load balancers and NAT gateways need a human: no action is offered.

Before acting, each resource's current state is re-read (the findings file may be days old):
anything attached, associated, tagged costopt:ignore=true, or gone since the scan is skipped.
"""
import json
import time

import boto3

from .aws import IGNORE_TAG, tags_of

ACTIONS = ("snapshot-and-delete-volume", "modify-volume-gp3", "release-address", "delete-snapshot")


class Skip(Exception):
    pass


def _not_ignored(tags):
    if tags.get(IGNORE_TAG, "").lower() == "true":
        raise Skip(f"tagged {IGNORE_TAG}=true since the scan")


def _volume(ec2, vid):
    vols = ec2.describe_volumes(Filters=[{"Name": "volume-id", "Values": [vid]}])["Volumes"]
    if not vols:
        raise Skip("no longer exists")
    _not_ignored(tags_of(vols[0]))
    return vols[0]


def snapshot_and_delete_volume(ec2, f, apply):
    v = _volume(ec2, f["resource_id"])
    if v["State"] != "available":
        raise Skip(f"now {v['State']} (attached since the scan)")
    if not apply:
        return f"would snapshot {v['VolumeId']} ({v['Size']} GiB) then delete it"
    snap = ec2.create_snapshot(VolumeId=v["VolumeId"], Description=f"costopt: before deleting {v['VolumeId']}",
                               TagSpecifications=[{"ResourceType": "snapshot", "Tags": [
                                   {"Key": "costopt:source-volume", "Value": v["VolumeId"]},
                                   {"Key": "costopt:deleted-at", "Value": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                                   *[t for t in v.get("Tags", []) if not t["Key"].startswith("aws:")]]}])
    ec2.get_waiter("snapshot_completed").wait(SnapshotIds=[snap["SnapshotId"]], WaiterConfig={"Delay": 5, "MaxAttempts": 120})
    ec2.delete_volume(VolumeId=v["VolumeId"])
    return f"snapshot {snap['SnapshotId']} created, volume {v['VolumeId']} deleted"


def modify_volume_gp3(ec2, f, apply):
    v = _volume(ec2, f["resource_id"])
    if v["VolumeType"] != "gp2":
        raise Skip(f"already {v['VolumeType']}")
    iops = max(3000, min(max(100, 3 * v["Size"]), 16000))
    throughput = 125 if v["Size"] <= 170 else 250
    if not apply:
        return f"would modify {v['VolumeId']} gp2 -> gp3 ({iops} IOPS, {throughput} MB/s)"
    ec2.modify_volume(VolumeId=v["VolumeId"], VolumeType="gp3", Iops=iops, Throughput=throughput)
    return f"modifying {v['VolumeId']} to gp3 ({iops} IOPS, {throughput} MB/s); runs online"


def release_address(ec2, f, apply):
    addrs = ec2.describe_addresses(Filters=[{"Name": "allocation-id", "Values": [f["resource_id"]]}])["Addresses"]
    if not addrs:
        raise Skip("no longer exists")
    a = addrs[0]
    _not_ignored(tags_of(a))
    if a.get("AssociationId"):
        raise Skip("associated since the scan")
    if not apply:
        return f"would release {a['PublicIp']}"
    ec2.release_address(AllocationId=a["AllocationId"])
    return f"released {a['PublicIp']}"


def delete_snapshot(ec2, f, apply):
    snaps = ec2.describe_snapshots(Filters=[{"Name": "snapshot-id", "Values": [f["resource_id"]]}], OwnerIds=["self"])["Snapshots"]
    if not snaps:
        raise Skip("no longer exists")
    _not_ignored(tags_of(snaps[0]))
    for image in ec2.describe_images(Owners=["self"])["Images"]:
        if any(b.get("Ebs", {}).get("SnapshotId") == f["resource_id"] for b in image.get("BlockDeviceMappings", [])):
            raise Skip(f"now used by {image['ImageId']}")
    if not apply:
        return f"would delete {f['resource_id']}"
    ec2.delete_snapshot(SnapshotId=f["resource_id"])
    return f"deleted {f['resource_id']}"


HANDLERS = {"snapshot-and-delete-volume": snapshot_and_delete_volume, "modify-volume-gp3": modify_volume_gp3,
            "release-address": release_address, "delete-snapshot": delete_snapshot}


def run(findings, apply=False, max_actions=20, only=None, session=None, audit_path=None, echo=print):
    """Returns a list of {resource_id, action, status, message}. Same-account credentials only:
    findings from other accounts are skipped (run per account, with that account's role)."""
    session = session or boto3.session.Session()
    account = session.client("sts").get_caller_identity()["Account"]
    todo = [f for f in findings if f.get("action") in ACTIONS and (not only or f["action"] in only)]
    results, done = [], 0
    for f in todo:
        entry = {"resource_id": f["resource_id"], "region": f["region"], "action": f["action"], "saving": f["monthly_saving"]}
        try:
            if f["account"] != account:
                raise Skip(f"belongs to account {f['account']}, credentials are for {account}")
            if done >= max_actions:
                raise Skip(f"max actions ({max_actions}) reached")
            msg = HANDLERS[f["action"]](session.client("ec2", region_name=f["region"]), f, apply)
            entry.update(status="applied" if apply else "planned", message=msg)
            done += 1
        except Skip as e:
            entry.update(status="skipped", message=str(e))
        except Exception as e:  # report and continue; nothing half-done is left (each handler acts last)
            entry.update(status="failed", message=f"{e.__class__.__name__}: {e}")
        echo(f"{entry['status']:8} {entry['action']:27} {entry['resource_id']:24} {entry['message']}")
        results.append(entry)
    if audit_path and apply:
        with open(audit_path, "a") as fh:
            for e in results:
                fh.write(json.dumps({**e, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "account": account}) + "\n")
    return results
