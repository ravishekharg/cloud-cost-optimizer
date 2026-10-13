import os

os.environ.setdefault("MOTO_EC2_LOAD_DEFAULT_AMIS", "false")  # moto's thousands of stock AMIs/snapshots make full scans slow
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

REGION = "us-east-1"


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
                 "AWS_DEFAULT_REGION": REGION}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        yield


@pytest.fixture
def ec2():
    return boto3.client("ec2", region_name=REGION)


def later(days):
    """Moto stamps resources with the real time; scanning 'days' later makes them that old."""
    return datetime.now(timezone.utc) + timedelta(days=days)


@pytest.fixture
def instance(ec2):
    image = ec2.register_image(Name="base-image", RootDeviceName="/dev/xvda")["ImageId"]

    def make(itype="m5.xlarge", tags=None):
        kw = {"TagSpecifications": [{"ResourceType": "instance", "Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}]} if tags else {}
        return ec2.run_instances(ImageId=image, InstanceType=itype, MinCount=1, MaxCount=1, **kw)["Instances"][0]["InstanceId"]
    return make
