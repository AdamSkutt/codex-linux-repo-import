from __future__ import annotations

from pathlib import Path
import sqlite3


class CatalogError(RuntimeError):
    pass


def load_thread_catalog(database: Path) -> set[str]:
    """Read only native thread ids; titles, previews, and messages are not queried."""

    if not database.is_file():
        raise CatalogError(f"native thread catalog not found: {database}")
    uri = database.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=3)
        try:
            connection.execute("PRAGMA query_only = ON")
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(threads)")
                if len(row) > 1
            }
            if "id" not in columns:
                raise CatalogError("native thread catalog has no threads.id column")
            return {
                row[0]
                for row in connection.execute("SELECT id FROM threads WHERE id IS NOT NULL")
                if isinstance(row[0], str) and row[0]
            }
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise CatalogError(f"cannot read native thread catalog: {exc}") from exc
