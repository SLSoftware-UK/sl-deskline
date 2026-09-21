"""
SQLite connection tuning for the single-container, single-volume default.

The default production shape for this project is one gunicorn process
running 2 worker processes (backend/Dockerfile) against one SQLite file
on the volume-mounted DATA_DIR (support_core/settings.py). SQLite's
default rollback-journal mode takes an exclusive lock on the whole
database file for the duration of a write, so two workers writing at
once — or a writer racing a reader — surfaces to the application as an
intermittent "database is locked" OperationalError. The pragmas below
are SQLite's own documented fix for exactly this shape, applied to every
new connection via Django's `connection_created` signal (wired up in
SupportCoreConfig.ready(), support_core/apps.py):

- `journal_mode=WAL`: write-ahead logging lets readers keep reading a
  consistent snapshot from the main database file while the single
  writer appends to a separate WAL file, instead of the writer taking
  an exclusive lock that blocks every reader.
- `busy_timeout=5000`: WAL still allows only one writer at a time, so
  two workers committing at the same instant can still collide. Rather
  than raising immediately, a connection that meets that lock now waits
  up to 5 seconds for it to clear — turning a rare, real contention
  event into a short pause instead of a 500.
- `synchronous=NORMAL`: SQLite's own documentation recommends this once
  WAL is on. It still fsyncs at WAL checkpoints, so a commit survives a
  process or OS crash; it just skips the fsync on every single
  transaction, which is the expensive part.
- `foreign_keys=ON`: SQLite ships this off by default for backward
  compatibility with pre-3.6.19 behaviour. Django's models declare real
  foreign key relationships and assume the database enforces them.

A deployment that opts into Postgres instead (DATABASE_URL set) doesn't
need any of this — Postgres has its own MVCC and row-level locking — so
this is a no-op for every vendor other than sqlite.
"""


def configure_sqlite(sender, connection, **kwargs):
    """`connection_created` receiver: apply the WAL pragmas above to a
    freshly opened SQLite connection, and do nothing for any other
    database backend.

    Signature matches Django's `connection_created` signal
    (sender=connection's class, connection=the new connection,
    **kwargs to absorb anything else the signal may one day carry).
    """
    if connection.vendor != 'sqlite':
        return
    cursor = connection.cursor()
    try:
        cursor.execute('PRAGMA journal_mode=WAL;')
        cursor.execute('PRAGMA busy_timeout=5000;')
        cursor.execute('PRAGMA synchronous=NORMAL;')
        cursor.execute('PRAGMA foreign_keys=ON;')
    finally:
        cursor.close()
