resource "aws_db_instance" "clinic" {
  identifier = var.db_identifier

  engine                   = "postgres"
  engine_version           = "16"
  engine_lifecycle_support = "open-source-rds-extended-support-disabled"
  instance_class           = var.instance_class
  db_name                  = var.db_name
  port                     = 5432

  username                    = var.master_username
  manage_master_user_password = true

  allocated_storage     = var.allocated_storage_gib
  max_allocated_storage = var.max_allocated_storage_gib
  storage_type          = "gp3"
  storage_encrypted     = true

  backup_retention_period  = var.backup_retention_days
  backup_window            = "03:00-04:00"
  copy_tags_to_snapshot    = true
  delete_automated_backups = false

  maintenance_window         = "sun:04:30-sun:05:30"
  auto_minor_version_upgrade = true
  apply_immediately          = false
  multi_az                   = var.multi_az

  publicly_accessible    = false
  db_subnet_group_name   = var.db_subnet_group_name
  vpc_security_group_ids = var.vpc_security_group_ids

  deletion_protection       = var.deletion_protection
  skip_final_snapshot       = var.skip_final_snapshot
  final_snapshot_identifier = var.skip_final_snapshot ? null : "${var.db_identifier}-final"

  lifecycle {
    precondition {
      condition     = var.max_allocated_storage_gib >= var.allocated_storage_gib
      error_message = "max_allocated_storage_gib must be greater than or equal to allocated_storage_gib."
    }
  }
}
