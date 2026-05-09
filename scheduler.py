"""
scheduler.py — Raiden Auto Video Maker: Core Scheduler

Jadwal default (WIB / Asia/Jakarta):
  🌅 05:00 — slot pagi
  ☀️ 12:00 — slot siang
  🌙 19:00 — slot malam

ENV VARS:
  SCHEDULE_PAGI    = "5"   (jam, default 5 → 05:00 WIB)
  SCHEDULE_SIANG   = "12"  (jam, default 12 → 12:00 WIB)
  SCHEDULE_MALAM   = "19"  (jam, default 19 → 19:00 WIB)
  DEFAULT_MODEL    = "gemini-2-flash"
  UPLOAD_TARGET    = "telegram" | "youtube" | "both" | "none"
  TIMEZONE         = "Asia/Jakarta"
  SCHEDULER_ENABLED= "true"
"""

import os
import time
import asyncio
import logging
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
HOUR_PAGI         = int(os.getenv("SCHEDULE_PAGI",  "5"))
HOUR_SIANG        = int(os.getenv("SCHEDULE_SIANG", "12"))
HOUR_MALAM        = int(os.getenv("SCHEDULE_MALAM", "19"))

def _get_default_model() -> str:
    return os.getenv("DEFAULT_MODEL", "gemini-2-flash")

_scheduler: AsyncIOScheduler | None = None

_active_slots: set = set()
_active_slots_lock = _threading.Lock()

# ─── Core Pipeline ────────────────────────────────────────────────────────────

async def run_full_pipeline(slot: str, model_override: str | None = None):
    with _active_slots_lock:
        if slot in _active_slots:
            logger.warning(f"[{slot.upper()}] ⚠️ Pipeline sudah berjalan — skip (duplicate lock).")
            return
        _active_slots.add(slot)

    start_time = time.time()
    topic = None

    try:
        model = model_override or _get_default_model()
        logger.info(f"[{slot.upper()}] ▶ Memulai pipeline otomatis (model: {model})...")

        topic = await topic_engine.get_topic(slot)
        logger.info(f"[{slot.upper()}] Topik: {topic['title']}")

        # Step 2: Generate script
        # FIX #3: retries=3, delay=30 untuk handle 429 Gemini dengan jeda lebih panjang
        script_data = await _call_with_retry(
            label=f"[{slot.upper()}] generate-script",
            coro_fn=lambda: _generate_script(topic, model, slot),
            retries=3,
            delay=30,
        )
        slides     = script_data["slides"]
        model_used = script_data.get("model_used", model)
        logger.info(f"[{slot.upper()}] Script OK — {len(slides)} slide, model: {model_used}")

        for i, slide in enumerate(slides, 1):
            logger.info(f"[{slot.upper()}] SLIDE {i}: {slide.get('script', '')[:200]}")

        ratio = topic.get("ratio", "16:9")
        width, height = {
            "16:9": (1280, 720),
            "9:16": (720, 1280),
            "1:1":  (720, 720),
        }.get(ratio, (1280, 720))

        # Step 3: Render video
        logger.info(f"[{slot.upper()}] Rendering video {width}x{height}...")
        job_id = await _call_with_retry(
            label=f"[{slot.upper()}] render-video",
            coro_fn=lambda: _start_render(topic, slides, width, height, slot),
            retries=2,
            delay=10,
        )
        logger.info(f"[{slot.upper()}] Render job: {job_id}")

        # Step 4: Poll DB
        video_url, thumbnail_path = await _poll_job_db(job_id, slot)
        duration = time.time() - start_time
        logger.info(f"[{slot.upper()}] ✅ Render selesai dalam {duration:.0f}s — {video_url}")

        # Step 5: Upload
        upload_results = await uploader.upload(
            video_url      = video_url,
            title          = topic["title"],
            tags           = topic.get("tags", []),
            slot           = slot,
            description    = topic.get("topic", ""),
            thumbnail_path = thumbnail_path,
        )
        logger.info(f"[{slot.upper()}] Upload results: {upload_results}")

        # Step 6: Notifikasi sukses
        try:
            await notifier.notify_success(
                slot            = slot,
                title           = topic["title"],
                video_url       = video_url,
                model_used      = model_used,
                duration_seconds= duration,
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
        try:
            await notifier.notify_error(slot=slot, title=title, error_message=str(e))
        except Exception as notif_err:
            logger.error(
                f"[{slot.upper()}] notify_error juga gagal: {notif_err} "
                f"(root cause pipeline: {e})"
            )

    finally:
        with _active_slots_lock:
            _active_slots.discard(slot)


# ─── Step helpers ─────────────────────────────────────────────────────────────

async def _generate_script(topic: dict, model: str, slot: str) -> dict:
    import json as _json
    from ai_client import call_ai, clean_json, AI_MODELS

    num_slides = topic.get("num_slides", 6)
    prompt = (
        f'Kamu adalah scriptwriter video edukasi berbahasa Indonesia yang profesional.\n\n'
        f'Buat script narasi untuk video berjudul: "{topic["title"]}"\n'
        f'Topik utama: {topic["topic"]}\n\n'
        f'KETENTUAN WAJIB:\n'
        f'- Buat tepat {num_slides} slide\n'
        f'- Slide 1: Pembukaan menarik — sapa penonton, perkenalkan topik (2-3 kalimat)\n'
        f'- Slide 2 s/d {num_slides-1}: Isi konten — setiap slide membahas 1 poin spesifik (3-4 kalimat per slide)\n'
        f'- Slide {num_slides}: Penutup — rangkuman singkat + ajakan action (2-3 kalimat)\n'
        f'- Setiap script HARUS berisi kalimat lengkap yang natural diucapkan, minimal 30 kata per slide\n'
        f'- Gunakan bahasa Indonesia yang natural, mudah dipahami, dan mengalir enak didengar\n'
        f'- JANGAN menggunakan bullet point atau numbering — tulis dalam bentuk paragraf narasi\n'
        f'- image_prompt: deskripsi visual dalam bahasa Inggris, cinematic style\n'
        f'- duration: perkiraan durasi baca dalam detik (minimal 8, maksimal 15)\n\n'
        f'Respond HANYA dalam JSON (tanpa markdown/backtick/komentar):\n'
        f'{{"slides": [{{"script": "teks narasi lengkap", "image_prompt": "visual description in english", "duration": 10}}]}}'
    )

    raw = await call_ai(model, prompt)

    try:
        data = _json.loads(clean_json(raw))
    except (_json.JSONDecodeError, ValueError) as exc:
        preview = raw[:200].replace("\n", " ")
        raise RuntimeError(
            f"generate-script: JSON parse error ({exc}). Preview: {preview!r}"
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
    FIX #3: Generic retry wrapper dengan exponential backoff untuk 429.
    Jika exception mengandung '429', delay dikalikan 2 tiap percobaan.
    """
    last_exc  = None
    cur_delay = delay

    for attempt in range(1, retries + 2):
        try:
            return await coro_fn()
        except Exception as exc:
            last_exc = exc
            is_rate_limit = "429" in str(exc)

            if attempt <= retries:
                wait = cur_delay * (2 ** (attempt - 1)) if is_rate_limit else cur_delay
                logger.warning(
                    f"{label}: percobaan {attempt}/{retries + 1} gagal "
                    f"({exc}). Retry dalam {wait:.0f}s..."
                )
                await asyncio.sleep(wait)
            else:
                logger.error(
                    f"{label}: semua {retries + 1} percobaan gagal. "
                    f"Error terakhir: {exc}"
                )

    raise last_exc


# ─── DB Poller ────────────────────────────────────────────────────────────────

async def _poll_job_db(job_id: str, slot: str, timeout: int = 600) -> tuple[str, str]:
    from job_store import get_job

    deadline = time.time() + timeout
    interval = 5

    while time.time() < deadline:
        job_record = get_job(job_id)

        if job_record is None:
            raise RuntimeError(f"Job {job_id} tidak ditemukan di DB")

        status = job_record.get("status")

        if status == "done":
            video_url = job_record.get("video_url") or ""
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
            raise RuntimeError(f"Render error: {job_record.get('message', 'unknown')}")

        elif status in ("queued", "processing"):
            logger.debug(f"[{slot.upper()}] Job {job_id}: {status}...")
            await asyncio.sleep(interval)

        else:
            raise RuntimeError(f"Status tidak dikenal: {status}")

    raise TimeoutError(f"Render job {job_id} timeout setelah {timeout}s")


# ─── Scheduler Setup ──────────────────────────────────────────────────────────

def create_scheduler() -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone=TIMEZONE)

    sched.add_job(
        run_full_pipeline,
        trigger=CronTrigger(hour=HOUR_PAGI, minute=0, timezone=TIMEZONE),
        args=["pagi"],
        id="job_pagi",
        name=f"Auto Video Pagi ({HOUR_PAGI:02d}:00 {TIMEZONE})",
        replace_existing=True,
        misfire_grace_time=300,
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
    global _scheduler
    if not SCHEDULER_ENABLED:
        logger.info("Scheduler dinonaktifkan (SCHEDULER_ENABLED=false)")
        return

    _scheduler = create_scheduler()
    _scheduler.start()
    jobs = _scheduler.get_jobs()
    logger.info(f"✅ Scheduler aktif dengan {len(jobs)} job:")
    for job in jobs:
        logger.info(f"  • {job.name} — next: {job.next_run_time}")
    return _scheduler


def stop_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler dihentikan")


def get_scheduler_status() -> dict:
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
