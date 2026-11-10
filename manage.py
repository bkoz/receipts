"""Local maintenance commands for the private receipt archive."""

from __future__ import annotations

import argparse
import sqlite3
import shutil
import sys
from pathlib import Path
from typing import TextIO

import main as app_main
from main import count_receipts, ensure_storage, exclusive_data_lock


def _storage_entries() -> list[Path]:
    return [path for path in app_main.DATA_DIR.iterdir() if path.name != ".app.lock"]


def _remove_storage_entry(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path)


def initialize_command(stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    if not stdin.isatty():
        print("Refusing to reset application storage without an interactive terminal.", file=stderr)
        return 2

    try:
        data_dir = app_main.DATA_DIR.resolve()
        project_root = app_main.ROOT.resolve()
        if data_dir == project_root or project_root.is_relative_to(data_dir):
            raise RuntimeError("DATA_DIR must be a dedicated directory, not the project directory or one of its parents.")
        with exclusive_data_lock():
            entries = _storage_entries()
            try:
                receipt_count: int | None = count_receipts() if app_main.DB_PATH.is_file() else None
            except sqlite3.Error:
                receipt_count = None
            print("This will completely reset application storage and recreate an empty database.", file=stdout)
            print("Every file and folder in DATA_DIR will be removed, including receipts, uploads, exports, and the current database.", file=stdout)
            count_summary = f"{receipt_count} saved record(s)" if receipt_count is not None else "An unknown number of saved records"
            print(f"{count_summary} and {len(entries)} top-level storage item(s) will be removed.", file=stdout)
            print("Type INITIALIZE to continue:", end=" ", file=stdout, flush=True)
            if stdin.readline().rstrip("\r\n") != "INITIALIZE":
                print("Application storage was not reset.", file=stdout)
                return 1

            for entry in entries:
                _remove_storage_entry(entry)
            ensure_storage(expire_staged=False)
    except Exception as exc:
        print(f"Application storage could not be reset: {exc}", file=stderr)
        return 1

    count_summary = f"{receipt_count} saved receipt record(s)" if receipt_count is not None else "saved receipt records"
    print(f"Application storage initialized. Removed {count_summary} and recreated the empty database.", file=stdout)
    return 0


def main(argv: list[str] | None = None, *, stdin: TextIO | None = None, stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(description="Initialize local receipt application storage.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("initialize", help="reset all application storage and recreate the empty database")
    parser.parse_args(argv)

    return initialize_command(
        stdin if stdin is not None else sys.stdin,
        stdout if stdout is not None else sys.stdout,
        stderr if stderr is not None else sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())
