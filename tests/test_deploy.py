import pathlib
import re

import boto3

from costopt import lambda_handler
from conftest import REGION

ROOT = pathlib.Path(__file__).parents[1]
ELB_OPS = {"describe_load_balancers", "describe_target_groups", "describe_target_health", "describe_tags"}


def iam_action(op):
    service = "elasticloadbalancing" if op in ELB_OPS else "cloudwatch" if op == "get_metric_statistics" \
        else "sts" if op == "get_caller_identity" else "ec2"
    return f"{service}:{''.join(w.capitalize() for w in op.split('_'))}"


def test_iam_policies_grant_exactly_what_the_scanner_calls():
    """If a check starts calling a new API, the Terraform policies must be updated with it (and
    nothing beyond read-only calls should ever be granted)."""
    code = (ROOT / "costopt/aws.py").read_text()
    ops = set(re.findall(r"\.(describe_\w+|get_metric_statistics|get_caller_identity)\(", code))
    ops |= set(re.findall(r'"(describe_\w+)"', code))           # paginated operations
    used = {iam_action(op) for op in ops}
    for tf in ("deploy/terraform/variables.tf", "deploy/terraform/member-role/main.tf"):
        granted = set(re.findall(r'"((?:ec2|elasticloadbalancing|cloudwatch|sts):\w+)"', (ROOT / tf).read_text())) - {"sts:AssumeRole", "sts:ExternalId"}  # trust/condition keys, not scan actions
        assert granted == used, (tf, granted ^ used)
        assert all(re.match(r"\w+:(Describe|Get)", a) for a in granted)


def test_lambda_writes_report_and_notifies(ec2, monkeypatch):
    ec2.allocate_address(Domain="vpc")
    s3, sns, sqs = (boto3.client(s, region_name=REGION) for s in ("s3", "sns", "sqs"))
    s3.create_bucket(Bucket="reports")
    topic = sns.create_topic(Name="findings")["TopicArn"]
    queue = sqs.create_queue(QueueName="inbox")["QueueUrl"]
    sns.subscribe(TopicArn=topic, Protocol="sqs", Endpoint=sqs.get_queue_attributes(QueueUrl=queue, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"])
    monkeypatch.setenv("REPORT_BUCKET", "reports")
    monkeypatch.setenv("TOPIC_ARN", topic)

    result = lambda_handler.handler({}, None)

    assert result["summary"]["total_monthly_saving"] == 3.65 and not result["errors"]
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket="reports")["Contents"]]
    assert any(k.endswith("/report.md") for k in keys) and any(k.endswith("/findings.json") for k in keys)
    [msg] = sqs.receive_message(QueueUrl=queue)["Messages"]
    assert "$3.65/month" in msg["Body"]
