variable "environment" {
  description = "Deployment environment used in resource tags."
  type        = string
  default     = "development"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}$", var.environment))
    error_message = "environment must be 2-31 lowercase letters, digits, or hyphens."
  }
}

variable "db_identifier" {
  description = "RDS instance identifier."
  type        = string
  default     = "clinic-os-development"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,61}[a-z0-9]$", var.db_identifier)) && !strcontains(var.db_identifier, "--")
    error_message = "db_identifier must be 2-63 lowercase letters, digits, or hyphens, without consecutive or trailing hyphens."
  }
}

variable "db_name" {
  description = "Initial PostgreSQL database name."
  type        = string
  default     = "clinic"

  validation {
    condition     = can(regex("^[A-Za-z][A-Za-z0-9_]{0,62}$", var.db_name))
    error_message = "db_name must start with a letter and contain at most 63 letters, digits, or underscores."
  }
}

variable "master_username" {
  description = "RDS master username; the password is generated and stored by AWS Secrets Manager."
  type        = string
  default     = "clinic_owner"

  validation {
    condition     = can(regex("^[A-Za-z][A-Za-z0-9_]{0,62}$", var.master_username)) && lower(var.master_username) != "postgres"
    error_message = "master_username must be a valid non-postgres identifier of at most 63 characters."
  }
}

variable "instance_class" {
  description = "RDS instance class. Review the current sa-east-1 price before changing or applying."
  type        = string
  default     = "db.t4g.micro"

  validation {
    condition     = can(regex("^db\\.[a-z0-9]+\\.[a-z0-9]+$", var.instance_class))
    error_message = "instance_class must be an RDS class such as db.t4g.micro."
  }
}

variable "allocated_storage_gib" {
  description = "Initial encrypted GP3 storage in GiB."
  type        = number
  default     = 20

  validation {
    condition     = var.allocated_storage_gib >= 20 && var.allocated_storage_gib <= 65536 && floor(var.allocated_storage_gib) == var.allocated_storage_gib
    error_message = "allocated_storage_gib must be a whole number from 20 through 65536 GiB."
  }
}

variable "max_allocated_storage_gib" {
  description = "Storage autoscaling ceiling in GiB. Must not be lower than allocated_storage_gib."
  type        = number
  default     = 100

  validation {
    condition     = var.max_allocated_storage_gib >= 20 && var.max_allocated_storage_gib <= 65536 && floor(var.max_allocated_storage_gib) == var.max_allocated_storage_gib
    error_message = "max_allocated_storage_gib must be a whole number from 20 through 65536 GiB."
  }
}

variable "backup_retention_days" {
  description = "Automated-backup retention window; nonzero retention enables point-in-time recovery."
  type        = number
  default     = 7

  validation {
    condition     = var.backup_retention_days >= 7 && var.backup_retention_days <= 35 && floor(var.backup_retention_days) == var.backup_retention_days
    error_message = "backup_retention_days must be a whole number from 7 through 35."
  }
}

variable "multi_az" {
  description = "Enable a Multi-AZ standby. Disabled by default to avoid unapproved development cost."
  type        = bool
  default     = false
}

variable "deletion_protection" {
  description = "Protect the database from accidental deletion."
  type        = bool
  default     = true
}

variable "skip_final_snapshot" {
  description = "Skip the final snapshot on deletion. Keep false outside disposable sandboxes."
  type        = bool
  default     = false
}

variable "db_subnet_group_name" {
  description = "Approved private RDS subnet group. Null uses the account default and requires explicit review before apply."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.db_subnet_group_name == null ? true : length(trimspace(var.db_subnet_group_name)) > 0
    error_message = "db_subnet_group_name must be null or a non-empty name."
  }
}

variable "vpc_security_group_ids" {
  description = "Approved security groups for database access. Empty uses the VPC default and requires explicit review before apply."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for id in var.vpc_security_group_ids : can(regex("^sg-[0-9a-f]{8}([0-9a-f]{9})?$", id))])
    error_message = "Each vpc_security_group_ids entry must be an AWS security group ID."
  }
}

variable "tags" {
  description = "Additional non-sensitive tags."
  type        = map(string)
  default     = {}
}
