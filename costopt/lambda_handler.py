"""Scheduled scan (deploy/terraform): scan, store the report in S3, post a summary to SNS.
Read-only: the Lambda's role can describe and read metrics, never change resources. Fixes stay a
reviewed, manual `costopt fix` run."""
import json
import os
from datetime import datetime, timezone

import boto3

from . import aws, report


def handler(event, context):
    regions = [r for r in os.environ.get("REGIONS", "us-east-1").split(",") if r]
    roles = [r for r in os.environ.get("MEMBER_ROLE_ARNS", "").split(",") if r] or None
    errors = []
    findings = aws.scan(regions, role_arns=roles, errors=errors)
    summary = report.summary(findings)

    prefix = f"reports/{datetime.now(timezone.utc):%Y-%m-%d}"
    s3 = boto3.client("s3")
    bucket = os.environ["REPORT_BUCKET"]
    s3.put_object(Bucket=bucket, Key=f"{prefix}/report.md", Body=report.as_markdown(findings, errors).encode(), ContentType="text/markdown")
    s3.put_object(Bucket=bucket, Key=f"{prefix}/findings.json", Body=report.as_json(findings, errors).encode(), ContentType="application/json")

    top = "\n".join(f"  ${f.monthly_saving:,.2f}  {f.check}  {f.resource_id} ({f.account}/{f.region}, owner {f.owner})" for f in findings[:10])
    message = (f"Estimated saving ${summary['total_monthly_saving']:,.2f}/month across {summary['findings']} findings "
               f"(${summary['automatable_monthly_saving']:,.2f}/month fixable with `costopt fix`).\n\nTop findings:\n{top}\n\n"
               f"Full report: s3://{bucket}/{prefix}/report.md" + (f"\n\n{len(errors)} check(s) failed:\n" + "\n".join(errors) if errors else ""))
    if os.environ.get("TOPIC_ARN"):
        boto3.client("sns").publish(TopicArn=os.environ["TOPIC_ARN"], Subject="Weekly cloud cost findings", Message=message)
    return {"summary": summary, "errors": errors, "report": f"s3://{bucket}/{prefix}/report.md"}


if __name__ == "__main__":
    print(json.dumps(handler({}, None), indent=2))
