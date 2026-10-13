variable "name" {
  type    = string
  default = "costopt"
}

variable "regions" {
  description = "Regions to scan in every account"
  type        = list(string)
  default     = ["us-east-1"]
}

variable "member_role_arns" {
  description = "Read-only roles to assume in other accounts (see member-role/); empty = scan this account only"
  type        = list(string)
  default     = []
}

variable "schedule" {
  type    = string
  default = "cron(0 6 ? * MON *)" # Mondays 06:00 UTC
}

variable "notification_emails" {
  type    = list(string)
  default = []
}

variable "report_retention_days" {
  type    = number
  default = 400
}

variable "tags" {
  type    = map(string)
  default = {}
}

locals {
  # Everything the checks call (costopt/aws.py). Describe/Get only.
  scan_actions = [
    "ec2:DescribeVolumes", "ec2:DescribeSnapshots", "ec2:DescribeImages", "ec2:DescribeAddresses",
    "ec2:DescribeInstances", "ec2:DescribeNatGateways",
    "elasticloadbalancing:DescribeLoadBalancers", "elasticloadbalancing:DescribeTargetGroups",
    "elasticloadbalancing:DescribeTargetHealth", "elasticloadbalancing:DescribeTags",
    "cloudwatch:GetMetricStatistics", "sts:GetCallerIdentity",
  ]
}
