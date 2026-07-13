output "aws_region" {
  description = "AWS region fixed for the foundation deployment."
  value       = "sa-east-1"
}

output "db_instance_arn" {
  description = "ARN of the RDS instance."
  value       = aws_db_instance.clinic.arn
}

output "db_address" {
  description = "DNS address of the RDS instance."
  value       = aws_db_instance.clinic.address
}

output "db_port" {
  description = "PostgreSQL port."
  value       = aws_db_instance.clinic.port
}

output "master_user_secret_arn" {
  description = "ARN of the AWS-managed master-password secret. Treat as sensitive metadata."
  value       = aws_db_instance.clinic.master_user_secret[0].secret_arn
  sensitive   = true
}

output "pitr_retention_days" {
  description = "Configured automated-backup/PITR retention window."
  value       = aws_db_instance.clinic.backup_retention_period
}
