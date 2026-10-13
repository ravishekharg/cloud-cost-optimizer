"""On-demand list prices, us-east-1, USD, as published by AWS (checked 2026-10). A static table
keeps scans fast, offline and deterministic; savings are estimates for prioritising work, not a
bill. Other regions are priced with a multiplier (most are within ~5-25% of us-east-1); pass a
real table for exact figures (the Pricing API's GetProducts is the source).
"""
HOURS_PER_MONTH = 730

EBS_GB_MONTH = {"gp2": 0.10, "gp3": 0.08, "io1": 0.125, "io2": 0.125, "st1": 0.045, "sc1": 0.015, "standard": 0.05}
SNAPSHOT_GB_MONTH = 0.05
PUBLIC_IPV4_HOUR = 0.005          # every public IPv4 address, attached or not (since Feb 2024)
LOAD_BALANCER_HOUR = {"application": 0.0225, "network": 0.0225, "gateway": 0.0125, "classic": 0.025}
NAT_GATEWAY_HOUR = 0.045

EC2_HOUR = {
    "t3.micro": 0.0104, "t3.small": 0.0208, "t3.medium": 0.0416, "t3.large": 0.0832, "t3.xlarge": 0.1664, "t3.2xlarge": 0.3328,
    "m5.large": 0.096, "m5.xlarge": 0.192, "m5.2xlarge": 0.384, "m5.4xlarge": 0.768,
    "m6i.large": 0.096, "m6i.xlarge": 0.192, "m6i.2xlarge": 0.384, "m6i.4xlarge": 0.768,
    "c5.large": 0.085, "c5.xlarge": 0.17, "c5.2xlarge": 0.34, "c5.4xlarge": 0.68,
    "r5.large": 0.126, "r5.xlarge": 0.252, "r5.2xlarge": 0.504, "r5.4xlarge": 1.008,
}
SIZES = ["nano", "micro", "small", "medium", "large", "xlarge", "2xlarge", "4xlarge", "8xlarge"]

# Kubernetes: what one requested vCPU / GiB costs per hour, splitting an m6i.large ($0.096/h for
# 2 vCPU + 8 GiB) the usual way (a vCPU priced like ~7.5 GiB). Freed requests become real savings
# only when the autoscaler (Karpenter / Cluster Autoscaler) consolidates nodes.
K8S_VCPU_HOUR = 0.0316
K8S_GIB_HOUR = 0.0041

REGION_MULTIPLIER = {"us-east-1": 1.0, "us-east-2": 1.0, "us-west-2": 1.0, "eu-west-1": 1.1, "eu-central-1": 1.15,
                     "ap-south-1": 1.05, "ap-southeast-1": 1.2, "ap-northeast-1": 1.25, "sa-east-1": 1.5}


def multiplier(region: str) -> float:
    return REGION_MULTIPLIER.get(region, 1.1)


def monthly(hourly: float, region: str = "us-east-1") -> float:
    return round(hourly * HOURS_PER_MONTH * multiplier(region), 2)


def ebs_monthly(volume_type: str, size_gb: int, region: str = "us-east-1") -> float:
    return round(EBS_GB_MONTH.get(volume_type, 0.10) * size_gb * multiplier(region), 2)


def one_size_down(instance_type: str):
    """m5.2xlarge -> m5.xlarge; None when there's no smaller priced size in the family."""
    family, _, size = instance_type.partition(".")
    if size not in SIZES or SIZES.index(size) == 0:
        return None
    smaller = f"{family}.{SIZES[SIZES.index(size) - 1]}"
    return smaller if smaller in EC2_HOUR else None
