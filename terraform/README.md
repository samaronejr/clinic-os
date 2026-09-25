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

## Database role plan (ADR-019)

Application migrations/bootstrap, not this Terraform skeleton, own PostgreSQL
roles. The added `clinic_agent` login is NOSUPERUSER, NOBYPASSRLS, NOINHERIT,
NOCREATEDB, NOCREATEROLE and NOREPLICATION, with no database/schema/table ownership
and no membership in clinic_app/clinic_owner/clinic_resolver. It receives CONNECT,
schema USAGE and only reviewed per-app `AGENT_GRANTS`; never audit/key-table
SELECT or default table/sequence privileges. Bootstrap consumes the optional
`CLINIC_AGENT_PASSWORD`; absence disables password login, not a shared fallback.
No password belongs in Terraform inputs, outputs, state or this document.

A principal maps uniquely to its authenticated database login (`session_user`).
For multiple independently scoped workers, an authorized owner must provision
separate `clinic_agent_*` logins with only inherited clinic_agent privileges and
register each with its own exact-clinic grants. A shared credential must never
select among multiple principal identities. `AGENT_DATABASE_URL` supplies a
separate optional Django alias on the same database endpoint as the application;
production enforces verified TLS and the existing DSN contract. Secret delivery,
rotation and any RDS role adaptation require the approved deployment runbook;
this change provisions no cloud resource and authorizes no plan/apply.

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
