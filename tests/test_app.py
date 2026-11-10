import io
import json
import os
import sqlite3
import time
import uuid
import zipfile
from urllib.parse import unquote

os.environ["APP_PASSWORD"] = "test-password-only"
os.environ["LLM_BASE_URL"] = "http://127.0.0.1:9999/v1"
os.environ["LLM_API_KEY"] = ""

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import main
import manage


ORIGIN_HEADERS = {"Origin": "http://testserver", "X-Requested-With": "receipt-app"}


@pytest.fixture
def private_store(tmp_path, monkeypatch):
    data_dir = tmp_path / "private-data"
    monkeypatch.setattr(main, "DATA_DIR", data_dir)
    monkeypatch.setattr(main, "ORIGINALS_DIR", data_dir / "originals")
    monkeypatch.setattr(main, "STAGING_DIR", data_dir / "staged")
    monkeypatch.setattr(main, "DB_PATH", data_dir / "receipts.sqlite3")
    main.ensure_storage(expire_staged=False)
    return data_dir


@pytest.fixture
def client(private_store):
    with TestClient(main.app) as test_client:
        yield test_client


def png_bytes(color=(31, 100, 70)):
    buffer = io.BytesIO()
    Image.new("RGB", (40, 60), color).save(buffer, format="PNG")
    return buffer.getvalue()


def mpo_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (40, 60), (31, 100, 70)).save(
        buffer,
        format="MPO",
        save_all=True,
        append_images=[Image.new("RGB", (40, 60), (72, 12, 133))],
    )
    return buffer.getvalue()


def login(client):
    response = client.post("/login", data={"password": "test-password-only"}, headers={"Origin": "http://testserver"})
    assert response.status_code == 200
    assert client.cookies.get(main.COOKIE_NAME)


def add_saved_receipt(filename="saved.png"):
    receipt_id = uuid.uuid4().hex
    stored_filename = f"{uuid.uuid4().hex}.png"
    original = png_bytes()
    image_path = main.ORIGINALS_DIR / stored_filename
    image_path.write_bytes(original)
    with main.db_session() as connection:
        connection.execute(
            """INSERT INTO receipts
            (id, merchant, receipt_date, subtotal, tax, total, currency, original_filename, stored_filename, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (receipt_id, "Test shop", "2026-01-02", "1.00", "", "1.00", "USD", filename,
             stored_filename, "2026-01-02T00:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO receipt_items (id, receipt_id, position, description, category) VALUES (?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, receipt_id, 0, "Test item", "Test category"),
        )
    return receipt_id, image_path, original


class TTYInput(io.StringIO):
    def isatty(self):
        return True


def test_authentication_protects_ui_and_downloads(client):
    assert "Sign in" in client.get("/").text
    assert client.get("/api/receipts").status_code == 401
    assert client.get("/api/receipts.csv").status_code == 401
    assert client.get("/api/receipts.zip").status_code == 401
    assert client.get("/static/index.html").status_code == 401
    login(client)
    assert "Receipts, in order." in client.get("/").text
    assert client.get("/api/receipts").json() == {"receipts": []}


def test_image_validation_and_extraction_parsing():
    assert main.validate_image(png_bytes()) == ("image/png", ".png")
    with pytest.raises(Exception):
        main.validate_image(b"not an image")
    with pytest.raises(Exception):
        main.validate_image(b"x" * (main.MAX_UPLOAD_BYTES + 1))


def test_archived_filename_uses_receipt_for_generic_image_name():
    assert main.archived_image_filename("image.jpeg", ".jpg", "2026-11-08", "22:03:04") == (
        "2026-11-08T22-03-04_receipt.jpg"
    )
    assert main.archived_image_filename("image.jpeg", ".jpg", "2026-11-08", "") == "2026-11-08_receipt.jpg"
    assert main.archived_image_filename("image.jpeg", ".jpg", "", "22:03") == "undatedT22-03_receipt.jpg"
    assert main.archived_image_filename("image.jpeg", ".jpg", "2026-11-08", "22:03:04", 2) == (
        "2026-11-08T22-03-04_receipt_2.jpg"
    )


def test_mpo_upload_stays_original_and_gets_a_jpeg_vision_copy():
    original = mpo_bytes()
    mime, extension = main.validate_image(original)
    assert (mime, extension) == ("image/jpeg", ".jpg")
    vision_data, vision_mime = main.vision_compatible_image(original, mime)
    assert vision_mime == "image/jpeg"
    assert vision_data != original
    with Image.open(io.BytesIO(vision_data)) as image:
        assert image.format == "JPEG"
    with Image.open(io.BytesIO(original)) as image:
        assert image.format == "MPO" and image.n_frames == 2

    parsed = main.parse_extraction_json(json.dumps({
        "merchant": "  Green   Market ", "receipt_date": "not-a-date", "receipt_time": "20:03:04", "subtotal": "4.20",
        "tax": None, "total": "4.50", "payment": "25.00", "change": "20.00", "currency": "usd",
        "items": [
            {"description": "  Organic   apples ", "category": "  Produce  "},
            {"description": "Cash back", "category": "Cash back"},
        ],
    }))
    assert parsed == {
        "fields": {
            "merchant": "Green Market", "receipt_date": "", "receipt_time": "20:03:04", "subtotal": "4.20",
            "tax": "", "total": "4.50", "payment": "25.00", "change": "20.00", "currency": "USD",
        },
        "items": [
            {"description": "Organic apples", "category": "Produce"},
            {"description": "Cash back", "category": "Cash back"},
        ],
    }
    unknown = main.parse_extraction_json(
        '{"merchant":"Shop","receipt_date":null,"receipt_time":null,"subtotal":null,"tax":null,"total":null,'
        '"payment":null,"change":null,"currency":null,"items":[]}'
    )
    assert unknown["fields"]["merchant"] == "Shop" and unknown["items"] == []
    with pytest.raises(ValueError, match="malformed JSON"):
        main.parse_extraction_json("not JSON")
    with pytest.raises(ValueError, match="omitted"):
        main.parse_extraction_json('{"merchant":"Shop"}')
    assert "category Cash back" in main.EXTRACTION_PROMPT
    assert "Keep its amount only in change" in main.EXTRACTION_PROMPT
    assert "ordinary change due" in main.EXTRACTION_PROMPT


def test_openai_endpoint_requires_key_without_network(monkeypatch):
    monkeypatch.setattr(main, "LLM_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setattr(main, "LLM_API_KEY", "")
    with pytest.raises(RuntimeError, match="requires an API key"):
        main.call_vision_endpoint(png_bytes(), "image/png")


def test_existing_database_migrates_new_payment_summary_fields(tmp_path, monkeypatch):
    data_dir = tmp_path / "legacy-data"
    data_dir.mkdir()
    db_path = data_dir / "receipts.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """CREATE TABLE receipts (
                id TEXT PRIMARY KEY, merchant TEXT NOT NULL DEFAULT '', receipt_date TEXT NOT NULL DEFAULT '',
                subtotal TEXT NOT NULL DEFAULT '', tax TEXT NOT NULL DEFAULT '', total TEXT NOT NULL DEFAULT '',
                currency TEXT NOT NULL DEFAULT '', original_filename TEXT NOT NULL,
                stored_filename TEXT NOT NULL, created_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            "INSERT INTO receipts (id, merchant, original_filename, stored_filename, created_at) VALUES (?, ?, ?, ?, ?)",
            ("a" * 32, "Legacy shop", "legacy.jpg", "stored.jpg", "2026-01-01T00:00:00+00:00"),
        )
    monkeypatch.setattr(main, "DATA_DIR", data_dir)
    monkeypatch.setattr(main, "ORIGINALS_DIR", data_dir / "originals")
    monkeypatch.setattr(main, "STAGING_DIR", data_dir / "staged")
    monkeypatch.setattr(main, "DB_PATH", db_path)
    main.ensure_storage(expire_staged=False)
    with main.db_connect() as connection:
        row = connection.execute("SELECT * FROM receipts WHERE id = ?", ("a" * 32,)).fetchone()
        columns = {item[1] for item in connection.execute("PRAGMA table_info(receipts)")}
    assert {"receipt_time", "payment", "change"}.issubset(columns)
    assert (row["merchant"], row["receipt_time"], row["payment"], row["change"]) == ("Legacy shop", "", "", "")


def test_provider_timeout_returns_clear_error_and_discards_stage(client, monkeypatch):
    login(client)

    def timeout(*_):
        raise TimeoutError("The vision endpoint timed out. Please try again.")

    monkeypatch.setattr(main, "call_vision_endpoint", timeout)
    response = client.post(
        "/api/receipts/extract", files={"file": ("receipt.png", png_bytes(), "image/png")}, headers=ORIGIN_HEADERS
    )
    assert response.status_code == 504
    assert "timed out" in response.json()["detail"]
    assert list(main.STAGING_DIR.iterdir()) == []


def test_reviewed_fields_persist_and_exports_preserve_originals(private_store, monkeypatch):
    extracted_fields = {
        "merchant": "Green Market", "receipt_date": "2026-10-12", "receipt_time": "10:15:00", "subtotal": "10.00",
        "tax": "0.83", "total": "10.83", "payment": "31.00", "change": "20.00", "currency": "USD",
    }
    extracted_items = [
        {"description": "Coffee", "category": "Beverages"},
        {"description": "Muffin", "category": "Bakery"},
        {"description": "Cash back", "category": "Cash back"},
    ]
    monkeypatch.setattr(main, "call_vision_endpoint", lambda *_: {"fields": extracted_fields, "items": extracted_items})
    original = png_bytes((72, 12, 133))
    with TestClient(main.app) as client:
        login(client)
        upload = client.post(
            "/api/receipts/extract",
            files={"file": ('market,"north".png', original, "image/png")},
            headers=ORIGIN_HEADERS,
        )
        assert upload.status_code == 200
        stage = upload.json()["staged_id"]
        assert upload.json()["fields"] == extracted_fields
        assert upload.json()["items"] == extracted_items
        save = client.post(
            "/api/receipts",
            json={
                **extracted_fields,
                "merchant": "Corrected Market",
                "total": "10.80",
                "change": "20.00",
                "staged_id": stage,
                "items": [
                    {"description": "Large coffee", "category": "Drinks"},
                    {"description": "Muffin", "category": "Bakery"},
                    {"description": "Cash back", "category": "Cash back"},
                ],
            },
            headers=ORIGIN_HEADERS,
        )
        assert save.status_code == 200
        receipt_id = save.json()["receipt_id"]
        listed = client.get("/api/receipts").json()["receipts"]
        assert listed[0]["merchant"] == "Corrected Market"
        assert listed[0]["total"] == "10.80"
        assert listed[0]["receipt_time"] == "10:15:00"
        assert listed[0]["payment"] == "31.00"
        assert listed[0]["change"] == "20.00"
        archived_filename = listed[0]["archived_filename"]
        assert archived_filename == '2026-10-12T10-15-00_market,"north".png'
        assert listed[0]["items"] == [
            {"description": "Large coffee", "category": "Drinks"},
            {"description": "Muffin", "category": "Bakery"},
            {"description": "Cash back", "category": "Cash back"},
        ]

        csv_response = client.get("/api/receipts.csv")
        assert csv_response.status_code == 200
        rows = list(__import__("csv").DictReader(io.StringIO(csv_response.text)))
        assert len(rows) == 3
        assert all(row["receipt_id"] == receipt_id for row in rows)
        assert all(row["merchant"] == "Corrected Market" for row in rows)
        assert [row["item_description"] for row in rows] == ["Large coffee", "Muffin", "Cash back"]
        assert [row["item_category"] for row in rows] == ["Drinks", "Bakery", "Cash back"]
        assert all(row["payment"] == "31.00" for row in rows)
        assert all(row["change"] == "20.00" for row in rows)
        assert all(row["receipt_time"] == "10:15:00" for row in rows)
        assert all(row["archived_filename"] == archived_filename for row in rows)
        assert all(row["original_filename"] == 'market,"north".png' for row in rows)
        assert list(rows[0]) == list(main.CSV_FIELDS)

        downloaded = client.get(f"/api/receipts/{receipt_id}/file")
        assert downloaded.status_code == 200
        assert downloaded.content == original
        assert archived_filename in unquote(downloaded.headers["content-disposition"])
        zipped = client.get("/api/receipts.zip")
        with zipfile.ZipFile(io.BytesIO(zipped.content)) as archive:
            assert archive.namelist() == [archived_filename]
            assert archive.read(archived_filename) == original

    # A new app lifespan over the same data directory sees the saved row and image.
    with TestClient(main.app) as restarted:
        login(restarted)
        restarted_receipts = restarted.get("/api/receipts").json()["receipts"]
        assert restarted_receipts[0]["id"] == receipt_id
        assert restarted.get(f"/api/receipts/{receipt_id}/file").content == original


def test_unknown_fields_export_as_blank_and_origin_check_blocks_mutations(client, monkeypatch):
    login(client)
    assert client.post("/logout", headers={"Origin": "http://evil.test", "X-Requested-With": "receipt-app"}).status_code == 403
    monkeypatch.setattr(main, "call_vision_endpoint", lambda *_: {
        "fields": {"merchant": "Unknown shop", "receipt_date": "", "receipt_time": "", "subtotal": "", "tax": "", "total": "",
                   "payment": "", "change": "", "currency": ""},
        "items": [],
    })
    upload = client.post("/api/receipts/extract", files={"file": ("unknown.png", png_bytes(), "image/png")}, headers=ORIGIN_HEADERS)
    assert upload.status_code == 200
    save = client.post("/api/receipts", json={"staged_id": upload.json()["staged_id"], "merchant": "Unknown shop"}, headers=ORIGIN_HEADERS)
    assert save.status_code == 200
    row = list(__import__("csv").DictReader(io.StringIO(client.get("/api/receipts.csv").text)))[0]
    assert row["receipt_date"] == row["receipt_time"] == row["subtotal"] == row["tax"] == row["total"] == ""
    assert row["payment"] == row["change"] == row["currency"] == ""
    assert row["item_description"] == row["item_category"] == ""


def test_initialize_cli_requires_interactive_exact_confirmation(private_store):
    _, image_path, original = add_saved_receipt()
    stdout = io.StringIO()
    stderr = io.StringIO()
    result = manage.main(["initialize"], stdin=io.StringIO("INITIALIZE\n"), stdout=stdout, stderr=stderr)
    assert result == 2
    assert main.count_receipts() == 1
    assert image_path.read_bytes() == original

    result = manage.main(["initialize"], stdin=TTYInput("initialize\n"), stdout=stdout, stderr=stderr)
    assert result == 1
    assert main.count_receipts() == 1
    assert image_path.read_bytes() == original


def test_initialize_resets_even_an_empty_archive(private_store):
    stdout = io.StringIO()
    result = manage.main(["initialize"], stdin=TTYInput("INITIALIZE\n"), stdout=stdout, stderr=io.StringIO())
    assert result == 0
    assert "initialized" in stdout.getvalue()
    assert main.count_receipts() == 0
    with main.db_connect() as connection:
        assert {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")} == {
            "receipts", "receipt_items",
        }


def test_initialize_command_refuses_while_app_is_running(client):
    _, image_path, original = add_saved_receipt()
    result = manage.main(["initialize"], stdin=TTYInput("INITIALIZE\n"), stdout=io.StringIO(), stderr=io.StringIO())
    assert result == 1
    assert main.count_receipts() == 1
    assert image_path.read_bytes() == original


def test_initialize_removes_all_storage_and_recreates_a_fresh_database(private_store):
    _, image_path, _ = add_saved_receipt()
    orphan_path = main.ORIGINALS_DIR / f"{uuid.uuid4().hex}.jpg"
    orphan_path.write_bytes(b"orphaned receipt image")
    staged_id = uuid.uuid4().hex
    staged_image = main.STAGING_DIR / f"{staged_id}.png"
    staged_metadata = main.STAGING_DIR / f"{staged_id}.json"
    staged_image.write_bytes(png_bytes((80, 30, 10)))
    staged_metadata.write_text(json.dumps({
        "created_at": time.time(),
        "original_filename": "pending.png",
        "stored_filename": staged_image.name,
        "mime_type": "image/png",
    }), encoding="utf-8")
    csv_file = main.DATA_DIR / "receipts.csv"
    csv_file.write_text("receipt_id,merchant\n", encoding="utf-8")
    nested_csv = main.DATA_DIR / "exports" / "annual" / "archive.CSV"
    nested_csv.parent.mkdir(parents=True)
    nested_csv.write_text("receipt_id,merchant\n", encoding="utf-8")
    unrelated_file = main.DATA_DIR / "old-export.bin"
    unrelated_file.write_bytes(b"stale application storage")
    with main.db_session() as connection:
        connection.execute("CREATE TABLE stale_schema (id TEXT PRIMARY KEY)")

    stdout = io.StringIO()
    result = manage.main(["initialize"], stdin=TTYInput("INITIALIZE\n"), stdout=stdout, stderr=io.StringIO())
    assert result == 0
    assert main.count_receipts() == 0
    with main.db_session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM receipt_items").fetchone()[0] == 0
    assert not image_path.exists()
    assert not orphan_path.exists()
    assert not staged_image.exists() and not staged_metadata.exists()
    assert not csv_file.exists() and not nested_csv.exists() and not unrelated_file.exists()
    assert not main.delete_transaction_dir().exists()
    assert main.DB_PATH.is_file()
    assert main.ORIGINALS_DIR.is_dir() and list(main.ORIGINALS_DIR.iterdir()) == []
    assert main.STAGING_DIR.is_dir() and list(main.STAGING_DIR.iterdir()) == []
    with main.db_connect() as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == {"receipts", "receipt_items"}
    assert main.count_receipt_data_files() == 0
    with TestClient(main.app) as client:
        login(client)
        assert client.get("/api/receipts").json() == {"receipts": []}
        csv_rows = list(__import__("csv").DictReader(io.StringIO(client.get("/api/receipts.csv").text)))
        assert csv_rows == []
        with zipfile.ZipFile(io.BytesIO(client.get("/api/receipts.zip").content)) as archive:
            assert archive.namelist() == []


def test_delete_failure_restores_original_and_database_row(private_store, monkeypatch):
    _, image_path, original = add_saved_receipt()
    staged_file = main.STAGING_DIR / "pending-upload.bin"
    staged_file.write_bytes(b"temporary upload")
    csv_file = main.DATA_DIR / "receipts.csv"
    csv_file.write_text("saved export", encoding="utf-8")

    def fail_delete(_connection):
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(main, "_delete_receipt_rows", fail_delete)
    with pytest.raises(RuntimeError, match="simulated database failure"):
        main.delete_all_receipts()
    assert main.count_receipts() == 1
    with main.db_session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM receipt_items").fetchone()[0] == 1
    assert image_path.read_bytes() == original
    assert staged_file.read_bytes() == b"temporary upload"
    assert csv_file.read_text(encoding="utf-8") == "saved export"
    assert not main.delete_transaction_dir().exists()


def test_startup_recovers_interrupted_delete_based_on_database_commit(private_store):
    _, image_path, original = add_saved_receipt("rollback.png")
    staged_file = main.STAGING_DIR / "pending.bin"
    staged_file.write_bytes(b"pending upload")
    csv_file = main.DATA_DIR / "receipts.csv"
    csv_file.write_text("saved export", encoding="utf-8")
    txn_dir = main.delete_transaction_dir()
    txn_dir.mkdir()
    original_quarantine = txn_dir / "originals" / image_path.name
    original_quarantine.parent.mkdir()
    os.replace(image_path, original_quarantine)
    staged_quarantine = txn_dir / "staged" / staged_file.name
    staged_quarantine.parent.mkdir()
    os.replace(staged_file, staged_quarantine)
    csv_quarantine = txn_dir / "csv" / csv_file.name
    csv_quarantine.parent.mkdir()
    os.replace(csv_file, csv_quarantine)
    main.ensure_storage(expire_staged=False)
    assert image_path.read_bytes() == original
    assert staged_file.read_bytes() == b"pending upload"
    assert csv_file.read_text(encoding="utf-8") == "saved export"
    assert not txn_dir.exists()

    with main.db_session() as connection:
        connection.execute("DELETE FROM receipts")
    txn_dir.mkdir()
    for area, path in (("originals", image_path), ("staged", staged_file), ("csv", csv_file)):
        quarantined = txn_dir / area / path.name
        quarantined.parent.mkdir(parents=True)
        os.replace(path, quarantined)
    main.ensure_storage(expire_staged=False)
    assert not image_path.exists()
    assert not staged_file.exists()
    assert not csv_file.exists()
    assert not txn_dir.exists()

