r"""
Run DB migration SQL files without mysql CLI.

Usage:
  .\\venv\\Scripts\\python.exe run_db_migration.py
  .\\venv\\Scripts\\python.exe run_db_migration.py --password your_password
"""

from __future__ import annotations

import argparse
import asyncio
from getpass import getpass
from pathlib import Path
from typing import Iterable

import aiomysql


DB_CONFIG = {
    "host": "172.16.5.32",
    "port": 3306,
    "user": "root",
    "db": "GHN_PROD_BAK",
    "connect_timeout": 60,
    "autocommit": True,
}

MIGRATION_FILES = [
    "migrations/2026_02_11_db_efficiency/00_precheck.sql",
    "migrations/2026_02_11_db_efficiency/01_backup.sql",
    "migrations/2026_02_11_db_efficiency/02_up.sql",
    "migrations/2026_02_11_db_efficiency/03_verify.sql",
]


def _iter_sql_statements(sql_text: str) -> Iterable[str]:
    """
    Yield SQL statements while honoring DELIMITER directives.
    Assumes each custom-delimiter statement ends at line end (true for this pack).
    """
    delimiter = ";"
    buffer: list[str] = []

    for raw_line in sql_text.splitlines():
        stripped = raw_line.strip()

        if not stripped:
            continue
        if stripped.startswith("--") or stripped.startswith("#"):
            continue

        if stripped.upper().startswith("DELIMITER "):
            delimiter = stripped.split(None, 1)[1]
            continue

        buffer.append(raw_line)
        candidate = "\n".join(buffer).rstrip()
        if candidate.endswith(delimiter):
            stmt = candidate[: -len(delimiter)].strip()
            buffer = []
            if stmt:
                yield stmt

    trailing = "\n".join(buffer).strip()
    if trailing:
        yield trailing


async def _run_file(conn: aiomysql.Connection, file_path: Path) -> None:
    if not file_path.exists():
        raise FileNotFoundError(f"Migration file not found: {file_path}")

    print(f"\n== Running: {file_path}")
    sql_text = file_path.read_text(encoding="utf-8")
    statements = list(_iter_sql_statements(sql_text))
    print(f"   statements: {len(statements)}")

    async with conn.cursor() as cur:
        for i, stmt in enumerate(statements, start=1):
            await cur.execute(stmt)
            if i % 10 == 0 or i == len(statements):
                print(f"   executed: {i}/{len(statements)}")

    print(f"== Done: {file_path}")


async def _main(password: str) -> int:
    cfg = dict(DB_CONFIG)
    cfg["password"] = password

    print(f"Connecting to {cfg['host']} / {cfg['db']} as {cfg['user']} ...")
    conn = await aiomysql.connect(**cfg)
    try:
        for file_name in MIGRATION_FILES:
            await _run_file(conn, Path(file_name))
        print("\nAll migration files completed successfully.")
        return 0
    finally:
        conn.close()
        await conn.ensure_closed()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run DB migration SQL files.")
    parser.add_argument(
        "--password",
        help="MySQL password. If omitted, you will be prompted securely.",
    )
    args = parser.parse_args()

    password = args.password or getpass("Enter MySQL password: ")
    return asyncio.run(_main(password))


if __name__ == "__main__":
    raise SystemExit(main())
