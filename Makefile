COMPOSE ?= docker compose
DOCKER ?= docker
UV ?= uv
POSTGRES_PORT ?= 5432
POSTGRES_DB ?= clinic
POSTGRES_USER ?= postgres
POSTGRES_PASSWORD ?= postgres
CLINIC_OWNER_PASSWORD ?= clinic_owner_password
CLINIC_APP_PASSWORD ?= clinic_app_password
CLINIC_SUPER_PASSWORD ?= clinic_super_password
APP_DATABASE_URL ?= postgresql://clinic_app:clinic_app_password@localhost:5432/clinic
MIGRATION_DATABASE_URL ?= postgresql://clinic_owner:clinic_owner_password@localhost:5432/clinic
TEST_SUPERUSER_DATABASE_URL ?= postgresql://clinic_super:clinic_super_password@localhost:5432/clinic

ifeq ($(origin POSTGRES_CONTAINER), undefined)
POSTGRES_CONTAINER := $(shell $(COMPOSE) ps -q db 2>/dev/null)
else
override POSTGRES_CONTAINER := $(value POSTGRES_CONTAINER)
endif

override POSTGRES_PORT := $(value POSTGRES_PORT)
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
export POSTGRES_USER POSTGRES_PASSWORD
export CLINIC_OWNER_PASSWORD CLINIC_APP_PASSWORD CLINIC_SUPER_PASSWORD
export APP_DATABASE_URL MIGRATION_DATABASE_URL TEST_SUPERUSER_DATABASE_URL
COVERAGE_TARGETS = \
	--cov=apps.audit \
	--cov=apps.identity \
	--cov=apps.tenancy

.PHONY: ci db-bootstrap db-inputs db-posture migrate

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

ci:
	$(UV) sync --locked
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
