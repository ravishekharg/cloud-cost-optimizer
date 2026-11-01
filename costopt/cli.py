"""costopt aws   - scan accounts/regions for waste
costopt k8s   - rightsize Kubernetes requests from Prometheus
costopt fix   - apply the safe fixes from a JSON findings file (dry run unless --apply)
costopt commit - how much Savings Plan commitment to buy, from hourly on-demand spend
"""
import argparse
import json
import sys

from . import aws, commit, k8s, remediate, report


def emit(text, out):
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)


def main(argv=None):
    p = argparse.ArgumentParser(prog="costopt", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("aws", help="scan AWS for waste")
    a.add_argument("--regions", default="us-east-1", help="comma-separated")
    a.add_argument("--role-arn", action="append", help="assume this role per account (repeatable); default: current credentials")
    a.add_argument("--checks", help=f"comma-separated subset of: {', '.join(aws.CHECKS)}")
    a.add_argument("--required-tags", default="owner,cost-center")
    a.add_argument("--min-age-days", type=int, default=7, help="ignore resources younger than this")
    a.add_argument("--snapshot-age-days", type=int, default=90)
    a.add_argument("--stopped-days", type=int, default=30)
    a.add_argument("--lookback-days", type=int, default=14, help="CloudWatch window for utilisation checks")
    a.add_argument("--min-saving", type=float, default=1.0, help="hide findings worth less than this per month")
    a.add_argument("--cpu-threshold", type=float, default=40.0, help="rightsize when p95 of hourly max CPU is below this")
    a.add_argument("--format", choices=report.FORMATS, default="text")
    a.add_argument("--out")
    a.add_argument("--fail-over", type=float, help="exit 2 if the monthly saving found exceeds this (for CI/scheduled gates)")

    k = sub.add_parser("k8s", help="rightsize Kubernetes requests from Prometheus")
    k.add_argument("--prometheus", required=True, help="e.g. http://localhost:9090")
    k.add_argument("--cluster", default="cluster")
    k.add_argument("--window", default="7d", help="PromQL duration; use at least a week covering peak days")
    k.add_argument("--headroom", type=float, default=0.2)
    k.add_argument("--exclude-namespaces", default="kube-system")
    k.add_argument("--namespaces", default="", help="only these (comma-separated); default: all")
    k.add_argument("--format", choices=report.FORMATS, default="text")
    k.add_argument("--out")

    f = sub.add_parser("fix", help="apply safe fixes from `costopt aws --format json` output")
    f.add_argument("findings")
    f.add_argument("--apply", action="store_true", help="actually change things (default: print the plan)")
    f.add_argument("--only", help=f"comma-separated subset of: {', '.join(remediate.ACTIONS)}")
    f.add_argument("--max-actions", type=int, default=20)
    f.add_argument("--audit-log", default="costopt-audit.jsonl")

    c = sub.add_parser("commit", help="optimal Savings Plan commitment from hourly spend")
    src = c.add_mutually_exclusive_group(required=True)
    src.add_argument("--usage", help="CSV with an hourly 'spend' column, or 'sample' for a generated month")
    src.add_argument("--from-cost-explorer", action="store_true", help="hourly compute spend from Cost Explorer")
    c.add_argument("--days", type=int, default=14)
    c.add_argument("--discount", type=float, default=commit.DEFAULT_DISCOUNT,
                   help="plan discount vs on-demand, e.g. 0.28 (check yours for term and payment option)")
    c.add_argument("--drop", type=float, default=0.2, help="also show the result if usage falls by this share")
    c.add_argument("--format", choices=["markdown", "json"], default="markdown")
    c.add_argument("--out")

    args = p.parse_args(argv)
    if args.cmd == "commit":
        if args.from_cost_explorer:
            import boto3
            spend = commit.from_cost_explorer(boto3.Session(), args.days)
        else:
            spend = commit.sample_series() if args.usage == "sample" else commit.load_csv(args.usage)
        r = commit.recommend(spend, args.discount, args.drop)
        emit(commit.markdown(r) if args.format == "markdown" else json.dumps(r, indent=2) + "\n", args.out)
        return 0
    if args.cmd == "aws":
        errors = []
        cfg = aws.Config(required_tags=[t for t in args.required_tags.split(",") if t], min_age_days=args.min_age_days,
                         snapshot_age_days=args.snapshot_age_days, stopped_days=args.stopped_days,
                         lookback_days=args.lookback_days, cpu_p95_threshold=args.cpu_threshold,
                         min_saving=args.min_saving)
        findings = aws.scan(args.regions.split(","), args.checks.split(",") if args.checks else None, cfg,
                            role_arns=args.role_arn, errors=errors)
        emit(report.FORMATS[args.format](findings, errors=errors), args.out)
        total = report.summary(findings)["total_monthly_saving"]
        if args.fail_over is not None and total > args.fail_over:
            print(f"saving ${total:,.2f}/month exceeds --fail-over {args.fail_over}", file=sys.stderr)
            return 2
        return 1 if errors else 0
    if args.cmd == "k8s":
        findings = k8s.rightsize(k8s.Prometheus(args.prometheus), args.cluster, args.window, args.headroom,
                                 exclude_namespaces=tuple(n for n in args.exclude_namespaces.split(",") if n),
                                 namespaces=tuple(n for n in args.namespaces.split(",") if n))
        emit(report.FORMATS[args.format](findings), args.out)
        return 0
    if args.cmd == "fix":
        with open(args.findings, encoding="utf-8") as fh:
            findings = json.load(fh)["findings"]
        if not args.apply:
            print("DRY RUN: nothing will change. Re-run with --apply to act.")
        results = remediate.run(findings, apply=args.apply, max_actions=args.max_actions,
                                only=args.only.split(",") if args.only else None, audit_path=args.audit_log)
        return 1 if any(r["status"] == "failed" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
