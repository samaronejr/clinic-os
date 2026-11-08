"""Equality-preserving query doubles and observable service refusal shapes."""

from __future__ import annotations

from dataclasses import dataclass

type QueryTrace = list[tuple[str, tuple[str, ...]]]


@dataclass(frozen=True)
class VisibleRows[Row]:
    rows: tuple[Row, ...]
    trace: QueryTrace
    label: str

    def filter(self, **conditions: object) -> VisibleRows[Row]:
        self.trace.append((f"{self.label}.filter", tuple(sorted(conditions))))

        def matches(row: Row) -> bool:
            for path, expected in conditions.items():
                value: object = row
                for field in path.split("__"):
                    value = getattr(value, field)
                if value != expected:
                    return False
            return True

        return VisibleRows(
            tuple(row for row in self.rows if matches(row)), self.trace, self.label
        )

    def first(self) -> Row | None:
        return next(iter(self.rows), None)

    def exists(self) -> bool:
        self.trace.append((f"{self.label}.exists", ()))
        return bool(self.rows)


def refusal_shape(
    error: Exception,
) -> tuple[type[Exception], tuple[object, ...], object, type[object], bool]:
    return (
        type(error),
        error.args,
        getattr(error, "code", None),
        type(error.__cause__),
        error.__suppress_context__,
    )
