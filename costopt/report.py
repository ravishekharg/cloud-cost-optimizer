import csv
import io
import json
from collections import defaultdict

FIELDS = ["check", "resource_id", "account", "region", "owner", "monthly_cost", "monthly_saving", "action", "recommendation", "detail"]


def summary(findings):
    by_check = defaultdict(lambda: [0, 0.0])
    for f in findings:
        by_check[f.check][0] += 1
        by_check[f.check][1] += f.monthly_saving
    return {"total_monthly_saving": round(sum(f.monthly_saving for f in findings), 2),
            "automatable_monthly_saving": round(sum(f.monthly_saving for f in findings if f.action), 2),
            "findings": len(findings),
            "by_check": {k: {"count": c, "monthly_saving": round(s, 2)}
                         for k, (c, s) in sorted(by_check.items(), key=lambda kv: -kv[1][1])}}


def as_json(findings, errors=()):
    return json.dumps({"summary": summary(findings), "errors": list(errors), "findings": [f.to_dict() for f in findings]}, indent=2)


def as_csv(findings):
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=FIELDS, extrasaction="ignore")
    w.writeheader()
    for f in findings:
        w.writerow(f.to_dict())
    return out.getvalue()


def as_markdown(findings, errors=(), title="Cost findings"):
    s = summary(findings)
    lines = [f"# {title}", "",
             f"**Estimated saving: ${s['total_monthly_saving']:,.2f}/month** (${s['total_monthly_saving'] * 12:,.0f}/year) "
             f"across {s['findings']} findings; ${s['automatable_monthly_saving']:,.2f}/month can be applied with `costopt fix`.", "",
             "| Check | Findings | Saving / month |", "|---|---:|---:|"]
    lines += [f"| {k} | {v['count']} | ${v['monthly_saving']:,.2f} |" for k, v in s["by_check"].items()]
    lines += ["", "| Saving / month | Check | Resource | Owner | Recommendation |", "|---:|---|---|---|---|"]
    lines += [f"| ${f.monthly_saving:,.2f} | {f.check} | `{f.resource_id}` ({f.region}) | {f.owner} | {f.recommendation}. {f.detail} |"
              for f in findings]
    if errors:
        lines += ["", "## Checks that could not run", ""] + [f"- {e}" for e in errors]
    return "\n".join(lines) + "\n"


def as_text(findings, errors=()):
    s = summary(findings)
    rows = [(f"${f.monthly_saving:,.2f}", f.check, f.resource_id, f.region, f.owner, f.recommendation[:70]) for f in findings]
    head = ("SAVING/MO", "CHECK", "RESOURCE", "REGION", "OWNER", "RECOMMENDATION")
    widths = [max(len(str(r[i])) for r in rows + [head]) for i in range(len(head))]
    fmt = "  ".join(f"{{:{'>' if i == 0 else '<'}{w}}}" for i, w in enumerate(widths))
    out = [fmt.format(*head)] + [fmt.format(*r) for r in rows]
    out += ["", f"Total: ${s['total_monthly_saving']:,.2f}/month across {s['findings']} findings "
                f"(${s['automatable_monthly_saving']:,.2f}/month automatable with `costopt fix`)"]
    out += [f"ERROR {e}" for e in errors]
    return "\n".join(out) + "\n"


FORMATS = {"text": as_text, "markdown": as_markdown, "json": as_json, "csv": lambda f, errors=(): as_csv(f)}
