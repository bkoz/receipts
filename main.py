from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from PIL import Image, ImageOps, UnidentifiedImageError


ROOT = Path(__file__).resolve().parent


def read_dotenv(path: Path) -> dict[str, str]:
    """Read the small KEY=value subset used by this app, without overriding env."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


DOTENV = read_dotenv(ROOT / ".env")


def setting(name: str, default: str = "") -> str:
    return os.environ.get(name, DOTENV.get(name, default)).strip()


APP_PASSWORD = setting("APP_PASSWORD")
LLM_BASE_URL = setting("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
LLM_MODEL = setting("LLM_MODEL", "gpt-5.4-mini")
LLM_API_KEY = setting("LLM_API_KEY")
COOKIE_SECURE = setting("COOKIE_SECURE", "false").lower() in {"1", "true", "yes", "on"}
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
STAGED_TTL_SECONDS = 24 * 60 * 60
SESSION_TTL_SECONDS = 14 * 24 * 60 * 60
COOKIE_NAME = "receipt_session"

DATA_DIR = Path(setting("DATA_DIR", str(ROOT / "data"))).expanduser().resolve()
ORIGINALS_DIR = DATA_DIR / "originals"
STAGING_DIR = DATA_DIR / "staged"
DB_PATH = DATA_DIR / "receipts.sqlite3"

RECEIPT_FIELDS = (
    "merchant", "receipt_date", "receipt_time", "subtotal", "tax", "total", "payment", "change", "currency",
)
CSV_FIELDS = (
    "receipt_id", "merchant", "receipt_date", "receipt_time", "subtotal", "tax", "total", "payment", "change",
    "currency", "item_description", "item_category", "archived_filename", "original_filename",
)
EXTRACTION_PROMPT = (
    "Read this receipt and return only a JSON object with these exact keys: merchant, receipt_date, receipt_time, "
    "subtotal, tax, total, payment, change, currency, items. Use YYYY-MM-DD dates, 24-hour HH:MM or HH:MM:SS times, decimal numbers without currency symbols, "
    "and three-letter ISO currency codes. receipt_time is the local time printed on the receipt; do not use the upload time "
    "or infer a time when none is printed. Do not infer a missing date year; use null if the year is not printed. "
    "Use null for unreadable or absent receipt fields. "
    "payment is the amount tendered or charged when explicitly printed. change is the amount returned to the customer, "
    "including any cash-back amount when the receipt labels it that way. These are the only summary fields for those amounts; "
    "do not infer values from other fields. "
    "items must be an array containing one object per purchased item, with string-or-null description and "
    "a concise string-or-null category. Include an explicitly labeled cash-back or cash-withdrawal line as "
    "a separate marker item with description Cash back and category Cash back. Keep its amount only in change, "
    "not in the item description, so the amount is not counted twice. Do not create a cash-back item for ordinary change due. "
    "Do not include prices for ordinary items, quantities, tax lines, totals, or payment lines other than the "
    "explicit cash-back line. Do not invent items or details; return [] when no purchased items are readable."
)
IMAGE_FORMATS = {
    "JPEG": ("image/jpeg", ".jpg"),
    "MPO": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
}


def ensure_storage(*, expire_staged: bool = True) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    ORIGINALS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    STAGING_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(DATA_DIR, 0o700)
        os.chmod(ORIGINALS_DIR, 0o700)
        os.chmod(STAGING_DIR, 0o700)
    except OSError:
        pass
    connection = sqlite3.connect(DB_PATH)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS receipts (
                id TEXT PRIMARY KEY,
                merchant TEXT NOT NULL DEFAULT '',
                receipt_date TEXT NOT NULL DEFAULT '',
                receipt_time TEXT NOT NULL DEFAULT '',
                subtotal TEXT NOT NULL DEFAULT '',
                tax TEXT NOT NULL DEFAULT '',
                total TEXT NOT NULL DEFAULT '',
                payment TEXT NOT NULL DEFAULT '',
                change TEXT NOT NULL DEFAULT '',
                currency TEXT NOT NULL DEFAULT '',
                original_filename TEXT NOT NULL,
                stored_filename TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(receipts)")}
        for field in ("receipt_time", "payment", "change"):
            if field not in columns:
                connection.execute(f"ALTER TABLE receipts ADD COLUMN {field} TEXT NOT NULL DEFAULT ''")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS receipt_items (
                id TEXT PRIMARY KEY,
                receipt_id TEXT NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT ''
            )"""
        )
        connection.execute("CREATE INDEX IF NOT EXISTS receipt_items_receipt_idx ON receipt_items(receipt_id, position)")
        connection.commit()
    finally:
        connection.close()
    recover_delete_transaction()
    if expire_staged:
        expire_staged_uploads()


def db_connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


@contextmanager
def db_session():
    connection = db_connect()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


@contextmanager
def exclusive_data_lock():
    """Prevent the app and local maintenance commands from mutating storage together."""
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_file = (DATA_DIR / ".app.lock").open("a+")
    try:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write("\0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        lock_file.close()
        raise RuntimeError("The receipt app is running. Stop it before using this maintenance command.") from exc
    try:
        yield
    finally:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def delete_transaction_dir() -> Path:
    return DATA_DIR / ".delete-all-txn"


def _delete_roots() -> dict[str, Path]:
    return {"originals": ORIGINALS_DIR, "staged": STAGING_DIR, "csv": DATA_DIR}


def _files_to_delete() -> list[tuple[str, Path, Path]]:
    """Return stored originals, staged files, and saved CSVs under DATA_DIR."""
    entries: list[tuple[str, Path, Path]] = []
    for area in ("originals", "staged"):
        root = _delete_roots()[area]
        if root.exists():
            entries.extend(
                (area, path.relative_to(root), path)
                for path in root.rglob("*")
                if path.is_file() or path.is_symlink()
            )

    for path in DATA_DIR.rglob("*"):
        relative = path.relative_to(DATA_DIR)
        if relative.parts[0] in {"originals", "staged", ".delete-all-txn"}:
            continue
        if path.suffix.lower() == ".csv" and (path.is_file() or path.is_symlink()):
            entries.append(("csv", relative, path))
    return sorted(entries, key=lambda entry: (entry[0], str(entry[1])))


def count_receipt_data_files() -> int:
    return len(_files_to_delete())


def recover_delete_transaction() -> None:
    """Restore quarantined files if receipt rows remain; otherwise finish cleanup."""
    transaction_dir = delete_transaction_dir()
    if not transaction_dir.exists():
        return
    if transaction_dir.is_symlink() or not transaction_dir.is_dir():
        raise RuntimeError("Delete recovery directory is invalid; inspect the private data directory.")
    with db_session() as connection:
        has_receipts = connection.execute("SELECT 1 FROM receipts LIMIT 1").fetchone() is not None
    roots = _delete_roots()
    quarantined_files: list[tuple[str, Path, Path]] = []
    for area_dir in transaction_dir.iterdir():
        if area_dir.name not in roots or area_dir.is_symlink() or not area_dir.is_dir():
            raise RuntimeError("Delete recovery found an unexpected path; inspect the private data directory.")
        for path in area_dir.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                continue
            if not (path.is_file() or path.is_symlink()):
                raise RuntimeError("Delete recovery found an unexpected path; inspect the private data directory.")
            quarantined_files.append((area_dir.name, path.relative_to(area_dir), path))

    if has_receipts:
        for area, relative, quarantined in quarantined_files:
            destination = roots[area] / relative
            if destination.exists() or destination.is_symlink():
                raise RuntimeError("Delete recovery cannot restore a file because its path is occupied.")
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.replace(quarantined, destination)
    shutil.rmtree(transaction_dir)


def count_receipts() -> int:
    with db_session() as connection:
        row = connection.execute("SELECT COUNT(*) AS count FROM receipts").fetchone()
    return int(row["count"])


def _delete_receipt_rows(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM receipts")


def delete_all_receipts() -> tuple[int, int, bool]:
    """Delete rows/files and return (row_count, file_count, cleanup_pending).

    The caller must hold exclusive_data_lock().
    """
    ensure_storage(expire_staged=False)
    transaction_dir = delete_transaction_dir()
    transaction_dir.mkdir(mode=0o700)
    connection = db_connect()
    moved: list[tuple[Path, Path]] = []
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute("SELECT id FROM receipts").fetchall()
        for area, relative, source in _files_to_delete():
            if not source.exists() and not source.is_symlink():
                continue
            destination = transaction_dir / area / relative
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.replace(source, destination)
            moved.append((destination, source))
        _delete_receipt_rows(connection)
        connection.commit()
    except BaseException as exc:
        connection.rollback()
        restore_errors: list[OSError] = []
        for quarantined, original in reversed(moved):
            if quarantined.exists():
                try:
                    original.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    os.replace(quarantined, original)
                except OSError as restore_error:
                    restore_errors.append(restore_error)
        connection.close()
        if not restore_errors:
            try:
                shutil.rmtree(transaction_dir)
            except OSError:
                pass
        if restore_errors:
            raise RuntimeError("Delete failed and file recovery is pending; rerun the app to recover safely.") from exc
        raise
    else:
        connection.close()

    try:
        shutil.rmtree(transaction_dir)
        cleanup_pending = False
    except OSError:
        # Startup recovery removes the remaining quarantined files after the commit.
        cleanup_pending = True
    return len(rows), len(moved), cleanup_pending


def expire_staged_uploads(now: float | None = None) -> None:
    """Remove staged originals older than 24 hours; saved originals are untouched."""
    if not STAGING_DIR.exists():
        return
    cutoff = (time.time() if now is None else now) - STAGED_TTL_SECONDS
    referenced_images: set[str] = set()
    for metadata_path in STAGING_DIR.glob("*.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            created_at = float(metadata.get("created_at", 0))
            stored_filename = str(metadata.get("stored_filename", ""))
            if re.fullmatch(r"[a-f0-9]{32}\.(?:jpg|png|webp)", stored_filename):
                referenced_images.add(stored_filename)
            if created_at < cutoff:
                if stored_filename in referenced_images:
                    (STAGING_DIR / stored_filename).unlink(missing_ok=True)
                metadata_path.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            # An unreadable stage entry is not a saved receipt and can be discarded.
            metadata_path.unlink(missing_ok=True)
    for image_path in STAGING_DIR.iterdir():
        if image_path.is_file() and image_path.name not in referenced_images and image_path.stat().st_mtime < cutoff:
            image_path.unlink(missing_ok=True)


def cookie_signature(timestamp: str) -> str:
    key = hashlib.sha256(("receipt-app:" + APP_PASSWORD).encode("utf-8")).digest()
    return hmac.new(key, timestamp.encode("ascii"), hashlib.sha256).hexdigest()


def valid_session(token: str | None, now: int | None = None) -> bool:
    if not APP_PASSWORD or not token or "." not in token:
        return False
    timestamp, signature = token.split(".", 1)
    if not timestamp.isdecimal():
        return False
    issued = int(timestamp)
    current = int(time.time() if now is None else now)
    if issued > current + 60 or current - issued > SESSION_TTL_SECONDS:
        return False
    return hmac.compare_digest(signature, cookie_signature(timestamp))


def request_origin_is_same(request: Request) -> bool:
    origin = request.headers.get("origin")
    host = request.headers.get("host", "").lower()
    if not origin or not host:
        return False
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == host


def add_security_headers(response: Response) -> Response:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; manifest-src 'self'; object-src 'none'; base-uri 'self'; "
        "form-action 'self'; frame-ancestors 'none'",
    )
    return response


@asynccontextmanager
async def lifespan(_: FastAPI):
    with exclusive_data_lock():
        ensure_storage()
        yield


app = FastAPI(title="Receipt Ledger", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.middleware("http")
async def protect_and_harden(request: Request, call_next):
    path = request.url.path
    if path == "/" and not valid_session(request.cookies.get(COOKIE_NAME)):
        response = FileResponse(ROOT / "static" / "login.html", media_type="text/html", headers={"Cache-Control": "no-store"})
        return add_security_headers(response)
    is_login = path == "/login"
    is_public_static = path.startswith("/static/") and path != "/static/index.html"
    if not is_login and not is_public_static and not valid_session(request.cookies.get(COOKIE_NAME)):
        response = JSONResponse({"detail": "Authentication required."}, status_code=401, headers={"Cache-Control": "no-store"})
        return add_security_headers(response)
    if request.method not in {"GET", "HEAD", "OPTIONS"} and not is_login:
        if not request_origin_is_same(request) or request.headers.get("x-requested-with") != "receipt-app":
            return add_security_headers(JSONResponse({"detail": "Request origin could not be verified."}, status_code=403))
    response = await call_next(request)
    if path != "/static/service-worker.js":
        response.headers.setdefault("Cache-Control", "no-store")
    return add_security_headers(response)


@app.get("/", response_class=HTMLResponse)
async def home():
    return FileResponse(ROOT / "static" / "index.html", media_type="text/html", headers={"Cache-Control": "no-store"})


@app.post("/login")
async def login(request: Request):
    if not request_origin_is_same(request):
        return HTMLResponse("Login request origin could not be verified.", status_code=403)
    if not APP_PASSWORD or APP_PASSWORD == "change-this-password":
        return HTMLResponse("APP_PASSWORD is not configured. Set it in the environment or .env file and restart the app.", status_code=503)
    body = (await request.body())[:4096].decode("utf-8", errors="replace")
    submitted = parse_qs(body).get("password", [""])[0]
    if not hmac.compare_digest(submitted.encode("utf-8"), APP_PASSWORD.encode("utf-8")):
        return FileResponse(ROOT / "static" / "login.html", media_type="text/html", status_code=401, headers={"Cache-Control": "no-store", "X-Login-Error": "1"})
    timestamp = str(int(time.time()))
    token = f"{timestamp}.{cookie_signature(timestamp)}"
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(COOKIE_NAME, token, max_age=SESSION_TTL_SECONDS, httponly=True, secure=COOKIE_SECURE, samesite="strict", path="/")
    return response


@app.post("/logout")
async def logout():
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=COOKIE_SECURE, samesite="strict")
    return response


class ReceiptItemInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(default="", max_length=300)
    category: str = Field(default="", max_length=100)

    @field_validator("description", "category")
    @classmethod
    def normalize_item_text(cls, value: str) -> str:
        return " ".join(value.split())


class SaveReceiptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    staged_id: str = Field(min_length=32, max_length=64)
    merchant: str = Field(default="", max_length=200)
    receipt_date: str = Field(default="", max_length=10)
    receipt_time: str = Field(default="", max_length=8)
    subtotal: str = Field(default="", max_length=32)
    tax: str = Field(default="", max_length=32)
    total: str = Field(default="", max_length=32)
    payment: str = Field(default="", max_length=32)
    change: str = Field(default="", max_length=32)
    currency: str = Field(default="", max_length=3)
    items: list[ReceiptItemInput] = Field(default_factory=list, max_length=200)

    @field_validator("staged_id")
    @classmethod
    def safe_stage_id(cls, value: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{32}", value):
            raise ValueError("Invalid staged upload identifier")
        return value

    @field_validator("merchant")
    @classmethod
    def clean_merchant(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("receipt_date")
    @classmethod
    def valid_iso_date(cls, value: str) -> str:
        value = value.strip()
        if value:
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("Date must use YYYY-MM-DD format") from exc
            if len(value) != 10:
                raise ValueError("Date must use YYYY-MM-DD format")
        return value

    @field_validator("receipt_time")
    @classmethod
    def valid_receipt_time(cls, value: str) -> str:
        value = value.strip()
        if value and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?", value):
            raise ValueError("Time must use 24-hour HH:MM or HH:MM:SS format")
        return value

    @field_validator("subtotal", "tax", "total", "payment", "change")
    @classmethod
    def decimal_string(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        try:
            amount = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("Amount must be a decimal number") from exc
        if not amount.is_finite() or amount < 0:
            raise ValueError("Amount must be a non-negative decimal number")
        return format(amount, "f")

    @field_validator("currency")
    @classmethod
    def iso_currency(cls, value: str) -> str:
        value = value.strip().upper()
        if value and not re.fullmatch(r"[A-Z]{3}", value):
            raise ValueError("Currency must be a three-letter ISO code")
        return value

    @field_validator("items")
    @classmethod
    def remove_empty_items(cls, value: list[ReceiptItemInput]) -> list[ReceiptItemInput]:
        return [item for item in value if item.description or item.category]


def clean_original_filename(filename: str | None) -> str:
    raw = unquote(filename or "receipt").replace("\\", "/").split("/")[-1]
    cleaned = "".join(char for char in raw if char >= " " and char != "\x7f").strip()
    return (cleaned or "receipt")[:240]


def archived_image_filename(
    original_filename: str, image_suffix: str, receipt_date: str, receipt_time: str, collision_index: int = 1,
) -> str:
    """Build a readable, unique archive name while preserving the source basename."""
    timestamp = receipt_date or "undated"
    if receipt_time:
        timestamp += "T" + receipt_time.replace(":", "-")
    prefix = f"{timestamp}_"
    stem, _ = os.path.splitext(original_filename)
    if stem.casefold() == "image":
        stem = "receipt"
    suffix = image_suffix
    collision_suffix = f"_{collision_index}" if collision_index > 1 else ""

    def trim_utf8(value: str, max_bytes: int) -> str:
        result: list[str] = []
        used = 0
        for character in value:
            size = len(character.encode("utf-8"))
            if used + size > max_bytes:
                break
            result.append(character)
            used += size
        return "".join(result)

    prefix_size = len(prefix.encode("utf-8"))
    suffix = trim_utf8(suffix, min(32, 255 - prefix_size))
    stem_limit = 255 - prefix_size - len(suffix.encode("utf-8")) - len(collision_suffix)
    stem = trim_utf8(stem, stem_limit)
    return f"{prefix}{stem}{collision_suffix}{suffix}"


def reserve_archived_image(
    original_filename: str, image_suffix: str, receipt_date: str, receipt_time: str,
) -> tuple[str, Path]:
    collision_index = 1
    while True:
        filename = archived_image_filename(
            original_filename, image_suffix, receipt_date, receipt_time, collision_index,
        )
        path = ORIGINALS_DIR / filename
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            collision_index += 1
        else:
            os.close(descriptor)
            return filename, path


def receipt_archive_filename(row: sqlite3.Row, collision_index: int = 1) -> str:
    expected = archived_image_filename(
        row["original_filename"], Path(row["stored_filename"]).suffix,
        row["receipt_date"], row["receipt_time"], collision_index,
    )
    stored = row["stored_filename"]
    if collision_index == 1 and stored == expected:
        return stored
    if collision_index == 1:
        stem, suffix = os.path.splitext(expected)
        stored_stem, stored_suffix = os.path.splitext(stored)
        if stored_suffix == suffix and re.fullmatch(re.escape(stem) + r"_\d+", stored_stem):
            return stored
    return expected


def receipt_archive_filenames(rows: list[sqlite3.Row]) -> dict[str, str]:
    names: dict[str, str] = {}
    used: set[str] = set()
    for row in sorted(rows, key=lambda item: (item["created_at"], item["id"])):
        collision_index = 1
        name = receipt_archive_filename(row, collision_index)
        while name in used:
            collision_index += 1
            name = receipt_archive_filename(row, collision_index)
        names[row["id"]] = name
        used.add(name)
    return names


def validate_image(data: bytes) -> tuple[str, str]:
    if not data:
        raise HTTPException(status_code=400, detail="Choose an image file first.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Images must be 15 MB or smaller.")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image_format = image.format
            width, height = image.size
            if width < 1 or height < 1 or width * height > 50_000_000:
                raise HTTPException(status_code=400, detail="This image has unsupported dimensions.")
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="The upload is not a readable JPEG, PNG, or WebP image.") from exc
    if image_format not in IMAGE_FORMATS:
        raise HTTPException(status_code=400, detail="Only JPEG, PNG, and WebP images are supported.")
    mime, extension = IMAGE_FORMATS[image_format]
    return mime, extension


def vision_compatible_image(data: bytes, mime: str) -> tuple[bytes, str]:
    """Convert MPO containers to a regular JPEG for vision while keeping originals intact."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "MPO":
                return data, mime
            image.seek(0)
            normalized = ImageOps.exif_transpose(image).convert("RGB")
            output = io.BytesIO()
            normalized.save(output, format="JPEG", quality=92)
            return output.getvalue(), "image/jpeg"
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="This image could not be prepared for receipt extraction.") from exc


def stage_paths(staged_id: str) -> tuple[Path, Path]:
    return STAGING_DIR / f"{staged_id}.json", STAGING_DIR / staged_id


def load_stage(staged_id: str) -> tuple[dict[str, Any], Path]:
    if not re.fullmatch(r"[a-f0-9]{32}", staged_id):
        raise HTTPException(status_code=404, detail="This upload has expired. Please choose the image again.")
    meta_path, _ = stage_paths(staged_id)
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        stored_filename = metadata["stored_filename"]
        if not re.fullmatch(re.escape(staged_id) + r"\.(?:jpg|png|webp)", stored_filename):
            raise ValueError("Invalid staged image reference")
        image_path = STAGING_DIR / stored_filename
        if not image_path.is_file() or time.time() - float(metadata["created_at"]) > STAGED_TTL_SECONDS:
            meta_path.unlink(missing_ok=True)
            image_path.unlink(missing_ok=True)
            raise HTTPException(status_code=404, detail="This upload has expired. Please choose the image again.")
        return metadata, image_path
    except HTTPException:
        raise
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=404, detail="This upload has expired. Please choose the image again.") from exc


def response_text_from_chat(payload: Any) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("The vision endpoint returned an incomplete response.") from exc
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"]
        return "\n".join(str(part) for part in parts).strip()
    raise ValueError("The vision endpoint returned an incomplete response.")


def parse_extraction_json(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError("The vision endpoint returned malformed JSON. Try the image again.") from exc
    if not isinstance(parsed, dict):
        raise ValueError("The vision endpoint returned malformed receipt fields. Try the image again.")
    if not all(field in parsed for field in RECEIPT_FIELDS) or "items" not in parsed:
        raise ValueError("The vision endpoint omitted required receipt fields. Try the image again.")
    result: dict[str, str] = {}
    for field in RECEIPT_FIELDS:
        value = parsed[field]
        if value is None or value == "":
            result[field] = ""
            continue
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError("The vision endpoint returned malformed receipt fields. Try the image again.")
        result[field] = str(value).strip()
    # Normalize values that cannot be represented faithfully in the export to unknown.
    try:
        if result["receipt_date"]:
            date.fromisoformat(result["receipt_date"])
            if len(result["receipt_date"]) != 10:
                result["receipt_date"] = ""
    except ValueError:
        result["receipt_date"] = ""
    if result["receipt_time"] and not re.fullmatch(
        r"(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?", result["receipt_time"],
    ):
        result["receipt_time"] = ""
    for field in ("subtotal", "tax", "total", "payment", "change"):
        if result[field]:
            try:
                amount = Decimal(result[field])
                result[field] = format(amount, "f") if amount.is_finite() and amount >= 0 else ""
            except InvalidOperation:
                result[field] = ""
    if result["currency"] and not re.fullmatch(r"[A-Za-z]{3}", result["currency"]):
        result["currency"] = ""
    else:
        result["currency"] = result["currency"].upper()
    result["merchant"] = " ".join(result["merchant"].split())[:200]

    raw_items = parsed["items"]
    if not isinstance(raw_items, list) or len(raw_items) > 200:
        raise ValueError("The vision endpoint returned malformed item details. Try the image again.")
    items: list[dict[str, str]] = []
    for item in raw_items:
        if not isinstance(item, dict) or not {"description", "category"}.issubset(item):
            raise ValueError("The vision endpoint returned malformed item details. Try the image again.")
        normalized_item: dict[str, str] = {}
        for field, limit in (("description", 300), ("category", 100)):
            value = item[field]
            if value is None or value == "":
                normalized_item[field] = ""
            elif isinstance(value, str):
                normalized_item[field] = " ".join(value.split())[:limit]
            else:
                raise ValueError("The vision endpoint returned malformed item details. Try the image again.")
        if normalized_item["description"] or normalized_item["category"]:
            items.append(normalized_item)
    return {"fields": result, "items": items}


def chat_completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def endpoint_requires_key(base_url: str) -> bool:
    host = (urlsplit(base_url).hostname or "").lower()
    return host == "api.openai.com" or host.endswith(".openai.azure.com")


def call_vision_endpoint(image_data: bytes, mime: str) -> dict[str, Any]:
    if not LLM_BASE_URL:
        raise RuntimeError("LLM_BASE_URL is not configured.")
    if not LLM_API_KEY and endpoint_requires_key(LLM_BASE_URL):
        raise RuntimeError("The configured vision endpoint requires an API key. Set LLM_API_KEY and restart the app.")
    image_url = f"data:{mime};base64,{base64.b64encode(image_data).decode('ascii')}"
    payload = json.dumps({
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": EXTRACTION_PROMPT},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]}],
        "max_completion_tokens": 3000,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"
    req = urllib.request.Request(chat_completions_url(LLM_BASE_URL), data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            provider_payload = json.loads(response.read(2_000_000).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403} and not LLM_API_KEY:
            raise RuntimeError("The configured vision endpoint requires an API key. Set LLM_API_KEY and restart the app.") from exc
        raise RuntimeError("The vision endpoint rejected the request. Check its URL, model, and API key.") from exc
    except (TimeoutError, urllib.error.URLError) as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError):
            raise TimeoutError("The vision endpoint timed out. Please try again.") from exc
        raise RuntimeError("The vision endpoint could not be reached. Check its URL and network connection.") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("The vision endpoint returned malformed JSON. Try the image again.") from exc
    return parse_extraction_json(response_text_from_chat(provider_payload))


@app.post("/api/receipts/extract")
async def extract_receipt(file: UploadFile = File(...)):
    ensure_storage()
    expire_staged_uploads()
    original_filename = clean_original_filename(file.filename)
    parts: list[bytes] = []
    total_size = 0
    while chunk := await file.read(1024 * 1024):
        total_size += len(chunk)
        if total_size > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Images must be 15 MB or smaller.")
        parts.append(chunk)
    image_data = b"".join(parts)
    mime, extension = validate_image(image_data)
    staged_id = uuid.uuid4().hex
    metadata_path, _ = stage_paths(staged_id)
    stored_filename = staged_id + extension
    image_path = STAGING_DIR / stored_filename
    try:
        image_path.write_bytes(image_data)
        try:
            os.chmod(image_path, 0o600)
        except OSError:
            pass
        metadata_path.write_text(json.dumps({
            "created_at": time.time(),
            "original_filename": original_filename,
            "stored_filename": stored_filename,
            "mime_type": mime,
        }), encoding="utf-8")
        try:
            vision_data, vision_mime = vision_compatible_image(image_data, mime)
            extraction = await asyncio.to_thread(call_vision_endpoint, vision_data, vision_mime)
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {
            "staged_id": staged_id,
            "original_filename": original_filename,
            "fields": extraction["fields"],
            "items": extraction["items"],
        }
    except Exception:
        image_path.unlink(missing_ok=True)
        metadata_path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()


@app.delete("/api/receipts/staged/{staged_id}")
async def discard_staged(staged_id: str):
    metadata_path, default_image = stage_paths(staged_id) if re.fullmatch(r"[a-f0-9]{32}", staged_id) else (None, None)
    if metadata_path is None or default_image is None:
        raise HTTPException(status_code=404, detail="Staged upload not found.")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        stored_filename = metadata.get("stored_filename", "")
        if re.fullmatch(re.escape(staged_id) + r"\.(?:jpg|png|webp)", stored_filename):
            (STAGING_DIR / stored_filename).unlink(missing_ok=True)
        metadata_path.unlink(missing_ok=True)
    except (OSError, ValueError, json.JSONDecodeError):
        default_image.unlink(missing_ok=True)
        metadata_path.unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/receipts")
async def save_receipt(receipt: SaveReceiptRequest):
    ensure_storage()
    metadata, staged_path = load_stage(receipt.staged_id)
    values = receipt.model_dump()
    values.pop("staged_id")
    items = values.pop("items")
    receipt_id = uuid.uuid4().hex
    archived_at = datetime.now(timezone.utc)
    stored_filename, destination = reserve_archived_image(
        metadata["original_filename"], staged_path.suffix, values["receipt_date"], values["receipt_time"],
    )
    created_at = archived_at.isoformat(timespec="microseconds")
    try:
        os.replace(staged_path, destination)
        with db_session() as connection:
            connection.execute(
                """INSERT INTO receipts
                (id, merchant, receipt_date, receipt_time, subtotal, tax, total, payment, change, currency, original_filename, stored_filename, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (receipt_id, *(values[field] for field in RECEIPT_FIELDS), metadata["original_filename"], stored_filename, created_at),
            )
            connection.executemany(
                """INSERT INTO receipt_items (id, receipt_id, position, description, category)
                VALUES (?, ?, ?, ?, ?)""",
                [
                    (uuid.uuid4().hex, receipt_id, position, item["description"], item["category"])
                    for position, item in enumerate(items)
                ],
            )
    except Exception:
        if staged_path.exists():
            destination.unlink(missing_ok=True)
        elif destination.exists():
            os.replace(destination, staged_path)
        raise
    try:
        stage_paths(receipt.staged_id)[0].unlink(missing_ok=True)
    except OSError:
        # The committed receipt remains valid; stale stage metadata expires later.
        pass
    return {"receipt_id": receipt_id}


def receipt_public(row: sqlite3.Row, items: list[dict[str, str]], archived_filename: str) -> dict[str, Any]:
    return {key: row[key] for key in ("id", *RECEIPT_FIELDS, "original_filename", "created_at")} | {
        "archived_filename": archived_filename, "items": items,
    }


@app.get("/api/receipts")
async def list_receipts():
    ensure_storage()
    with db_session() as connection:
        rows = connection.execute("SELECT * FROM receipts ORDER BY created_at DESC, id DESC").fetchall()
        archive_names = receipt_archive_filenames(rows)
        item_rows = connection.execute(
            "SELECT receipt_id, description, category FROM receipt_items ORDER BY receipt_id, position"
        ).fetchall()
    items_by_receipt: dict[str, list[dict[str, str]]] = {}
    for item in item_rows:
        items_by_receipt.setdefault(item["receipt_id"], []).append({
            "description": item["description"],
            "category": item["category"],
        })
    return {"receipts": [
        receipt_public(row, items_by_receipt.get(row["id"], []), archive_names[row["id"]]) for row in rows
    ]}


@app.get("/api/receipts.csv")
async def export_csv():
    ensure_storage()
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    with db_session() as connection:
        receipt_rows = connection.execute("SELECT * FROM receipts ORDER BY created_at ASC, id ASC").fetchall()
        archive_names = receipt_archive_filenames(receipt_rows)
        rows = connection.execute(
            """SELECT r.*, i.description AS item_description, i.category AS item_category
            FROM receipts AS r
            LEFT JOIN receipt_items AS i ON i.receipt_id = r.id
            ORDER BY r.created_at ASC, r.id ASC, i.position ASC"""
        ).fetchall()
    for row in rows:
        writer.writerow({
            "receipt_id": row["id"],
            "merchant": row["merchant"],
            "receipt_date": row["receipt_date"],
            "receipt_time": row["receipt_time"],
            "subtotal": row["subtotal"],
            "tax": row["tax"],
            "total": row["total"],
            "payment": row["payment"],
            "change": row["change"],
            "currency": row["currency"],
            "item_description": row["item_description"] or "",
            "item_category": row["item_category"] or "",
            "archived_filename": archive_names[row["id"]],
            "original_filename": row["original_filename"],
        })
    body = output.getvalue()
    return Response(body.encode("utf-8"), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="receipts.csv"', "Cache-Control": "no-store"})


@app.get("/api/receipts/{receipt_id}/file")
async def get_original(receipt_id: str):
    if not re.fullmatch(r"[a-f0-9]{32}", receipt_id):
        raise HTTPException(status_code=404, detail="Receipt not found.")
    with db_session() as connection:
        rows = connection.execute("SELECT * FROM receipts ORDER BY created_at ASC, id ASC").fetchall()
        row = next((item for item in rows if item["id"] == receipt_id), None)
        archive_names = receipt_archive_filenames(rows)
    if row is None:
        raise HTTPException(status_code=404, detail="Receipt not found.")
    path = ORIGINALS_DIR / row["stored_filename"]
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Receipt image is missing from storage.")
    return FileResponse(path, filename=archive_names[receipt_id], media_type="application/octet-stream", headers={"Cache-Control": "no-store"})


@app.get("/api/receipts.zip")
async def export_zip():
    with db_session() as connection:
        rows = connection.execute("SELECT * FROM receipts ORDER BY created_at ASC, id ASC").fetchall()
        archive_names = receipt_archive_filenames(rows)
    output = io.BytesIO()
    used_names: set[str] = set()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for row in rows:
            path = ORIGINALS_DIR / row["stored_filename"]
            if not path.is_file():
                continue
            name = archive_names[row["id"]]
            if name in used_names:
                stem, suffix = os.path.splitext(name)
                collision_index = 2
                while f"{stem}_{collision_index}{suffix}" in used_names:
                    collision_index += 1
                name = f"{stem}_{collision_index}{suffix}"
            used_names.add(name)
            archive.write(path, arcname=name)
    output.seek(0)
    return StreamingResponse(output, media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="receipt-images.zip"', "Cache-Control": "no-store"})


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(ROOT / "static" / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/service-worker.js")
async def service_worker():
    return FileResponse(ROOT / "static" / "service-worker.js", media_type="application/javascript", headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/icon.svg")
async def icon():
    return FileResponse(ROOT / "static" / "icon.svg", media_type="image/svg+xml")
