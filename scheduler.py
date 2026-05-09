"""
scheduler.py — Raiden Auto Video Maker: Core Scheduler
Jadwalkan pembuatan & upload video otomatis 3x sehari tanpa interaksi user.

Jadwal default (WIB / Asia/Jakarta):
  🌅 05:00 — slot pagi
  ☀️ 12:00 — slot siang
  🌙 19:00 — slot malam

Dijalankan otomatis saat server FastAPI start (lihat main.py).

ENV VARS:
  SCHEDULE_PAGI    = "5"   (jam, default 5 → 05:00 WIB)
  SCHEDULE_SIANG   = "12"  (jam, default 12 → 12:00 WIB)
  SCHEDULE_MALAM   = "19"  (jam, default 19 → 19:00 WIB)
  DEFAULT_MODEL    = "gemini-2-flash"   (model hemat untuk auto-run)
  UPLOAD_TARGET    = "telegram" | "youtube" | "both" | "none"
  TIMEZONE         = "Asia/Jakarta"
  SCHEDULER_ENABLED= "true" (set "false" untuk nonaktifkan)
"""

import os
import asyncio
import logging
import time
import threading as _threading
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import notifier
import uploader
import topics as topic_engine

logger = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────

TIMEZONE          = os.getenv("TIMEZONE", "Asia/Jakarta")
INTERNAL_API      = os.getenv("INTERNAL_API_BASE", "http://localhost:8000")
SCHEDULER_ENABLED = os.getenv("SCHEDULER_ENABLED", "true").lower() == "true"

HOUR_PAGI   = int(os.getenv("SCHEDULE_PAGI",  "5"))
HOUR_SIANG  = int(os.getenv("SCHEDULE_SIANG", "12"))
HOUR_MALAM  = int(os.getenv("SCHEDULE_MALAM", "19"))

# FIX #1: Jangan cache DEFAULT_MODEL di module-level.
# Baca via fungsi helper agar override os.environ["DEFAULT_MODEL"] dari /auto-run
# selalu terbaca fresh — termasuk oleh APScheduler yang memanggil run_full_pipeline langsung.
def _get_default_model() -> str:
    return os.getenv("DEFAULT_MODEL", "gemini-2-flash")

# Singleton scheduler
_scheduler: AsyncIOScheduler | None = None

# FIX #2: Pipeline lock dibuat di sini (module-level) agar melindungi baik
# /auto-run maupun APScheduler yang memanggil run_full_pipeline() langsung.
# main.py tidak perlu _active_slots-nya sendiri lagi untuk perlindungan scheduler.
_active_slots: set = set()
_active_slots_lock = _threading.Lock()


# ─── Core Pipeline ────────────────────────────────────────────────────────────

async def run_full_pipeline(slot: str, model_override: str | None = None):
    """
    Pipeline otomatis penuh untuk satu slot.
    model_override: jika diisi (dari /auto-run), pakai model ini alih-alih DEFAULT_MODEL.
    Tidak ada mutasi os.environ — aman untuk concurrent slot.
    """
    # ── Duplicate-run guard ──────────────────────────────────────────────────
    with _active_slots_lock:
        if slot in _active_slots:
            logger.warning(
                f"[{slot.upper()}] ⚠️  Pipeline sudah berjalan — skip (duplicate lock)."
            )
            return
        _active_slots.add(slot)

    start_time = time.time()
    topic = None

    try:
        # Step 1: Pilih topik
        model = model_override or _get_default_model()
        logger.info(f"[{slot.upper()}] ▶ Memulai pipeline otomatis (model: {model})...")
        topic = await topic_engine.get_topic(slot)
        logger.info(f"[{slot.upper()}] Topik: {topic['title']}")

        # ── Step 2: Generate script (dengan retry) ───────────────────────────
        # FIX #7: retry untuk step ini (contoh pola dari uploader.py)
        script_data = await _call_with_retry(
            label=f"[{slot.upper()}] generate-script",
            coro_fn=lambda: _generate_script(topic, model, slot),
            retries=2,
            delay=5,
        )

        slides     = script_data["slides"]
        model_used = script_data.get("model_used", model)
        logger.info(f"[{slot.upper()}] Script OK — {len(slides)} slide, model: {model_used}")

        # Tentukan dimensi berdasarkan ratio
        ratio  = topic.get("ratio", "16:9")
        width, height = {
            "16:9": (1280, 720),
            "9:16": (720, 1280),
            "1:1":  (720, 720),
        }.get(ratio, (1280, 720))

        # ── Notifikasi mulai proses ──────────────────────────────────────────
        try:
            await notifier.notify_start(
                slot       = slot,
                title      = topic["title"],
                topic      = topic.get("topic", topic["title"]),
                model      = model_used,
                num_slides = len(slides),
                ratio      = ratio,
            )
        except Exception as notif_err:
            logger.warning(f"[{slot.upper()}] notify_start gagal (non-fatal): {notif_err}")

        # ── Step 3: Render video (dengan retry) ─────────────────────────────
        # FIX #7: retry untuk step ini juga
        logger.info(f"[{slot.upper()}] Rendering video {width}x{height}...")
        job_id = await _call_with_retry(
            label=f"[{slot.upper()}] render-video",
            coro_fn=lambda: _start_render(topic, slides, width, height, slot),
            retries=2,
            delay=10,
        )
        logger.info(f"[{slot.upper()}] Render job: {job_id}")

        # ── Step 4: Poll langsung dari DB ────────────────────────────────────
        # FIX #5: tidak lagi buka 120 koneksi HTTP — query DB in-process
        video_url, thumbnail_path = await _poll_job_db(job_id, slot)
        duration = time.time() - start_time
        logger.info(f"[{slot.upper()}] ✅ Render selesai dalam {duration:.0f}s — {video_url}")

        # ── Baca durasi video ─────────────────────────────────────────────────
        video_duration_sec = 0
        try:
            import imageio_ffmpeg as _iio_ffmpeg
            import subprocess, json as _json
            video_filename = video_url.split("/")[-1]
            video_filepath = str(Path(os.getenv("OUTPUT_DIR", "/app/outputs")) / video_filename)
            _result = subprocess.run([
                _iio_ffmpeg.get_ffmpeg_exe(),
                "-v", "quiet", "-print_format", "json", "-show_format",
                "-i", video_filepath,
                "-f", "null", "-",
            ], capture_output=True, text=True, timeout=15)
            # ffmpeg -i outputs duration to stderr
            import re as _re
            _match = _re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", _result.stderr)
            if _match:
                h, m, s = int(_match.group(1)), int(_match.group(2)), float(_match.group(3))
                video_duration_sec = h * 3600 + m * 60 + s
        except Exception as _e:
            logger.warning(f"[{slot.upper()}] Gagal baca durasi video: {_e}")

        # ── Step 5: Upload ke platform ───────────────────────────────────────
        upload_results = await uploader.upload(
            video_url      = video_url,
            title          = topic["title"],
            tags           = topic.get("tags", []),
            slot           = slot,
            description    = topic.get("topic", ""),
            thumbnail_path = thumbnail_path,
        )
        logger.info(f"[{slot.upper()}] Upload results: {upload_results}")

        # ── Step 6: Notifikasi sukses ────────────────────────────────────────
        # FIX #8: notifikasi error dibungkus try-except sendiri agar traceback
        # asli tidak tertimpa jika Telegram down
        try:
            await notifier.notify_success(
                slot             = slot,
                title            = topic["title"],
                video_url        = video_url,
                model_used       = model_used,
                duration_seconds = duration,
                video_duration   = video_duration_sec,
            )
            for res in upload_results:
                if res.get("success") and res.get("platform") != "none":
                    await notifier.notify_upload_success(
                        slot     = slot,
                        title    = topic["title"],
                        platform = res["platform"],
                        url      = res.get("url", ""),
                    )
        except Exception as notif_err:
            logger.error(f"[{slot.upper()}] Notifikasi sukses gagal (non-fatal): {notif_err}")

        logger.info(f"[{slot.upper()}] 🎉 Pipeline selesai! Total: {duration:.0f}s")

    except Exception as e:
        duration = time.time() - start_time
        title    = topic["title"] if topic else f"Slot {slot}"
        logger.error(
            f"[{slot.upper()}] ❌ Pipeline gagal setelah {duration:.0f}s: {e}",
            exc_info=True,
        )
        # FIX #8: bungkus notify_error dalam try-except sendiri
        # agar exception Telegram tidak menimpa traceback asli di log
        try:
            await notifier.notify_error(slot=slot, title=title, error_message=str(e))
        except Exception as notif_err:
            logger.error(
                f"[{slot.upper()}] notify_error juga gagal: {notif_err} "
                f"(root cause pipeline: {e})"
            )

    finally:
        # Selalu lepas lock, bahkan jika pipeline crash
        with _active_slots_lock:
            _active_slots.discard(slot)


# ─── Step helpers ─────────────────────────────────────────────────────────────

async def _generate_script(topic: dict, model: str, slot: str) -> dict:
    """
    Panggil /generate-script dan parse JSON-nya.
    FIX #4: tangkap JSONDecodeError + non-JSON response dari AI (rate-limit, markdown).
    """
    import json as _json
    from ai_client import call_ai, clean_json, AI_MODELS

    prompt = (
        f'Kamu adalah scriptwriter video profesional. Buat script untuk video berjudul '
        f'"{topic["title"]}" tentang topik: {topic["topic"]}\n\n'
        f'Buat tepat {topic.get("num_slides", 6)} slide. Respond HANYA dalam JSON '
        f'(tanpa markdown/backtick), format:\n'
        f'{{"slides": [{{"script": "...", "image_prompt": "...", "duration": 5}}]}}'
    )

    raw = await call_ai(model, prompt)

    try:
        data = _json.loads(clean_json(raw))
    except (_json.JSONDecodeError, ValueError) as exc:
        # AI mungkin return rate-limit message atau markdown panjang
        preview = raw[:200].replace("\n", " ")
        raise RuntimeError(
            f"generate-script: JSON parse error ({exc}). "
            f"Raw response preview: {preview!r}"
        )

    if "slides" not in data or not isinstance(data["slides"], list):
        raise RuntimeError(
            f"generate-script: response tidak punya key 'slides'. Keys: {list(data.keys())}"
        )

    cfg = AI_MODELS.get(model, {})
    data["model_used"]    = cfg.get("label", model)
    data["provider_used"] = cfg.get("provider_label", "")
    return data


async def _start_render(topic: dict, slides: list, width: int, height: int, slot: str) -> str:
    """Kirim request render ke endpoint in-process dan return job_id."""
    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{INTERNAL_API}/render-video", json={
            "title":  topic["title"],
            "slides": slides,
            "voice":  topic.get("voice", "id-ID-ArdiNeural"),
            "style":  topic.get("style", "cinematic"),
            "width":  width,
            "height": height,
        })
        r.raise_for_status()
        return r.json()["job_id"]


async def _call_with_retry(label: str, coro_fn, retries: int = 2, delay: float = 5):
    """
    FIX #7: wrapper retry generic.
    Coba coro_fn() hingga (retries+1) kali dengan jeda delay detik antar percobaan.
    Raise exception terakhir jika semua percobaan gagal.
    """
    last_exc = None
    for attempt in range(1, retries + 2):  # attempt 1..retries+1
        try:
            return await coro_fn()
        except Exception as exc:
            last_exc = exc
            if attempt <= retries:
                logger.warning(
                    f"{label}: percobaan {attempt}/{retries + 1} gagal "
                    f"({exc}). Retry dalam {delay}s..."
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    f"{label}: semua {retries + 1} percobaan gagal. "
                    f"Error terakhir: {exc}"
                )
    raise last_exc


# ─── DB Poller (tanpa HTTP) ───────────────────────────────────────────────────

async def _poll_job_db(job_id: str, slot: str, timeout: int = 600) -> tuple[str, str]:
    """
    FIX #5: Poll status render langsung dari SQLite — tanpa HTTP round-trip.
    Hemat 120 koneksi per pipeline (polling 5 detik × 10 menit).
    Return tuple (video_url, thumbnail_path).
    """
    import json as _json
    from job_store import get_job

    deadline = time.time() + timeout
    interval = 5  # cek setiap 5 detik

    while time.time() < deadline:
        job_record = get_job(job_id)

        if job_record is None:
            raise RuntimeError(f"Job {job_id} tidak ditemukan di DB")

        status = job_record.get("status")

        if status == "done":
            video_url      = job_record.get("video_url") or ""
            # FIX G: jika video_url kosong, render selesai tapi URL tidak tersimpan —
            # ini bug data corruption, raise agar pipeline tidak upload ke platform
            # dengan URL kosong (Telegram/YouTube akan return API error yang tidak jelas).
            if not video_url:
                raise RuntimeError(
                    f"Job {job_id} berstatus 'done' tapi video_url kosong — "
                    f"kemungkinan error saat set_job_status(). Cek log do_render."
                )
            thumbnail_path = ""
            extra = job_record.get("extra") or {}
            if isinstance(extra, dict):
                thumbnail_path = extra.get("thumbnail_path", "")
            return video_url, thumbnail_path

        elif status == "error":
            raise RuntimeError(
                f"Render error: {job_record.get('message', 'unknown')}"
            )

        elif status in ("queued", "processing"):
            logger.debug(f"[{slot.upper()}] Job {job_id}: {status}...")
            await asyncio.sleep(interval)

        else:
            raise RuntimeError(f"Status tidak dikenal: {status}")

    raise TimeoutError(f"Render job {job_id} timeout setelah {timeout}s")


# ─── Scheduler Setup ──────────────────────────────────────────────────────────

def create_scheduler() -> AsyncIOScheduler:
    """Buat dan konfigurasi APScheduler dengan 3 jadwal harian."""
    sched = AsyncIOScheduler(timezone=TIMEZONE)

    sched.add_job(
        run_full_pipeline,
        trigger=CronTrigger(hour=HOUR_PAGI, minute=0, timezone=TIMEZONE),
        args=["pagi"],
        id="job_pagi",
        name=f"Auto Video Pagi ({HOUR_PAGI:02d}:00 {TIMEZONE})",
        replace_existing=True,
        misfire_grace_time=300,  # toleransi 5 menit jika server restart
    )

    sched.add_job(
        run_full_pipeline,
        trigger=CronTrigger(hour=HOUR_SIANG, minute=0, timezone=TIMEZONE),
        args=["siang"],
        id="job_siang",
        name=f"Auto Video Siang ({HOUR_SIANG:02d}:00 {TIMEZONE})",
        replace_existing=True,
        misfire_grace_time=300,
    )

    sched.add_job(
        run_full_pipeline,
        trigger=CronTrigger(hour=HOUR_MALAM, minute=0, timezone=TIMEZONE),
        args=["malam"],
        id="job_malam",
        name=f"Auto Video Malam ({HOUR_MALAM:02d}:00 {TIMEZONE})",
        replace_existing=True,
        misfire_grace_time=300,
    )

    return sched


def start_scheduler():
    """Start scheduler — dipanggil dari main.py saat FastAPI startup."""
    global _scheduler

    if not SCHEDULER_ENABLED:
        logger.info("Scheduler dinonaktifkan (SCHEDULER_ENABLED=false)")
        return

    _scheduler = create_scheduler()
    _scheduler.start()

    jobs = _scheduler.get_jobs()
    logger.info(f"✅ Scheduler aktif dengan {len(jobs)} job:")
    for job in jobs:
        next_run = job.next_run_time
        logger.info(f"   • {job.name} — next: {next_run}")

    return _scheduler


def stop_scheduler():
    """Stop scheduler — dipanggil saat FastAPI shutdown."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler dihentikan")


def get_scheduler_status() -> dict:
    """Return status semua job untuk endpoint /scheduler-status."""
    if not _scheduler:
        return {"enabled": False, "jobs": []}

    jobs = []
    for job in _scheduler.get_jobs():
        jobs.append({
            "id":       job.id,
            "name":     job.name,
            "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
        })

    return {
        "enabled":  _scheduler.running,
        "timezone": TIMEZONE,
        "jobs":     jobs,
    }
