"""Load the migration-only bootstrap block for disposable database factories."""

from pathlib import Path


def migration_progress_sql() -> str:
    """Use the same owner and privileges as the administrator bootstrap."""
    source = Path(__file__).with_name("bootstrap.sql").read_text(encoding="utf-8")
    return (
        source.split("-- BEGIN migration progress bootstrap", 1)[1]
        .split("\n", 1)[1]
        .split("-- END migration progress bootstrap", 1)[0]
    )
