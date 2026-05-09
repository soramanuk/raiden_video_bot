"""
job_store.py — Persistent Job State via SQLite
Menggantikan JOBS dict in-memory agar render job tidak hilang saat restart.

Fitur:
  • Simpan status, video_url, error message, timestamps ke SQLite
  • Auto-migrate schema (tambah kolom jika tabel sudah ada)
  • Helper: get, set, list jobs
  • Cleanup otomatis job lama (ikut VIDEO_MAX_AGE_HOURS)

DB file: outputs/jobs.db (sama folder dengan video, supaya Railway persistent volume 1 path)
"""

import os
import sqlite3
import json
import logging
import time
from pathlib import Path
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger("job_store")

# DB disimpan di folder outputs/ supaya persistent volume Railway cukup 1 path
DB_PATH = Path(os.getenv("JOB_DB_PATH", "outputs/jobs.db"))


# ─── Schema ───────────────────────────────────────────────────────────────────

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,
    status       TEXT NOT NULL DEFAULT 'queued',
    video_url    TEXT,
    message      TEXT,
    slot         TEXT,
    title        TEXT,
    model_key    TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    extra_json   TEXT
);
"""

# Kolom yang boleh ada di versi lama tapi belum ada — auto-added saat init
_OPTIONAL_COLUMNS = [
    ("slot",       "TEXT"),
    ("title",      "TEXT"),
    ("model_key",  "TEXT"),
    ("extra_json", "TEXT"),
]


# ─── Connection helper ────────────────────────────────────────────────────────

@contextmanager
def _conn():
    """Context manager: buka koneksi, commit jika sukses, rollback jika error."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ─── Init ─────────────────────────────────────────────────────────────────────

def init_db():
    """Buat tabel jika belum ada; tambah kolom baru jika schema berubah."""
    with _conn() as con:
        # WAL mode: izinkan concurrent reads + writes tanpa blocking
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(_CREATE_TABLE)

        # Auto-migrate: tambah kolom opsional jika belum ada
        existing = {row[1] for row in con.execute("PRAGMA table_info(jobs)")}
        for col_name, col_type in _OPTIONAL_COLUMNS:
            if col_name not in existing:
                con.execute(f"ALTER TABLE jobs ADD COLUMN {col_name} {col_type}")
                logger.info(f"Schema migrated: tambah kolom '{col_name}'")

    logger.info(f"Job store siap: {DB_PATH}")


# ─── CRUD ─────────────────────────────────────────────────────────────────────

def create_job(
    job_id: str,
    *,
    slot: Optional[str] = None,
    title: Optional[str] = None,
    model_key: Optional[str] = None,
) -> dict:
    """Daftarkan job baru dengan status 'queued'."""
    now = time.time()
    with _conn() as con:
        con.execute(
            """
            INSERT OR REPLACE INTO jobs
                (job_id, status, video_url, message, slot, title, model_key, created_at, updated_at)
            VALUES (?, 'queued', NULL, NULL, ?, ?, ?, ?, ?)
            """,
            (job_id, slot, title, model_key, now, now),
        )
    return get_job(job_id)


def get_job(job_id: str) -> Optional[dict]:
    """Ambil satu job sebagai dict. Return None jika tidak ada."""
    with _conn() as con:
        row = con.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def update_job(job_id: str, **fields) -> Optional[dict]:
    """
    Update field job. Field yang boleh diupdate:
      status, video_url, message, slot, title, model_key, extra_json
    updated_at selalu di-update otomatis.
    """
    allowed = {"status", "video_url", "message", "slot", "title", "model_key", "extra_json"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_job(job_id)

    updates["updated_at"] = time.time()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [job_id]

    with _conn() as con:
        con.execute(f"UPDATE jobs SET {set_clause} WHERE job_id = ?", values)
    return get_job(job_id)


def set_job_status(job_id: str, status: str, **extra) -> Optional[dict]:
    """Shortcut: update status + field opsional lainnya sekaligus."""
    return update_job(job_id, status=status, **extra)


def list_jobs(limit: int = 50, status_filter: Optional[str] = None) -> list[dict]:
    """Ambil daftar job terbaru (desc created_at). Bisa filter per status."""
    with _conn() as con:
        if status_filter:
            rows = con.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status_filter, limit),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [_row_to_dict(r) for r in rows]


def delete_old_jobs(max_age_hours: float = 48.0) -> int:
    """Hapus job yang lebih tua dari max_age_hours. Return jumlah terhapus."""
    cutoff = time.time() - (max_age_hours * 3600)
    with _conn() as con:
        cur = con.execute("DELETE FROM jobs WHERE created_at < ?", (cutoff,))
    deleted = cur.rowcount
    if deleted:
        logger.info(f"Cleanup: hapus {deleted} job lama dari DB")
    return deleted


# ─── Compat helper (pengganti JOBS dict lama) ─────────────────────────────────

class JobStore:
    """
    Interface seperti dict untuk backward-compat dengan kode lama yang pakai JOBS[job_id].
    Contoh:
        JOBS = JobStore()
        JOBS[job_id] = {"status": "queued", "video_url": None}
        JOBS[job_id]["status"]          # baca
        JOBS[job_id] = {"status": "done", "video_url": "https://..."}  # tulis ulang
    """

    def __contains__(self, job_id: str) -> bool:
        return get_job(job_id) is not None

    def __getitem__(self, job_id: str) -> dict:
        job = get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def __setitem__(self, job_id: str, value: dict):
        """
        Tulis status job.
        Jika job belum ada → create; jika sudah ada → update.
        value harus dict dengan minimal key 'status'.
        """
        if get_job(job_id) is None:
            create_job(job_id)
        update_job(job_id, **{k: v for k, v in value.items() if k != "job_id"})

    def get(self, job_id: str, default=None):
        job = get_job(job_id)
        return job if job is not None else default

    def count_active(self) -> int:
        """Hitung job yang sedang berjalan (status = 'processing'). Dipakai health check."""
        # FIX #3: gunakan DB_PATH module-level, bukan hardcode "jobs.db"
        with _conn() as con:
            cur = con.execute("SELECT COUNT(*) FROM jobs WHERE status = 'processing'")
            return cur.fetchone()[0]



# ─── Internal helpers ─────────────────────────────────────────────────────────

def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    # Parse extra_json jika ada
    if d.get("extra_json"):
        try:
            d["extra"] = json.loads(d["extra_json"])
        except Exception:
            d["extra"] = {}
    else:
        d["extra"] = {}
    return d
