output "function_name" {
  value = aws_lambda_function.fn.function_name
}

output "report_bucket" {
  value = aws_s3_bucket.reports.id
}

output "topic_arn" {
  value = aws_sns_topic.findings.arn
}

output "scanner_role_arn" {
  description = "Trust this from member-role/ in each account to be scanned"
  value       = aws_iam_role.fn.arn
}
