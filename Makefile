COMPOSE ?= docker compose
APP_DATABASE_URL ?= postgresql://clinic_app:clinic_app_password@localhost:5432/clinic
MIGRATION_DATABASE_URL ?= postgresql://clinic_owner:clinic_owner_password@localhost:5432/clinic

.PHONY: db-bootstrap db-posture migrate

db-bootstrap:
	$(COMPOSE) exec -T db sh -ceu 'psql -v ON_ERROR_STOP=1 -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" -v database_name="$$POSTGRES_DB" -v app_schema="clinic_app" -v clinic_owner_password="$$CLINIC_OWNER_PASSWORD" -v clinic_app_password="$$CLINIC_APP_PASSWORD" -v clinic_super_password="$$CLINIC_SUPER_PASSWORD" -f /opt/clinic/bootstrap.sql'

db-posture:
	@$(COMPOSE) exec -T -e APP_DATABASE_URL="$(APP_DATABASE_URL)" db sh -ceu 'set -- $$(psql "$$APP_DATABASE_URL" -At -F " " -c "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"); [ "$$1" = clinic_app ] || { printf "%s\\n" "APP_DATABASE_URL must authenticate as clinic_app, got $$1" >&2; exit 1; }; [ "$$2" = f ] && [ "$$3" = f ] || { printf "%s\\n" "APP_DATABASE_URL must not authenticate as a superuser or BYPASSRLS role" >&2; exit 1; }'

migrate:
	APP_DATABASE_URL="$(MIGRATION_DATABASE_URL)" uv run python manage.py migrate
