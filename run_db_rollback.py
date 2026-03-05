r"""
Run DB rollback SQL file without mysql CLI.

Usage:
  .\\venv\\Scripts\\python.exe run_db_rollback.py
  .\\venv\\Scripts\\python.exe run_db_rollback.py --password your_password
"""

from __future__ import annotations

import argparse
import asyncio
from getpass import getpass
from pathlib import Path

import aiomysql

from run_db_migration import DB_CONFIG, _run_file


ROLLBACK_FILE = Path("migrations/2026_02_11_db_efficiency/04_down.sql")


async def _main(password: str) -> int:
    cfg = dict(DB_CONFIG)
    cfg["password"] = password

    print(f"Connecting to {cfg['host']} / {cfg['db']} as {cfg['user']} ...")
    conn = await aiomysql.connect(**cfg)
    try:
        await _run_file(conn, ROLLBACK_FILE)
        print("\nRollback completed successfully.")
        return 0
    finally:
        conn.close()
        await conn.ensure_closed()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run DB rollback SQL file.")
    parser.add_argument(
        "--password",
        help="MySQL password. If omitted, you will be prompted securely.",
    )
    args = parser.parse_args()

    password = args.password or getpass("Enter MySQL password: ")
    return asyncio.run(_main(password))


if __name__ == "__main__":
    raise SystemExit(main())
