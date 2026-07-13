COMPOSE ?= docker compose
DOCKER ?= docker
UV ?= uv
POSTGRES_CONTAINER ?= $(shell $(COMPOSE) ps -q db 2>/dev/null)
POSTGRES_PORT ?= 5432
POSTGRES_DB ?= clinic
TEST_DATABASE_NAME ?= test_$(POSTGRES_DB)
POSTGRES_USER ?= postgres
POSTGRES_PASSWORD ?= postgres
CLINIC_OWNER_PASSWORD ?= clinic_owner_password
CLINIC_APP_PASSWORD ?= clinic_app_password
CLINIC_SUPER_PASSWORD ?= clinic_super_password
APP_DATABASE_URL ?= postgresql://clinic_app:clinic_app_password@localhost:5432/clinic
MIGRATION_DATABASE_URL ?= postgresql://clinic_owner:clinic_owner_password@localhost:5432/clinic
TEST_SUPERUSER_DATABASE_URL ?= postgresql://clinic_super:clinic_super_password@localhost:5432/clinic
COVERAGE_TARGETS = \
	--cov=apps.audit \
	--cov=apps.identity \
	--cov=apps.tenancy

.PHONY: ci db-bootstrap db-posture migrate

db-bootstrap:
	@test -n "$(POSTGRES_CONTAINER)" || { printf '%s\n' "PostgreSQL container not found; start Compose or set POSTGRES_CONTAINER" >&2; exit 2; }
	@$(DOCKER) exec -i -e PGPASSWORD="$(POSTGRES_PASSWORD)" "$(POSTGRES_CONTAINER)" \
		psql -v ON_ERROR_STOP=1 -h localhost -p "$(POSTGRES_PORT)" \
		-U "$(POSTGRES_USER)" -d "$(POSTGRES_DB)" \
		-v database_name="$(POSTGRES_DB)" -v app_schema="clinic_app" \
		-v clinic_owner_password="$(CLINIC_OWNER_PASSWORD)" \
		-v clinic_app_password="$(CLINIC_APP_PASSWORD)" \
		-v clinic_super_password="$(CLINIC_SUPER_PASSWORD)" \
		-f - < ops/db/bootstrap.sql
	@$(DOCKER) exec -i -e PGPASSWORD="$(POSTGRES_PASSWORD)" \
		-e TEST_DATABASE_NAME="$(TEST_DATABASE_NAME)" "$(POSTGRES_CONTAINER)" \
		sh -ceu '\
			case "$$TEST_DATABASE_NAME" in \
				""|*[!A-Za-z0-9_]*) printf "%s\n" "invalid TEST_DATABASE_NAME" >&2; exit 2;; \
			esac; \
			if ! psql -h localhost -p "$(POSTGRES_PORT)" -U "$(POSTGRES_USER)" \
				-d "$(POSTGRES_DB)" -Atqc \
				"SELECT 1 FROM pg_database WHERE datname = '\''$$TEST_DATABASE_NAME'\''" \
				| grep -qx 1; then \
				createdb -h localhost -p "$(POSTGRES_PORT)" -U "$(POSTGRES_USER)" \
					--owner=clinic_owner "$$TEST_DATABASE_NAME"; \
			fi'
	@$(DOCKER) exec -i -e PGPASSWORD="$(POSTGRES_PASSWORD)" "$(POSTGRES_CONTAINER)" \
		psql -v ON_ERROR_STOP=1 -h localhost -p "$(POSTGRES_PORT)" \
		-U "$(POSTGRES_USER)" -d "$(TEST_DATABASE_NAME)" \
		-v database_name="$(TEST_DATABASE_NAME)" -v app_schema="clinic_app" \
		-v clinic_owner_password="$(CLINIC_OWNER_PASSWORD)" \
		-v clinic_app_password="$(CLINIC_APP_PASSWORD)" \
		-v clinic_super_password="$(CLINIC_SUPER_PASSWORD)" \
		-f - < ops/db/bootstrap.sql

db-posture:
	@test -n "$(POSTGRES_CONTAINER)" || { printf '%s\n' "PostgreSQL container not found; start Compose or set POSTGRES_CONTAINER" >&2; exit 2; }
	@$(DOCKER) exec -i -e APP_DATABASE_URL="$(APP_DATABASE_URL)" \
		-e TEST_DATABASE_NAME="$(TEST_DATABASE_NAME)" "$(POSTGRES_CONTAINER)" \
		sh -ceu '\
			case "$$TEST_DATABASE_NAME" in \
				""|*[!A-Za-z0-9_]*) printf "%s\n" "invalid TEST_DATABASE_NAME" >&2; exit 2;; \
			esac; \
			set -- $$(psql "$$APP_DATABASE_URL" -At -F " " -c \
				"SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"); \
			[ "$$1" = clinic_app ] || { \
				printf "%s\n" "APP_DATABASE_URL must authenticate as clinic_app, got $$1" >&2; \
				exit 1; \
			}; \
			[ "$$2" = f ] && [ "$$3" = f ] || { \
				printf "%s\n" "APP_DATABASE_URL must not authenticate as a superuser or BYPASSRLS role" >&2; \
				exit 1; \
			}; \
			set -- $$(psql "$$APP_DATABASE_URL" -At -F " " -c \
				"SELECT rolsuper, rolbypassrls, rolcreatedb FROM pg_roles WHERE rolname = '\''clinic_owner'\''"); \
			[ "$$1" = f ] && [ "$$2" = f ] && [ "$$3" = f ] || { \
				printf "%s\n" "clinic_owner must be NOSUPERUSER, NOBYPASSRLS, and NOCREATEDB" >&2; \
				exit 1; \
			}; \
			test_owner=$$(psql "$$APP_DATABASE_URL" -Atc \
				"SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = '\''$$TEST_DATABASE_NAME'\''"); \
			[ "$$test_owner" = clinic_owner ] || { \
				printf "%s\n" "$$TEST_DATABASE_NAME must be owned by clinic_owner" >&2; \
				exit 1; \
			}; \
			printf "%s\n" \
				"database posture: clinic_app runtime; clinic_owner NOCREATEDB; $$TEST_DATABASE_NAME precreated"'

migrate:
	APP_DATABASE_URL="$(MIGRATION_DATABASE_URL)" $(UV) run python manage.py migrate

ci:
	$(UV) sync --locked
	$(MAKE) db-bootstrap POSTGRES_CONTAINER="$(POSTGRES_CONTAINER)"
	$(MAKE) migrate
	$(MAKE) db-posture POSTGRES_CONTAINER="$(POSTGRES_CONTAINER)"
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy .
	APP_DATABASE_URL="$(APP_DATABASE_URL)" MIGRATION_DATABASE_URL="$(MIGRATION_DATABASE_URL)" TEST_SUPERUSER_DATABASE_URL="$(TEST_SUPERUSER_DATABASE_URL)" \
		$(UV) run pytest --reuse-db $(COVERAGE_TARGETS) --cov-report=term-missing --cov-fail-under=90 tests
	$(UV) run pip-audit --local
