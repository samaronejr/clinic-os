COMPOSE ?= docker compose
DOCKER ?= docker
UV ?= uv
POSTGRES_PORT ?= 5432
POSTGRES_HOST_IP ?= 127.0.0.1
POSTGRES_HOST_PORT ?= 5432
POSTGRES_IMAGE ?= postgres:16
POSTGRES_DATA_VOLUME ?= clinic_postgres_data
POSTGRES_DB ?= clinic
POSTGRES_USER ?= postgres
POSTGRES_PASSWORD ?= postgres
CLINIC_OWNER_PASSWORD ?= clinic_owner_password
CLINIC_APP_PASSWORD ?= clinic_app_password
CLINIC_SUPER_PASSWORD ?= clinic_super_password
APP_DATABASE_URL ?= postgresql://clinic_app:clinic_app_password@localhost:$(POSTGRES_HOST_PORT)/clinic
MIGRATION_DATABASE_URL ?= postgresql://clinic_owner:clinic_owner_password@localhost:$(POSTGRES_HOST_PORT)/clinic
TEST_SUPERUSER_DATABASE_URL ?= postgresql://clinic_super:clinic_super_password@localhost:$(POSTGRES_HOST_PORT)/clinic

ifeq ($(origin POSTGRES_CONTAINER), undefined)
POSTGRES_CONTAINER := $(shell $(COMPOSE) ps -q db 2>/dev/null)
else
override POSTGRES_CONTAINER := $(value POSTGRES_CONTAINER)
endif

override POSTGRES_PORT := $(value POSTGRES_PORT)
override POSTGRES_HOST_IP := $(value POSTGRES_HOST_IP)
override POSTGRES_HOST_PORT := $(value POSTGRES_HOST_PORT)
override POSTGRES_IMAGE := $(value POSTGRES_IMAGE)
override POSTGRES_DATA_VOLUME := $(value POSTGRES_DATA_VOLUME)
override POSTGRES_DB := $(value POSTGRES_DB)
override POSTGRES_USER := $(value POSTGRES_USER)
override POSTGRES_PASSWORD := $(value POSTGRES_PASSWORD)
override CLINIC_OWNER_PASSWORD := $(value CLINIC_OWNER_PASSWORD)
override CLINIC_APP_PASSWORD := $(value CLINIC_APP_PASSWORD)
override CLINIC_SUPER_PASSWORD := $(value CLINIC_SUPER_PASSWORD)
override APP_DATABASE_URL := $(value APP_DATABASE_URL)
override MIGRATION_DATABASE_URL := $(value MIGRATION_DATABASE_URL)
override TEST_SUPERUSER_DATABASE_URL := $(value TEST_SUPERUSER_DATABASE_URL)

ifeq ($(origin TEST_DATABASE_NAME), undefined)
TEST_DATABASE_NAME := test_$(POSTGRES_DB)
else
override TEST_DATABASE_NAME := $(value TEST_DATABASE_NAME)
endif

export POSTGRES_CONTAINER POSTGRES_PORT POSTGRES_DB TEST_DATABASE_NAME
export POSTGRES_HOST_IP POSTGRES_HOST_PORT POSTGRES_IMAGE POSTGRES_DATA_VOLUME
export POSTGRES_USER POSTGRES_PASSWORD
export CLINIC_OWNER_PASSWORD CLINIC_APP_PASSWORD CLINIC_SUPER_PASSWORD
export APP_DATABASE_URL MIGRATION_DATABASE_URL TEST_SUPERUSER_DATABASE_URL
COVERAGE_TARGETS := $(shell grep -v '^\#' ops/testing/coverage-targets.txt | tr '\n' ' ')

.PHONY: bootstrap-clinic ci ci-browser-contract ci-image-contracts current-source-snapshot db-bootstrap db-inputs db-posture isolated-db-down isolated-db-status isolated-db-up migrate provision-staff restore-rehearsal revoke-staff-role set-clinic-timezone

isolated-db-up:
	@./ops/testing/isolated_db.sh up

isolated-db-status:
	@./ops/testing/isolated_db.sh status

isolated-db-down:
	@./ops/testing/isolated_db.sh down

db-inputs:
	@set -- \
		POSTGRES_DB "$${POSTGRES_DB}" \
		TEST_DATABASE_NAME "$${TEST_DATABASE_NAME}" \
		POSTGRES_USER "$${POSTGRES_USER}"; \
	while [ "$${#}" -gt 0 ]; do \
		input_name="$${1}"; \
		input_value="$${2}"; \
		shift 2; \
		case "$${input_value}" in \
			""|[!A-Za-z_]*|*[!A-Za-z0-9_]*) \
				printf 'invalid %s\n' "$${input_name}" >&2; exit 2;; \
		esac; \
		[ "$${#input_value}" -le 63 ] || { \
			printf 'invalid %s\n' "$${input_name}" >&2; exit 2; \
		}; \
	done; \
	set -- \
		POSTGRES_PASSWORD "$${POSTGRES_PASSWORD}" \
		CLINIC_OWNER_PASSWORD "$${CLINIC_OWNER_PASSWORD}" \
		CLINIC_APP_PASSWORD "$${CLINIC_APP_PASSWORD}" \
		CLINIC_SUPER_PASSWORD "$${CLINIC_SUPER_PASSWORD}"; \
	while [ "$${#}" -gt 0 ]; do \
		input_name="$${1}"; \
		input_value="$${2}"; \
		shift 2; \
		[ -n "$${input_value}" ] || { \
			printf 'invalid %s\n' "$${input_name}" >&2; exit 2; \
		}; \
	done; \
	case "$${POSTGRES_CONTAINER}" in \
		""|[!A-Za-z0-9]*|*[!A-Za-z0-9_.-]*) \
			printf '%s\n' 'invalid POSTGRES_CONTAINER' >&2; exit 2;; \
	esac; \
	[ "$${#POSTGRES_CONTAINER}" -le 128 ] || { \
		printf '%s\n' 'invalid POSTGRES_CONTAINER' >&2; exit 2; \
	}; \
	case "$${POSTGRES_PORT}" in \
		""|*[!0-9]*) printf '%s\n' 'invalid POSTGRES_PORT' >&2; exit 2;; \
	esac; \
	[ "$${#POSTGRES_PORT}" -le 5 ] \
		&& [ "$${POSTGRES_PORT}" -ge 1 ] \
		&& [ "$${POSTGRES_PORT}" -le 65535 ] || { \
			printf '%s\n' 'invalid POSTGRES_PORT' >&2; exit 2; \
		}

db-bootstrap: db-inputs
	@PGPASSWORD="$${POSTGRES_PASSWORD}" $(DOCKER) exec -i -e PGPASSWORD \
		-e CLINIC_OWNER_PASSWORD -e CLINIC_APP_PASSWORD \
		-e CLINIC_SUPER_PASSWORD \
		"$${POSTGRES_CONTAINER}" \
		psql -v ON_ERROR_STOP=1 -h localhost -p "$${POSTGRES_PORT}" \
		-U "$${POSTGRES_USER}" -d "$${POSTGRES_DB}" \
		-v database_name="$${POSTGRES_DB}" -v app_schema="clinic_app" \
		-f - < ops/db/bootstrap.sql
	@PGPASSWORD="$${POSTGRES_PASSWORD}" $(DOCKER) exec -i \
		-e PGPASSWORD -e POSTGRES_PORT -e POSTGRES_USER -e POSTGRES_DB \
		-e TEST_DATABASE_NAME "$${POSTGRES_CONTAINER}" \
		sh -ceu '\
			case "$$TEST_DATABASE_NAME" in \
				""|*[!A-Za-z0-9_]*) printf "%s\n" "invalid TEST_DATABASE_NAME" >&2; exit 2;; \
			esac; \
			if ! psql -h localhost -p "$$POSTGRES_PORT" -U "$$POSTGRES_USER" \
				-d "$$POSTGRES_DB" -Atqc \
				"SELECT 1 FROM pg_database WHERE datname = '\''$$TEST_DATABASE_NAME'\''" \
				| grep -qx 1; then \
				createdb -h localhost -p "$$POSTGRES_PORT" -U "$$POSTGRES_USER" \
					--owner=clinic_owner "$$TEST_DATABASE_NAME"; \
			fi'
	@PGPASSWORD="$${POSTGRES_PASSWORD}" $(DOCKER) exec -i -e PGPASSWORD \
		-e CLINIC_OWNER_PASSWORD -e CLINIC_APP_PASSWORD \
		-e CLINIC_SUPER_PASSWORD \
		"$${POSTGRES_CONTAINER}" \
		psql -v ON_ERROR_STOP=1 -h localhost -p "$${POSTGRES_PORT}" \
		-U "$${POSTGRES_USER}" -d "$${TEST_DATABASE_NAME}" \
		-v database_name="$${TEST_DATABASE_NAME}" -v app_schema="clinic_app" \
		-f - < ops/db/bootstrap.sql

db-posture: db-inputs
	@$(UV) run python ops/db/posture.py

migrate:
	@APP_DATABASE_URL="$${MIGRATION_DATABASE_URL}" $(UV) run python manage.py migrate

bootstrap-clinic:
	@APP_DATABASE_URL="$${MIGRATION_DATABASE_URL}" $(UV) run --frozen --no-sync --no-env-file python manage.py bootstrap_clinic \
		--organization-id "$${ORGANIZATION_ID}" \
		--organization-name "$${ORGANIZATION_NAME}" \
		--cnpj "$${CLINIC_CNPJ}" \
		--clinic-id "$${CLINIC_ID}" \
		--clinic-name "$${CLINIC_NAME}" \
		--crm-uf "$${CLINIC_CRM_UF}" \
		--timezone "$${CLINIC_TIMEZONE}" \
		--owner-user-id "$${OWNER_USER_ID}" \
		--owner-username "$${OWNER_USERNAME}" \
		--owner-email "$${OWNER_EMAIL}"

provision-staff:
	@APP_DATABASE_URL="$${MIGRATION_DATABASE_URL}" $(UV) run --frozen --no-sync --no-env-file python manage.py provision_staff \
		--operator-id "$${OPERATOR_ID}" \
		--organization-id "$${ORGANIZATION_ID}" \
		--clinic-id "$${CLINIC_ID}" \
		--staff-user-id "$${STAFF_USER_ID}" \
		--username "$${STAFF_USERNAME}" \
		--email "$${STAFF_EMAIL}" \
		--role "$${STAFF_ROLE}"

set-clinic-timezone:
	@APP_DATABASE_URL="$${MIGRATION_DATABASE_URL}" $(UV) run --frozen --no-sync --no-env-file python manage.py set_clinic_timezone \
		--operator-id "$${OPERATOR_ID}" \
		--organization-id "$${ORGANIZATION_ID}" \
		--clinic-id "$${CLINIC_ID}" \
		--timezone "$${CLINIC_TIMEZONE}"

revoke-staff-role:
	@APP_DATABASE_URL="$${MIGRATION_DATABASE_URL}" $(UV) run --frozen --no-sync --no-env-file python manage.py revoke_staff_role \
		--operator-id "$${OPERATOR_ID}" \
		--organization-id "$${ORGANIZATION_ID}" \
		--clinic-id "$${CLINIC_ID}" \
		--target-user-id "$${TARGET_USER_ID}" \
		--role "$${STAFF_ROLE}"

restore-rehearsal:
	@$(UV) run --frozen --no-sync --no-env-file python -m ops.testing.restore_rehearsal \
		--source-container "$${RESTORE_SOURCE_CONTAINER:?}" \
		--target-container "$${RESTORE_TARGET_CONTAINER:?}" \
		--source-database "$${RESTORE_SOURCE_DATABASE:?}" \
		--target-database "$${RESTORE_TARGET_DATABASE:?}" \
		--source-claim "$${RESTORE_SOURCE_CLAIM:?}" \
		--target-claim "$${RESTORE_TARGET_CLAIM:?}" \
		--credentials-fd "$${RESTORE_CREDENTIALS_FD:?}" \
		--work-dir "$${RESTORE_WORK_DIR:?}" \
		--evidence "$${RESTORE_EVIDENCE_PATH:?}" \
		--probe "$${RESTORE_PROBE_PATH:?}" \
		--object-store "$${RESTORE_OBJECT_STORE:?}" \
		--secret-dir "$${RESTORE_SECRET_DIR:?}" \
		--attachment-root "$${RESTORE_ATTACHMENT_ROOT:?}"

ci-browser-contract:
	@actual="$$(sha256sum ops/testing/ci-required-browser-suites.txt | cut -d' ' -f1)"; \
		test "$${actual}" = 4c6e14cdf93690d1c38bba4b5a7c36cf657bf795bc3a003a17ad5bc3e67067c5
	@set --; previous=''; \
		while IFS= read -r suite; do \
			case "$${suite}" in availability|patient|scheduling) ;; *) exit 2 ;; esac; \
			test -z "$${previous}" || test "$${previous}" \< "$${suite}" || exit 2; \
			set -- "$${@}" --require-suite "$${suite}"; previous="$${suite}"; \
		done < ops/testing/ci-required-browser-suites.txt; \
		test "$${#}" -eq 6; \
		ops/testing/browser_runner.sh probe "$${@}"

ci-image-contracts:
	$(UV) run --frozen --no-sync --no-env-file pytest -q tests/isolation/test_container_contract.py
	@sha="$$(git rev-parse HEAD)"; \
		ops/testing/image_smoke.sh build --sha "$${sha}"
	@ops/testing/tls_stack.sh smoke

current-source-snapshot:
	@set --; \
		if [ -n "$${CLINIC_CURRENT_SOURCE_ALLOWLIST:-}" ]; then \
			set -- "$${@}" --allowlist "$${CLINIC_CURRENT_SOURCE_ALLOWLIST}"; \
		fi; \
		$(UV) run --frozen --no-sync --no-env-file python -m ops.testing.current_source_snapshot snapshot \
			--repository "$${CLINIC_CURRENT_SOURCE_REPOSITORY:?}" \
			--run-root "$${CLINIC_CURRENT_SOURCE_RUN_ROOT:?}" \
			--report "$${CLINIC_CURRENT_SOURCE_REPORT:?}" \
			"$${@}"

ci: override export DJANGO_SETTINGS_MODULE := config.settings.test
ci:
	$(UV) sync --locked --all-groups
	@$(MAKE) db-bootstrap
	@$(MAKE) migrate
	@$(MAKE) db-posture
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy .
	@APP_DATABASE_URL="$${APP_DATABASE_URL}" \
		MIGRATION_DATABASE_URL="$${MIGRATION_DATABASE_URL}" \
		TEST_SUPERUSER_DATABASE_URL="$${TEST_SUPERUSER_DATABASE_URL}" \
		$(UV) run pytest --reuse-db $(COVERAGE_TARGETS) --cov-report=term-missing --cov-fail-under=90 tests
	$(UV) run pip-audit --local
	@$(MAKE) ci-image-contracts
	@$(MAKE) ci-browser-contract
