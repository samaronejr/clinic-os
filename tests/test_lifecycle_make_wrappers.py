from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_lifecycle_make_targets_use_owner_url_and_no_secret_arguments() -> None:
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    targets = {
        "bootstrap-clinic": "bootstrap_clinic",
        "provision-staff": "provision_staff",
        "set-clinic-timezone": "set_clinic_timezone",
        "revoke-staff-role": "revoke_staff_role",
    }

    for target, command in targets.items():
        marker = f"{target}:"
        assert marker in makefile
        recipe = makefile.split(marker, maxsplit=1)[1].split("\n\n", maxsplit=1)[0]
        assert 'APP_DATABASE_URL="$${MIGRATION_DATABASE_URL}"' in recipe
        assert f"python manage.py {command}" in recipe
        assert "--password" not in recipe
        assert "--token" not in recipe
        assert "--device" not in recipe
