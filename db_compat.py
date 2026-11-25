"""Compatibility imports for the database adapter's former module path."""

from kono_trade.repositories.db_compat import (
    HybridRow,
    PostgresConnection,
    postgres_row_factory as _postgres_row_factory,
    postgres_schema,
    postgres_sql,
)


def postgres_row_factory(cursor):
    if cursor.description is None:
        return lambda values: None
    return _postgres_row_factory(cursor)


__all__ = [
    "HybridRow",
    "PostgresConnection",
    "postgres_row_factory",
    "postgres_schema",
    "postgres_sql",
]
