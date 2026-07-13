# Clinic OS AWS database skeleton

This directory is a validation-only Terraform root module for one encrypted
Amazon RDS for PostgreSQL 16 instance in São Paulo (`sa-east-1`). It has never
been applied by this repository. No AWS credentials, backend values, Terraform
state, or saved plans belong in version control.

## Safety posture

- The AWS provider is constrained to `~> 5.60`, and the generated lockfile pins
  the selected 5.x release for repeatable validation.
- The provider region is fixed to `sa-east-1`.
- RDS generates and rotates the master password in AWS Secrets Manager;
  plaintext database passwords are not Terraform inputs or outputs.
- Storage is encrypted GP3, public accessibility is disabled, deletion
  protection defaults on, and final snapshots default on.
- Automated-backup retention defaults to seven days and cannot be configured
  below seven. Amazon RDS uses this retention window for point-in-time recovery
  (PITR).
- Multi-AZ defaults off to avoid unapproved development spend. Enabling it is a
  production availability and cost decision, not a validation requirement.
- The default/null network inputs are placeholders only. A real deployment must
  supply an approved private DB subnet group and least-privilege security groups.

## Validation (no AWS resources)

Install a compatible Terraform CLI, then run only the no-resource/backend-disabled
validation path during foundation work:

```sh
terraform -chdir=terraform fmt -check -recursive
terraform -chdir=terraform init -backend=false
terraform -chdir=terraform validate
```

`init -backend=false` downloads the declared provider and writes only the local
`.terraform/` cache plus `.terraform.lock.hcl`; it does not configure remote
state or provision AWS resources. Commit lockfile updates after review, and
remove `.terraform/` before packaging evidence.

## Gated deployment

Do not run `terraform plan` or `terraform apply` from this foundation task.
Before any future deployment, an authorized infrastructure owner must:

1. approve an AWS account, budget, and current `sa-east-1` RDS pricing;
2. provide private subnet and security-group IDs and review inbound access;
3. create and approve an encrypted, versioned, lock-capable remote state
   backend, supplied via an untracked `.tfbackend` file;
4. review backup, restore, final-snapshot, deletion-protection, monitoring, and
   Multi-AZ requirements for the target environment;
5. run policy/security checks and obtain an independent review of the proposed
   changes before any apply authorization.

The partial `backend "s3" {}` block intentionally contains no bucket, key,
account, profile, or credentials. Those values are environment-specific and
must remain outside the repository.

## Cost warning

RDS instance hours, allocated and autoscaled storage, backup retention beyond
the included allocation, retained automated backups, final snapshots, Secrets
Manager, monitoring, data transfer, and especially Multi-AZ can all incur
ongoing charges. Defaults are development-sized but are not a cost estimate.
Check the current AWS Pricing Calculator and set a budget/alarm before an
authorized deployment.

## References

- [Terraform backend configuration](https://developer.hashicorp.com/terraform/language/backend)
- [Terraform provider requirements and lockfiles](https://developer.hashicorp.com/terraform/language/providers/requirements)
- [AWS provider `aws_db_instance`](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/db_instance)
- [Amazon RDS point-in-time recovery](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_PIT.html)
- [Amazon RDS backup retention](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_WorkingWithAutomatedBackups.BackupRetention.html)
