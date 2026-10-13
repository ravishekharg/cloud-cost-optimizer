from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class Finding:
    check: str                  # e.g. "ebs-unattached"
    resource_id: str
    region: str
    account: str
    monthly_cost: float         # what the resource costs now (USD/month)
    monthly_saving: float       # what acting on the recommendation saves (USD/month)
    recommendation: str
    detail: str = ""
    action: Optional[str] = None  # a remediation `costopt fix` can apply; None = needs a human
    tags: dict = field(default_factory=dict)

    @property
    def owner(self) -> str:
        return self.tags.get("owner") or self.tags.get("Owner") or "unowned"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["owner"] = self.owner
        return d
