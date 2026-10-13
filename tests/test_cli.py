import json

from costopt import cli, report
from costopt.models import Finding


def test_aws_json_then_fix_dry_run(ec2, tmp_path, capsys, monkeypatch):
    ec2.allocate_address(Domain="vpc")
    out = tmp_path / "findings.json"
    assert cli.main(["aws", "--checks", "eip-unassociated", "--format", "json", "--out", str(out)]) == 0
    data = json.loads(out.read_text())
    assert data["summary"]["total_monthly_saving"] == 3.65
    assert cli.main(["fix", str(out)]) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert len(ec2.describe_addresses()["Addresses"]) == 1


def test_fail_over_threshold(ec2):
    ec2.allocate_address(Domain="vpc")
    assert cli.main(["aws", "--checks", "eip-unassociated", "--fail-over", "1"]) == 2
    assert cli.main(["aws", "--checks", "eip-unassociated", "--fail-over", "100"]) == 0


def test_reports():
    fs = [Finding("ebs-unattached", "vol-1", "us-east-1", "123", 10.0, 10.0, "Delete", action="snapshot-and-delete-volume",
                  tags={"owner": "team-a"}),
          Finding("ec2-rightsize", "i-1", "us-east-1", "123", 140.16, 70.08, "Resize")]
    md = report.as_markdown(fs)
    assert "$80.08/month" in md and "$10.00/month can be applied" in md and "| team-a |" in md
    assert report.as_csv(fs).splitlines()[0].startswith("check,resource_id")
    assert "Total: $80.08/month" in report.as_text(fs)
    assert list(report.summary(fs)["by_check"]) == ["ec2-rightsize", "ebs-unattached"]
