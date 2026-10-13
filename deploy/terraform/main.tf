# Weekly read-only scan as a Lambda: EventBridge schedule -> Lambda -> report in S3 + summary on SNS.
# The function can describe resources and read metrics; it can't change anything.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws     = { source = "hashicorp/aws", version = ">= 6.0" }
    archive = { source = "hashicorp/archive", version = ">= 2.4" }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}

locals {
  account = data.aws_caller_identity.current.account_id
  tags    = merge(var.tags, { app = var.name })
}

# --- Code ---------------------------------------------------------------------------------
data "archive_file" "code" {
  type        = "zip"
  output_path = "${path.module}/build/costopt.zip"
  dynamic "source" {
    for_each = fileset("${path.module}/../../costopt", "*.py")
    content {
      content  = file("${path.module}/../../costopt/${source.value}")
      filename = "costopt/${source.value}"
    }
  }
}

# --- Encryption key for the bucket, topic, queue and logs -----------------------------------
resource "aws_kms_key" "this" {
  description         = "${var.name}: reports, notifications, logs"
  enable_key_rotation = true
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "Account", Effect = "Allow", Principal = { AWS = "arn:${data.aws_partition.current.partition}:iam::${local.account}:root" },
      Action = "kms:*", Resource = "*" },
      { Sid    = "Logs", Effect = "Allow", Principal = { Service = "logs.${data.aws_region.current.region}.amazonaws.com" },
        Action = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"], Resource = "*",
      Condition = { ArnLike = { "kms:EncryptionContext:aws:logs:arn" = "arn:${data.aws_partition.current.partition}:logs:${data.aws_region.current.region}:${local.account}:*" } } },
    ]
  })
  tags = local.tags
}

# --- Report bucket ------------------------------------------------------------------------
resource "aws_s3_bucket" "reports" {
  #checkov:skip=CKV_AWS_18:Reports are regenerated weekly; CloudTrail data events cover access auditing if needed
  #checkov:skip=CKV_AWS_144:Regenerable reports don't justify a second-region copy
  #checkov:skip=CKV2_AWS_62:Nothing consumes object events; the summary is pushed to SNS by the function
  bucket_prefix = "${var.name}-reports-"
  tags          = local.tags
}

resource "aws_s3_bucket_public_access_block" "reports" {
  bucket                  = aws_s3_bucket.reports.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "reports" {
  bucket = aws_s3_bucket.reports.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_versioning" "reports" {
  bucket = aws_s3_bucket.reports.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "reports" {
  bucket = aws_s3_bucket.reports.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.this.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "reports" {
  bucket = aws_s3_bucket.reports.id
  rule {
    id     = "expire-old-reports"
    status = "Enabled"
    filter {}
    expiration { days = var.report_retention_days }
    noncurrent_version_expiration { noncurrent_days = 30 }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}

# --- Notifications ------------------------------------------------------------------------
resource "aws_sns_topic" "findings" {
  name              = "${var.name}-findings"
  kms_master_key_id = aws_kms_key.this.id
  tags              = local.tags
}

resource "aws_sns_topic_subscription" "email" {
  for_each  = toset(var.notification_emails)
  topic_arn = aws_sns_topic.findings.arn
  protocol  = "email"
  endpoint  = each.value
}

resource "aws_sqs_queue" "dlq" {
  name                      = "${var.name}-dlq"
  kms_master_key_id         = aws_kms_key.this.id
  message_retention_seconds = 1209600
  tags                      = local.tags
}

# --- Function -----------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "fn" {
  name              = "/aws/lambda/${var.name}"
  retention_in_days = 365
  kms_key_id        = aws_kms_key.this.arn
  tags              = local.tags
}

resource "aws_iam_role" "fn" {
  name = var.name
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy" "fn" {
  name = "scan-and-report"
  role = aws_iam_role.fn.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      { Sid = "ReadOnlyScan", Effect = "Allow", Resource = "*", Action = local.scan_actions },
      { Sid = "WriteReports", Effect = "Allow", Action = "s3:PutObject", Resource = "${aws_s3_bucket.reports.arn}/reports/*" },
      { Sid = "Notify", Effect = "Allow", Action = "sns:Publish", Resource = aws_sns_topic.findings.arn },
      { Sid = "DeadLetter", Effect = "Allow", Action = "sqs:SendMessage", Resource = aws_sqs_queue.dlq.arn },
      { Sid = "Encrypt", Effect = "Allow", Action = ["kms:GenerateDataKey", "kms:Decrypt"], Resource = aws_kms_key.this.arn },
      { Sid = "Logs", Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.fn.arn}:*" },
      { Sid = "Tracing", Effect = "Allow", Action = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"], Resource = "*" },
      ], length(var.member_role_arns) == 0 ? [] : [
      { Sid = "AssumeMemberRoles", Effect = "Allow", Action = "sts:AssumeRole", Resource = var.member_role_arns },
    ])
  })
}

resource "aws_lambda_function" "fn" {
  #checkov:skip=CKV_AWS_117:Calls only AWS public APIs; a VPC would add NAT/endpoints cost for nothing
  #checkov:skip=CKV_AWS_272:Code signing is out of scope for this lab; the zip is built from the reviewed repo
  function_name                  = var.name
  role                           = aws_iam_role.fn.arn
  runtime                        = "python3.12"
  handler                        = "costopt.lambda_handler.handler"
  filename                       = data.archive_file.code.output_path
  source_code_hash               = data.archive_file.code.output_base64sha256
  timeout                        = 900
  memory_size                    = 512
  reserved_concurrent_executions = 1
  kms_key_arn                    = aws_kms_key.this.arn
  environment {
    variables = {
      REGIONS          = join(",", var.regions)
      MEMBER_ROLE_ARNS = join(",", var.member_role_arns)
      REPORT_BUCKET    = aws_s3_bucket.reports.id
      TOPIC_ARN        = aws_sns_topic.findings.arn
    }
  }
  dead_letter_config { target_arn = aws_sqs_queue.dlq.arn }
  tracing_config { mode = "Active" }
  depends_on = [aws_cloudwatch_log_group.fn]
  tags       = local.tags
}

# --- Schedule -----------------------------------------------------------------------------
resource "aws_cloudwatch_event_rule" "weekly" {
  name                = "${var.name}-weekly"
  schedule_expression = var.schedule
  tags                = local.tags
}

resource "aws_cloudwatch_event_target" "fn" {
  rule = aws_cloudwatch_event_rule.weekly.name
  arn  = aws_lambda_function.fn.arn
}

resource "aws_lambda_permission" "events" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.fn.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.weekly.arn
}
