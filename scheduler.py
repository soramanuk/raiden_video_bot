"""
scheduler.py — Raiden Auto Video Maker: Core Scheduler

Jadwalkan pembuatan & upload video otomatis 3x sehari tanpa interaksi user.

Jadwal default (WIB / Asia/Jakarta):
  🌅 05:00 — slot pagi
  ☀️ 12:00 — slot siang
  🌙 19:00 — slot malam

FIX v2 (2026-05):
  ✅ _generate_script prompt → English only (was Indonesian)
  ✅ voice fallback → "en-US-AndrewNeural" (was "id-ID-ArdiNeural")
  ✅ duration per slide → 12s default (was 10s) to avoid TTS cutoff

ENV VARS:
  SCHEDULE_PAGI    = "5"   (jam, default 5  → 05:00 WIB)
  SCHEDULE_SIANG   = "12"  (jam, default 12 → 12:00 WIB)
  SCHEDULE_MALAM   = "19"  (jam, default 19 → 19:00 WIB)
  DEFAULT_MODEL    = "gemini-2-flash"
  UPLOAD_TARGET    = "telegram" | "youtube" | "both" | "none"
  TIMEZONE         = "Asia/Jakarta"
  SCHEDULER_ENABLED= "true"
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
            logger.warning(f"[{slot.upper()}] ⚠️ Pipeline sudah berjalan — skip.")
            return
        _active_slots.add(slot)

    start_time = time.time()
    topic = None
    try:
        model = model_override or _get_default_model()
        logger.info(f"[{slot.upper()}] ▶ Pipeline mulai (model: {model})...")

        topic = await topic_engine.get_topic(slot)
        logger.info(f"[{slot.upper()}] Topik: {topic['title']}")

        # Step 2: Generate script
        script_data = await _call_with_retry(
            label=f"[{slot.upper()}] generate-script",
            coro_fn=lambda: _generate_script(topic, model, slot),
            retries=2,
            delay=5,
        )
        slides     = script_data["slides"]
        model_used = script_data.get("model_used", model)
        logger.info(f"[{slot.upper()}] Script OK — {len(slides)} slide, model: {model_used}")

        for i, slide in enumerate(slides, 1):
            logger.info(f"[{slot.upper()}] SLIDE {i}: {slide.get('script', '')[:200]}")

        ratio = topic.get("ratio", "16:9")
        width, height = {"16:9": (1280, 720), "9:16": (720, 1280), "1:1": (720, 720)}.get(ratio, (1280, 720))

        # Step 3: Render video
        logger.info(f"[{slot.upper()}] Rendering {width}x{height}...")
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
        logger.info(f"[{slot.upper()}] ✅ Render selesai {duration:.0f}s — {video_url}")

        # Step 5: Upload
        upload_results = await uploader.upload(
            video_url      = video_url,
            title          = topic["title"],
            tags           = topic.get("tags", []),
            slot           = slot,
            description    = topic.get("topic", ""),
            thumbnail_path = thumbnail_path,
        )
        logger.info(f"[{slot.upper()}] Upload: {upload_results}")

        # Step 6: Notifikasi
        try:
            await notifier.notify_success(
                slot=slot, title=topic["title"],
                video_url=video_url, model_used=model_used,
                duration_seconds=duration,
            )
            for res in upload_results:
                if res.get("success") and res.get("platform") != "none":
                    await notifier.notify_upload_success(
                        slot=slot, title=topic["title"],
                        platform=res["platform"], url=res.get("url", ""),
                    )
        except Exception as notif_err:
            logger.error(f"[{slot.upper()}] Notifikasi gagal (non-fatal): {notif_err}")

        logger.info(f"[{slot.upper()}] 🎉 Pipeline selesai! {duration:.0f}s")

    except Exception as e:
        duration = time.time() - start_time
        title = topic["title"] if topic else f"Slot {slot}"
        logger.error(f"[{slot.upper()}] ❌ Gagal {duration:.0f}s: {e}", exc_info=True)
        try:
            await notifier.notify_error(slot=slot, title=title, error_message=str(e))
        except Exception as notif_err:
            logger.error(f"[{slot.upper()}] notify_error gagal: {notif_err}")
    finally:
        with _active_slots_lock:
            _active_slots.discard(slot)


# ─── Step helpers ─────────────────────────────────────────────────────────────

async def _generate_script(topic: dict, model: str, slot: str) -> dict:
    """
    Generate script via AI — ENGLISH ONLY.

    FIX v2: prompt sekarang force English narration + duration minimum 12s
    agar TTS tidak terpotong di ujung kalimat.
    """
    import json as _json
    from ai_client import call_ai, clean_json, AI_MODELS

    num_slides = topic.get("num_slides", 6)

    prompt = (
        f'You are a professional educational video scriptwriter.\n\n'
        f'Create a script for a video titled: "{topic["title"]}"\n'
        f'Main topic: {topic["topic"]}\n\n'
        f'STRICT REQUIREMENTS:\n'
        f'- Create exactly {num_slides} slides\n'
        f'- Slide 1: Engaging opening — greet viewers, introduce the topic (2-3 complete sentences)\n'
        f'- Slides 2 to {num_slides - 1}: Content — each slide covers 1 specific point with a clear, '
        f'concrete explanation (3-4 complete sentences per slide)\n'
        f'- Slide {num_slides}: Closing — brief summary + call to action (2-3 complete sentences)\n'
        f'- EVERY script MUST contain complete, natural-sounding sentences, minimum 40 words per slide\n'
        f'- Language: ENGLISH ONLY — do NOT use any other language\n'
        f'- Write in narrative paragraph form — NO bullet points or numbering inside the script\n'
        f'- Tone: educational, engaging, enthusiastic\n'
        f'- image_prompt: detailed visual description in English relevant to the slide content, '
        f'{topic.get("style", "cinematic")} style\n'
        f'- duration: estimated reading time in seconds (minimum 12, maximum 18)\n\n'
        f'Respond ONLY in JSON (no markdown/backticks/comments), format:\n'
        f'{{"slides": [{{"script": "full English narration text", '
        f'"image_prompt": "visual description in english", "duration": 12}}]}}'
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

    # Enforce minimum duration per slide — cegah TTS terpotong
    for slide in data["slides"]:
        if isinstance(slide.get("duration"), (int, float)):
            slide["duration"] = max(slide["duration"], 12)
        else:
            slide["duration"] = 12

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
            # FIX v2: voice fallback ke English — gTTS pakai lang="en"
            "voice":  topic.get("voice", "en-US-AndrewNeural"),
            "style":  topic.get("style", "cinematic"),
            "width":  width,
            "height": height,
        })
        r.raise_for_status()
        return r.json()["job_id"]


async def _call_with_retry(label: str, coro_fn, retries: int = 2, delay: float = 5):
    last_exc = None
    for attempt in range(1, retries + 2):
        try:
            return await coro_fn()
        except Exception as exc:
            last_exc = exc
            if attempt <= retries:
                logger.warning(
                    f"{label}: percobaan {attempt}/{retries + 1} gagal ({exc}). "
                    f"Retry dalam {delay}s..."
                )
                await asyncio.sleep(delay)
            else:
                logger.error(f"{label}: semua {retries + 1} percobaan gagal. Error: {exc}")
    raise last_exc


async def _poll_job_db(job_id: str, slot: str, timeout: int = 600) -> tuple[str, str]:
    """Poll status render langsung dari SQLite — tanpa HTTP."""
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
                    f"Job {job_id} berstatus 'done' tapi video_url kosong — cek log do_render."
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
        args=["pagi"], id="job_pagi",
        name=f"Auto Video Pagi ({HOUR_PAGI:02d}:00 {TIMEZONE})",
        replace_existing=True, misfire_grace_time=300,
    )
    sched.add_job(
        run_full_pipeline,
        trigger=CronTrigger(hour=HOUR_SIANG, minute=0, timezone=TIMEZONE),
        args=["siang"], id="job_siang",
        name=f"Auto Video Siang ({HOUR_SIANG:02d}:00 {TIMEZONE})",
        replace_existing=True, misfire_grace_time=300,
    )
    sched.add_job(
        run_full_pipeline,
        trigger=CronTrigger(hour=HOUR_MALAM, minute=0, timezone=TIMEZONE),
        args=["malam"], id="job_malam",
        name=f"Auto Video Malam ({HOUR_MALAM:02d}:00 {TIMEZONE})",
        replace_existing=True, misfire_grace_time=300,
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
            "id": job.id,
            "name": job.name,
            "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
        })
    return {"enabled": _scheduler.running, "timezone": TIMEZONE, "jobs": jobs}
