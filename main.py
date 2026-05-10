"""
Video Content Maker — FastAPI Backend (Multi-Model AI Edition + Auto Scheduler)
Deploy ke Railway: https://railway.app

BARU di versi ini:
  ✅ Scheduler otomatis 3x sehari (05:00 / 12:00 / 19:00 WIB)
  ✅ POST /auto-run         — trigger pipeline penuh 1 klik
  ✅ GET  /scheduler-status — lihat status & jadwal berikutnya
  ✅ Upload otomatis ke Telegram / YouTube
  ✅ Notifikasi Telegram setiap sukses/gagal

Supported AI Providers:
  - Anthropic Claude (claude-sonnet-4-20250514, claude-haiku-4-5-20251001)
  - Groq (llama-4-scout-17b-16e-instruct, llama-4-maverick-17b-128e-instruct, llama3-70b-8192)
  - Google Gemini (gemini-2.0-flash, gemini-1.5-pro)
  - OpenAI (gpt-4.1, gpt-4o-mini)
  - Mistral (mistral-large-latest, open-mixtral-8x7b)
"""

import os, uuid, asyncio, json, tempfile, shutil, re, re
from pathlib import Path
from typing import Optional, Literal
import httpx
from gtts import gTTS
import imageio_ffmpeg as _iio_ffmpeg
FFMPEG_BIN = _iio_ffmpeg.get_ffmpeg_exe()
import uploader
from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ─── Scheduler & helpers (import bersyarat agar dev mode tetap jalan) ─────────
try:
    import scheduler as sched_module
    import notifier
    SCHEDULER_AVAILABLE = True
except ImportError:
    SCHEDULER_AVAILABLE = False

app = FastAPI(title="Video Content Maker API — Auto Scheduler Edition")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

from job_store import JobStore, init_db, delete_old_jobs

# Persistent job state — data survive restart via SQLite
# Interface sama seperti dict lama: JOBS[job_id], JOBS[job_id] = {...}
JOBS = JobStore()
OUTPUT_DIR = Path("outputs")
OUTPUT_DIR.mkdir(exist_ok=True)

app.mount("/videos", StaticFiles(directory="outputs"), name="videos")

# ─── Cleanup Config ───────────────────────────────────────────────────────────
# Semua bisa diatur via env vars di Railway tanpa ubah kode

# Berapa jam file .mp4 boleh disimpan sebelum dihapus (default: 24 jam)
VIDEO_MAX_AGE_HOURS  = int(os.getenv("VIDEO_MAX_AGE_HOURS", "24"))
# Batas maksimum total file .mp4 di folder outputs (default: 10 file)
VIDEO_MAX_FILES      = int(os.getenv("VIDEO_MAX_FILES", "10"))
# Batas maksimum total ukuran folder outputs dalam MB (default: 500 MB)
VIDEO_MAX_SIZE_MB    = int(os.getenv("VIDEO_MAX_SIZE_MB", "500"))
# Interval cleanup berjalan otomatis, dalam menit (default: 30 menit)
CLEANUP_INTERVAL_MIN = int(os.getenv("CLEANUP_INTERVAL_MIN", "30"))


# ─── Cleanup Engine ───────────────────────────────────────────────────────────

import logging
import logging as _logging
_cleanup_logger = _logging.getLogger("cleanup")


def _get_mp4_files() -> list:
    """Return semua .mp4 di OUTPUT_DIR, diurutkan dari yang paling lama."""
    return sorted(OUTPUT_DIR.glob("*.mp4"), key=lambda f: f.stat().st_mtime)


def _delete_mp4_with_thumb(f: Path) -> None:
    """Hapus file .mp4 beserta thumbnail orphan-nya jika ada."""
    thumb = f.with_name(f.stem + "_thumb.jpg")
    f.unlink(missing_ok=True)
    thumb.unlink(missing_ok=True)


def cleanup_by_age() -> int:
    """Hapus file .mp4 (+ thumbnail pasangannya) yang lebih tua dari VIDEO_MAX_AGE_HOURS."""
    import time
    cutoff  = time.time() - (VIDEO_MAX_AGE_HOURS * 3600)
    deleted = 0
    for f in OUTPUT_DIR.glob("*.mp4"):
        if f.stat().st_mtime < cutoff:
            _delete_mp4_with_thumb(f)
            deleted += 1
    return deleted


def cleanup_by_count() -> int:
    """Hapus file .mp4 (+ thumbnail) terlama jika jumlah melebihi VIDEO_MAX_FILES."""
    files   = _get_mp4_files()
    excess  = len(files) - VIDEO_MAX_FILES
    deleted = 0
    for f in files[:max(excess, 0)]:
        _delete_mp4_with_thumb(f)
        deleted += 1
    return deleted


def cleanup_by_size() -> int:
    """Hapus file .mp4 (+ thumbnail) terlama jika total ukuran folder melebihi VIDEO_MAX_SIZE_MB."""
    limit_bytes = VIDEO_MAX_SIZE_MB * 1024 * 1024
    files       = _get_mp4_files()
    total_bytes = sum(f.stat().st_size for f in files)
    deleted     = 0
    for f in files:
        if total_bytes <= limit_bytes:
            break
        total_bytes -= f.stat().st_size
        _delete_mp4_with_thumb(f)
        deleted += 1
    return deleted


def run_cleanup() -> dict:
    """
    Jalankan semua 3 strategi cleanup sekaligus.
    Return ringkasan hasil untuk logging & endpoint /cleanup.
    """
    files_before = list(OUTPUT_DIR.glob("*.mp4"))
    before_count = len(files_before)
    before_mb    = sum(f.stat().st_size for f in files_before) / 1024 / 1024

    by_age   = cleanup_by_age()
    by_count = cleanup_by_count()
    by_size  = cleanup_by_size()

    files_after = list(OUTPUT_DIR.glob("*.mp4"))
    after_count = len(files_after)
    after_mb    = sum(f.stat().st_size for f in files_after) / 1024 / 1024
    total_del   = before_count - after_count

    result = {
        "deleted_total":    total_del,
        "deleted_by_age":   by_age,
        "deleted_by_count": by_count,
        "deleted_by_size":  by_size,
        "files_before":     before_count,
        "files_after":      after_count,
        "size_before_mb":   round(before_mb, 2),
        "size_after_mb":    round(after_mb,  2),
    }

    if total_del > 0:
        _cleanup_logger.info(
            f"Hapus {total_del} file "
            f"({before_mb:.1f} MB → {after_mb:.1f} MB) | "
            f"age={by_age} count={by_count} size={by_size}"
        )
    return result


async def _cleanup_loop():
    """Background loop: jalankan cleanup setiap CLEANUP_INTERVAL_MIN menit."""
    _cleanup_logger.info(f"Loop aktif — interval {CLEANUP_INTERVAL_MIN} menit")
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_MIN * 60)
        try:
            run_cleanup()
        except Exception as exc:
            _cleanup_logger.error(f"Cleanup loop error (non-fatal, loop tetap jalan): {exc}", exc_info=True)


# ─── Startup / Shutdown ───────────────────────────────────────────────────────

@app.on_event("startup")
async def on_startup():
    # Init SQLite job store (buat tabel jika belum ada, auto-migrate schema)
    init_db()

    # Bersihkan sisa video dari sesi / deployment sebelumnya
    result = run_cleanup()
    _cleanup_logger.info(f"Boot cleanup: {result}")

    # Hapus record job lama dari DB (>48 jam) supaya tidak menumpuk
    deleted_jobs = delete_old_jobs(max_age_hours=48)
    if deleted_jobs:
        _cleanup_logger.info(f"Boot: hapus {deleted_jobs} job record lama dari DB")

    # Start background cleanup loop
    asyncio.create_task(_cleanup_loop())

    if SCHEDULER_AVAILABLE:
        sched_module.start_scheduler()
        await notifier.notify_startup()


@app.on_event("shutdown")
async def on_shutdown():
    if SCHEDULER_AVAILABLE:
        sched_module.stop_scheduler()


# ─── AI Provider Registry (delegasi ke ai_client) ────────────────────────────
# Definisi lengkap ada di ai_client.py — topics.py dan modul lain import dari sana
# untuk menghindari circular import. main.py re-export agar kode lama tetap jalan.

from ai_client import AI_MODELS, ENV_KEY_MAP, PROVIDER_COLORS, call_ai, clean_json


# ─── Pydantic Models ──────────────────────────────────────────────────────────

class ScriptRequest(BaseModel):
    topic: str
    title: Optional[str] = ""
    style: Optional[str] = "cinematic"
    num_slides: Optional[int] = 5
    model_key: Optional[str] = "claude-sonnet-4"


class SlideItem(BaseModel):
    script: str
    image_prompt: str
    duration: int = 5


class RenderRequest(BaseModel):
    title: str
    slides: list[SlideItem]
    voice: str = "id-ID-ArdiNeural"
    style: str = "cinematic"
    width: int = 1280
    height: int = 720


class AutoRunRequest(BaseModel):
    """Request untuk trigger pipeline otomatis manual."""
    # FIX #9: validasi slot di level Pydantic — nilai invalid langsung HTTP 422
    # sebelum masuk pipeline, bukan gagal jauh di dalam saat topik tidak ketemu
    slot: Literal["pagi", "siang", "malam"] = "pagi"
    model_key: Optional[str] = None  # override DEFAULT_MODEL jika diisi


# ─── Endpoints: Original ──────────────────────────────────────────────────────


@app.get("/")
@app.get("/health")
def health_check():
    """
    Health check endpoint untuk Railway.
    Validasi: scheduler running, DB bisa dibaca, disk tidak penuh.
    """
    import time, shutil as _shutil

    checks = {}
    overall = "ok"

    # 1. Scheduler running?
    if SCHEDULER_AVAILABLE:
        try:
            status = sched_module.get_scheduler_status()
            checks["scheduler"] = {
                "status": "ok",
                "enabled": status.get("enabled", False),
                "next_jobs": status.get("jobs", []),
            }
        except Exception as e:
            checks["scheduler"] = {"status": "error", "detail": str(e)}
            overall = "degraded"
    else:
        checks["scheduler"] = {"status": "unavailable"}
        overall = "degraded"

    # 2. DB bisa dibaca?
    try:
        _ = JOBS.count_active()
        checks["db"] = {"status": "ok"}
    except Exception as e:
        checks["db"] = {"status": "error", "detail": str(e)}
        overall = "degraded"

    # 3. Disk tidak penuh (cek outputs/ dan filesystem)?
    try:
        total, used, free = _shutil.disk_usage(str(OUTPUT_DIR))
        free_gb  = free  / 1024 ** 3
        used_pct = used / total * 100
        disk_ok  = free_gb > 0.2  # < 200 MB sisa = warning
        checks["disk"] = {
            "status": "ok" if disk_ok else "warning",
            "free_gb": round(free_gb, 2),
            "used_pct": round(used_pct, 1),
        }
        if not disk_ok:
            overall = "degraded"
    except Exception as e:
        checks["disk"] = {"status": "error", "detail": str(e)}
        overall = "degraded"

    # 4. Output folder: berapa file mp4 aktif?
    try:
        mp4_files = list(OUTPUT_DIR.glob("*.mp4"))
        thumb_files = list(OUTPUT_DIR.glob("*_thumb.jpg"))
        checks["storage"] = {
            "status": "ok",
            "mp4_count": len(mp4_files),
            "thumb_count": len(thumb_files),
            "limit_files": VIDEO_MAX_FILES,
            "limit_mb": VIDEO_MAX_SIZE_MB,
        }
    except Exception as e:
        checks["storage"] = {"status": "error", "detail": str(e)}

    from fastapi.responses import JSONResponse
    status_code = 200 if overall == "ok" else 207
    return JSONResponse(
        status_code=status_code,
        content={
            "status": overall,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "service": "Video Content Maker API — Auto Scheduler Edition",
            "checks": checks,
        },
    )


@app.get("/models")
def list_models():
    return {
        "models": [
            {
                "key":           key,
                "label":         cfg["label"],
                "provider":      cfg["provider"],
                "provider_label":cfg["provider_label"],
                "tier":          cfg["tier"],
                "context":       cfg["context"],
                "color":         PROVIDER_COLORS.get(cfg["provider"], "#6366f1"),
                "available":     bool(os.getenv(ENV_KEY_MAP.get(cfg["provider"], ""), "")),
            }
            for key, cfg in AI_MODELS.items()
        ]
    }


@app.post("/generate-script")
async def generate_script(req: ScriptRequest):
    prompt = f"""Kamu adalah scriptwriter video profesional. Buat script untuk video berjudul "{req.title}" tentang topik: {req.topic}

Buat tepat {req.num_slides} slide. Respond HANYA dalam JSON (tanpa markdown/backtick), format:
{{
  "slides": [
    {{
      "script": "Teks narasi yang akan dibacakan oleh voice-over (1-3 kalimat)",
      "image_prompt": "Deskripsi gambar visual dalam bahasa Inggris, gaya: {req.style}",
      "duration": 5
    }}
  ]
}}

Aturan:
- Script: bahasa yang sama dengan topik, natural, engaging
- image_prompt: deskripsi visual detail dalam bahasa Inggris, sesuaikan dengan isi slide
- duration: 4-8 detik tergantung panjang script
- Jangan tambahkan penjelasan di luar JSON"""

    model_key = req.model_key or "claude-sonnet-4"
    raw  = await call_ai(model_key, prompt)
    try:
        data = json.loads(clean_json(raw))
    except (json.JSONDecodeError, ValueError) as exc:
        from fastapi import HTTPException
        preview = raw[:200].replace("\n", " ")
        raise HTTPException(
            status_code=502,
            detail=f"AI return non-JSON response ({exc}). Preview: {preview!r}"
        )
    cfg  = AI_MODELS.get(model_key, {})
    data["model_used"]    = cfg.get("label", model_key)
    data["provider_used"] = cfg.get("provider_label", "")
    return data


@app.post("/render-video")
async def render_video(req: RenderRequest, bg: BackgroundTasks):
    job_id = str(uuid.uuid4())[:12]
    # Persist ke SQLite sejak awal — survive restart
    from job_store import create_job
    create_job(job_id, title=req.title)
    bg.add_task(do_render, job_id, req)
    return {"job_id": job_id}


@app.get("/job-status/{job_id}")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        return {"status": "not_found"}
    # Kembalikan field yang dibutuhkan frontend (sama seperti sebelumnya)
    return {
        "status":    job["status"],
        "video_url": job.get("video_url"),
        "message":   job.get("message"),
    }


@app.get("/jobs")
def list_all_jobs(limit: int = 20):
    """Lihat daftar job terbaru dari DB (persistent, survive restart)."""
    from job_store import list_jobs
    return {"jobs": list_jobs(limit=limit)}


# ─── Endpoints: Baru (Scheduler) ─────────────────────────────────────────────

@app.get("/scheduler-status")
def get_scheduler_status():
    """Lihat status scheduler dan jadwal job berikutnya."""
    if not SCHEDULER_AVAILABLE:
        return {"enabled": False, "reason": "apscheduler tidak terinstall"}
    return sched_module.get_scheduler_status()


@app.post("/auto-run")
async def manual_auto_run(req: AutoRunRequest, bg: BackgroundTasks):
    """
    Trigger pipeline otomatis secara manual (tanpa menunggu jadwal).
    Berguna untuk test atau upload dadakan.

    FIX #2: Proteksi duplikasi dipindah ke dalam run_full_pipeline() di scheduler.py,
    sehingga APScheduler yang memanggil fungsi itu langsung juga terlindungi.
    Endpoint ini tidak perlu lock sendiri lagi — pipeline akan return early jika
    slot yang sama sudah berjalan (log warning, tidak crash).
    """
    if not SCHEDULER_AVAILABLE:
        return {"error": "Scheduler module tidak tersedia"}

    bg.add_task(sched_module.run_full_pipeline, req.slot, req.model_key)
    return {
        "status":  "started",
        "slot":    req.slot,
        "model":   req.model_key or os.getenv("DEFAULT_MODEL", "gemini-2-flash"),
        "message": f"Pipeline untuk slot '{req.slot}' dimulai di background",
    }


@app.get("/topics")
def list_topics():
    """Lihat semua topik yang terdaftar dan topik hari ini."""
    import json as _json
    from pathlib import Path as P
    try:
        with open(P(__file__).parent / "topics.json") as f:
            data = _json.load(f)
        # Hitung topik hari ini
        from datetime import date
        # FIX D: pakai ordinal sama seperti topics.py (get_topic_for_slot)
        # tm_yday berbeda di tahun kabisat; ordinal konsisten lintas tahun
        day_idx = date.today().toordinal()
        today = {}
        for slot in ("pagi", "siang", "malam"):
            items = data.get(slot, [])
            if items:
                today[slot] = items[day_idx % len(items)]["title"]
        return {"today": today, "all": {k: v for k, v in data.items() if not k.startswith("_")}}
    except Exception as e:
        return {"error": str(e)}


@app.get("/storage")
def storage_status():
    """
    Monitor penggunaan storage folder outputs/.
    Tampilkan daftar file, ukuran total, dan batas yang dikonfigurasi.
    """
    files = _get_mp4_files()
    total_bytes = 0
    file_list = []
    import time
    now = time.time()
    for f in reversed(files):  # terbaru di atas
        try:
            st = f.stat()
        except FileNotFoundError:
            continue  # file dihapus cleanup saat kita iterasi — skip
        age_hours = (now - st.st_mtime) / 3600
        size_bytes = st.st_size
        total_bytes += size_bytes
        file_list.append({
            "name":             f.name,
            "size_mb":          round(size_bytes / 1024 / 1024, 2),
            "age_hours":        round(age_hours, 1),
            "expires_in_hours": round(max(VIDEO_MAX_AGE_HOURS - age_hours, 0), 1),
        })

    return {
        "files":          file_list,
        "total_files":    len(files),
        "total_size_mb":  round(total_bytes / 1024 / 1024, 2),
        "limits": {
            "max_age_hours":    VIDEO_MAX_AGE_HOURS,
            "max_files":        VIDEO_MAX_FILES,
            "max_size_mb":      VIDEO_MAX_SIZE_MB,
            "cleanup_interval_min": CLEANUP_INTERVAL_MIN,
        },
        "usage_pct": {
            "by_count": round(len(files) / VIDEO_MAX_FILES * 100, 1),
            "by_size":  round(total_bytes / (VIDEO_MAX_SIZE_MB * 1024 * 1024) * 100, 1),
        },
    }


@app.post("/cleanup")
def trigger_cleanup():
    """
    Trigger cleanup manual via POST request.
    Berguna untuk darurat jika storage mendekati penuh.
    """
    result = run_cleanup()
    return {"status": "ok", **result}


# ─── Render Pipeline ──────────────────────────────────────────────────────────

async def do_render(job_id: str, req: RenderRequest):
    from job_store import set_job_status
    set_job_status(job_id, "processing")
    work_dir = Path(tempfile.mkdtemp())
    try:
        inputs_for_ffmpeg = []
        fallback_slides = []   # track slide yang pakai gambar fallback
        total = len(req.slides)
        for i, slide in enumerate(req.slides):
            slide_dir = work_dir / f"slide_{i:02d}"
            slide_dir.mkdir()

            # TTS voiceover
            audio_path = slide_dir / "audio.mp3"
            await gen_voiceover(slide.script, req.voice, str(audio_path))

            # Validasi file audio — gTTS kadang diam-diam gagal
            if not audio_path.exists() or audio_path.stat().st_size < 100:
                raise FileNotFoundError(
                    f"Slide {i+1}: audio gagal dibuat — "
                    f"file={'ada' if audio_path.exists() else 'tidak ada'}, "
                    f"size={audio_path.stat().st_size if audio_path.exists() else 0} bytes"
                )

            # Download + validasi gambar (dengan retry otomatis)
            img_path = slide_dir / "image.jpg"
            prompt   = f"{slide.image_prompt}, {req.style} style"
            await download_image(prompt, req.width, req.height, str(img_path))

            # Cek apakah slide ini pakai fallback (gambar terlalu kecil = fallback)
            valid, _ = _is_valid_image(str(img_path))
            if not valid:
                fallback_slides.append(i + 1)

            duration = await get_audio_duration(str(audio_path))
            duration = max(duration + 3, slide.duration)
            inputs_for_ffmpeg.append({"img": str(img_path), "audio": str(audio_path), "duration": duration})

            _img_logger.debug(f"Slide {i+1}/{total} siap")

        if fallback_slides:
            _img_logger.warning(
                f"Job {job_id}: {len(fallback_slides)} slide pakai gambar fallback "
                f"(slide #{', #'.join(map(str, fallback_slides))})"
            )

        output_name = f"{job_id}.mp4"
        output_path = OUTPUT_DIR / output_name
        await concat_slides(inputs_for_ffmpeg, req.width, req.height, str(output_path))

        base_url = os.getenv("RAILWAY_PUBLIC_DOMAIN", "http://localhost:8000")
        if not base_url.startswith("http"):
            base_url = f"https://{base_url}"
        video_url = f"{base_url}/videos/{output_name}"

        # ── Generate thumbnail dari slide pertama ────────────────────────────
        # Thumbnail disimpan di outputs/ berdampingan dengan video
        thumbnail_path = ""
        first_img = inputs_for_ffmpeg[0]["img"] if inputs_for_ffmpeg else ""
        if first_img:
            thumb_name = f"{job_id}_thumb.jpg"
            thumb_out  = str(OUTPUT_DIR / thumb_name)
            ok = await uploader.generate_thumbnail(
                image_path=first_img,
                title=req.title,
                out_path=thumb_out,
                width=1280,
                height=720,
            )
            if ok:
                thumbnail_path = thumb_out
                _img_logger.info(f"Thumbnail siap: {thumb_name}")

        # Persist hasil ke SQLite — video_url bisa diambil lagi setelah restart
        extra = {"fallback_slides": fallback_slides} if fallback_slides else {}
        if thumbnail_path:
            extra["thumbnail_path"] = thumbnail_path
        set_job_status(job_id, "done", video_url=video_url,
                       message=(f"{len(fallback_slides)} slide pakai gambar fallback" if fallback_slides else None),
                       extra_json=__import__("json").dumps(extra) if extra else None)

        # Cleanup setelah setiap render selesai — jaga storage tetap bersih
        run_cleanup()
    except Exception as e:
        import traceback
        logging.getLogger("render").error(
            f"do_render [{job_id}] error: {e}\n{traceback.format_exc()}"
        )
        set_job_status(job_id, "error", message=str(e))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


async def gen_voiceover(text: str, voice: str, out_path: str):
    # gTTS: tidak bergantung Edge TTS — tidak ada IP block di Railway
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _gtts_save, text, out_path)

def _gtts_save(text: str, out_path: str):
    tts = gTTS(text=text, lang="id", slow=False)
    tts.save(out_path)


# ─── Image Download: Retry + Validasi ────────────────────────────────────────

# Gambar fallback — dibuat saat startup via ffmpeg (solid dark, ukuran penuh)
def _make_fallback_jpeg(width: int = 1280, height: int = 720) -> bytes:
    """Generate solid dark JPEG via ffmpeg — dijamin valid untuk encoding."""
    import subprocess, tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        tmp = f.name
    try:
        subprocess.run([
            FFMPEG_BIN, "-y",
            "-f", "lavfi", "-i", f"color=c=0x1a1a2e:size={width}x{height}:rate=1",
            "-frames:v", "1", "-q:v", "5", tmp,
        ], capture_output=True, check=True)
        with open(tmp, "rb") as f:
            return f.read()
    except Exception:
        # Ultimate fallback: minimal valid JPEG 2x2 jika ffmpeg gagal
        return bytes([
            0xFF,0xD8,0xFF,0xE0,0x00,0x10,0x4A,0x46,0x49,0x46,0x00,0x01,
            0x01,0x00,0x00,0x01,0x00,0x01,0x00,0x00,0xFF,0xDB,0x00,0x43,
            0x00,0x08,0x06,0x06,0x07,0x06,0x05,0x08,0x07,0x07,0x07,0x09,
            0x09,0x08,0x0A,0x0C,0x14,0x0D,0x0C,0x0B,0x0B,0x0C,0x19,0x12,
            0x13,0x0F,0x14,0x1D,0x1A,0x1F,0x1E,0x1D,0x1A,0x1C,0x1C,0x20,
            0x24,0x2E,0x27,0x20,0x22,0x2C,0x23,0x1C,0x1C,0x28,0x37,0x29,
            0x2C,0x30,0x31,0x34,0x34,0x34,0x1F,0x27,0x39,0x3D,0x38,0x32,
            0x3C,0x2E,0x33,0x34,0x32,0xFF,0xC0,0x00,0x0B,0x08,0x00,0x02,
            0x00,0x02,0x01,0x01,0x11,0x00,0xFF,0xC4,0x00,0x1F,0x00,0x00,
            0x01,0x05,0x01,0x01,0x01,0x01,0x01,0x01,0x00,0x00,0x00,0x00,
            0x00,0x00,0x00,0x00,0x01,0x02,0x03,0x04,0x05,0x06,0x07,0x08,
            0x09,0x0A,0x0B,0xFF,0xC4,0x00,0xB5,0x10,0x00,0x02,0x01,0x03,
            0x03,0x02,0x04,0x03,0x05,0x05,0x04,0x04,0x00,0x00,0x01,0x7D,
            0xFF,0xDA,0x00,0x08,0x01,0x01,0x00,0x00,0x3F,0x00,0xFB,0xD3,
            0xFF,0xD9,
        ])
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass

_FALLBACK_JPEG = _make_fallback_jpeg()

_img_logger = logging.getLogger("image")

IMAGE_DOWNLOAD_RETRIES   = int(os.getenv("IMAGE_DOWNLOAD_RETRIES", "3"))
IMAGE_DOWNLOAD_TIMEOUT   = int(os.getenv("IMAGE_DOWNLOAD_TIMEOUT", "90"))
IMAGE_MIN_BYTES          = int(os.getenv("IMAGE_MIN_BYTES",        "2048"))   # < 2 KB dianggap gagal
IMAGE_RETRY_DELAY        = float(os.getenv("IMAGE_RETRY_DELAY",    "10"))     # detik antar retry


def _is_valid_image(path: str) -> tuple[bool, str]:
    """
    Cek apakah file di path adalah gambar JPEG/PNG yang valid.
    Return (valid: bool, reason: str).
    """
    p = Path(path)

    # 1. File harus ada dan punya ukuran minimal
    if not p.exists():
        return False, "file tidak ditemukan"
    size = p.stat().st_size
    if size < IMAGE_MIN_BYTES:
        return False, f"ukuran terlalu kecil ({size} bytes < {IMAGE_MIN_BYTES})"

    # 2. Cek magic bytes — JPEG dimulai FF D8, PNG dimulai 89 50 4E 47
    with open(path, "rb") as f:
        header = f.read(8)
    if header[:2] == b"\xFF\xD8":
        return True, "JPEG valid"
    if header[:4] == b"\x89PNG":
        return True, "PNG valid"

    # 3. Bukan gambar — kemungkinan dapat HTML error dari Pollinations
    preview = header.decode("ascii", errors="replace")
    return False, f"bukan gambar (header: {preview!r})"


async def download_image(prompt: str, width: int, height: int, out_path: str):
    """
    Download gambar dari Pixabay API.
    Set env var PIXABAY_API_KEY di Railway.
    Fallback ke Picsum jika Pixabay gagal.
    """
    import urllib.parse

    keywords = prompt.split(",")[0].strip()
    keywords = re.sub(r"[^a-zA-Z0-9 ]", "", keywords)
    keywords = "+".join(keywords.split()[:4])

    pixabay_key = os.getenv("PIXABAY_API_KEY", "")
    last_error = ""

    # Provider 1: Pixabay API
    if pixabay_key:
        try:
            api_url = (
                f"https://pixabay.com/api/?key={pixabay_key}"
                f"&q={urllib.parse.quote(keywords)}"
                f"&image_type=photo&orientation=horizontal"
                f"&min_width={width}&safesearch=true&per_page=5"
            )
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get(api_url)
            if r.status_code == 200:
                hits = r.json().get("hits", [])
                if hits:
                    img_url = hits[0].get("largeImageURL") or hits[0].get("webformatURL")
                    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
                        r2 = await c.get(img_url)
                    if r2.status_code == 200:
                        with open(out_path, "wb") as f:
                            f.write(r2.content)
                        valid, reason = _is_valid_image(out_path)
                        if valid:
                            _img_logger.info(f"Gambar OK dari Pixabay ({reason}, {Path(out_path).stat().st_size // 1024} KB)")
                            return
                        last_error = f"Pixabay validasi gagal: {reason}"
                    else:
                        last_error = f"Pixabay img HTTP {r2.status_code}"
                else:
                    last_error = "Pixabay: tidak ada hasil"
            else:
                last_error = f"Pixabay API HTTP {r.status_code}"
        except Exception as exc:
            last_error = f"Pixabay error: {exc}"
        _img_logger.warning(f"{last_error} — fallback ke Picsum")

    # Provider 2: Picsum fallback
    try:
        seed = uuid.uuid4().int % 99999
        url = f"https://picsum.photos/{width}/{height}?random={seed}"
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            r = await c.get(url)
        if r.status_code == 200:
            with open(out_path, "wb") as f:
                f.write(r.content)
            valid, reason = _is_valid_image(out_path)
            if valid:
                _img_logger.info(f"Gambar OK dari Picsum ({reason})")
                return
        last_error = f"Picsum HTTP {r.status_code}"
    except Exception as exc:
        last_error = f"Picsum error: {exc}"

    # Fallback abu-abu
    _img_logger.error(f"Semua provider gagal ({last_error}). Pakai fallback untuk: {prompt[:60]}")
    with open(out_path, "wb") as f:
        f.write(_FALLBACK_JPEG)


async def get_audio_duration(audio_path: str) -> float:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", audio_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        return float(out.decode().strip())
    except (ValueError, asyncio.TimeoutError, OSError):
        return 5.0


async def concat_slides(slides: list, width: int, height: int, output: str):
    if not slides:
        raise ValueError("concat_slides: tidak ada slide untuk digabung")

    tmp = Path(tempfile.mkdtemp())
    segment_paths = []
    try:
        for i, s in enumerate(slides):
            seg = str(tmp / f"seg_{i:02d}.mp4")
            cmd = [
                FFMPEG_BIN, "-y", "-loop", "1", "-i", s["img"], "-i", s["audio"],
                "-c:v", "libx264", "-tune", "stillimage", "-c:a", "aac", "-b:a", "128k",
                "-pix_fmt", "yuv420p",
                "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1",
                "-af", "apad=pad_dur=2",
                "-t", str(s["duration"]),
                seg,
            ]
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            if proc.returncode != 0:
                raise RuntimeError(
                    f"ffmpeg gagal encode slide {i} (exit {proc.returncode}): "
                    f"{stderr.decode()[-300:].strip()}"
                )
            segment_paths.append(seg)

        list_file = str(tmp / "list.txt")
        with open(list_file, "w") as f:
            for seg in segment_paths:
                f.write(f"file '{seg}'\n")

        concat_cmd = [FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", output]
        proc = await asyncio.create_subprocess_exec(*concat_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg gagal concat video (exit {proc.returncode}): "
                f"{stderr.decode()[-300:].strip()}"
            )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
