# Apply in each account to be scanned: a read-only role the central scanner can assume.
# (With AWS Organizations, deploy it to all accounts as a CloudFormation StackSet instead.)

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = ">= 6.0" }
  }
}

variable "scanner_role_arn" {
  description = "The scanner Lambda's role (output scanner_role_arn of the parent module)"
  type        = string
}

variable "external_id" {
  description = "Shared secret against the confused-deputy problem; empty to skip"
  type        = string
  default     = ""
}

resource "aws_iam_role" "read" {
  name = "costopt-read"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [merge(
      { Effect = "Allow", Principal = { AWS = var.scanner_role_arn }, Action = "sts:AssumeRole" },
      var.external_id == "" ? {} : { Condition = { StringEquals = { "sts:ExternalId" = var.external_id } } },
    )]
  })
}

resource "aws_iam_role_policy" "read" {
  name = "costopt-read"
  role = aws_iam_role.read.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Resource = "*"
      Action = [
        "ec2:DescribeVolumes", "ec2:DescribeSnapshots", "ec2:DescribeImages", "ec2:DescribeAddresses",
        "ec2:DescribeInstances", "ec2:DescribeNatGateways",
        "elasticloadbalancing:DescribeLoadBalancers", "elasticloadbalancing:DescribeTargetGroups",
        "elasticloadbalancing:DescribeTargetHealth", "elasticloadbalancing:DescribeTags",
        "cloudwatch:GetMetricStatistics", "sts:GetCallerIdentity",
      ]
    }]
  })
}

output "role_arn" {
  value = aws_iam_role.read.arn
}
