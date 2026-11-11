"""PostgreSQL server settings for the databases the test suite runs against.

The xdist phase of ``ops.testing.ci_pytest`` gives every worker its own cloned
database: 8 workers in the renewal runner's coverage gate, 4 in hosted CI and
``make ci``. Each transactional test ends with Django's flush: one ``TRUNCATE``
of every table, which holds an AccessExclusive lock on each table, index and
TOAST relation until it commits. At 9b6d1715 one flush takes 2,934 shared
lock-table entries (315 tables, 1,994 indexes), the largest single-transaction
demand measured; identity 0051's reverse migration takes 865.

PostgreSQL's default ``max_locks_per_transaction`` of 64 sizes the shared lock
table at 64 x (max_connections + max_prepared_transactions) = 6,400 entries.
Eight concurrent flushes need about 23,500 and fail with "out of shared
memory". 512 sizes it at 51,200, about 2.2 times the eight-worker demand,
which leaves room for the schema to grow.

The restore rehearsal and the image TLS stack keep PostgreSQL's defaults: they
run single-process restore and serving paths, where the default lock table is
the production-like limit.
"""

from typing import Final

# Appended to the server command after the image (``postgres -c ...``).
TEST_DATABASE_SETTINGS: Final = ("-c", "max_locks_per_transaction=512")
