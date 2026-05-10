"""
Video Content Maker — FastAPI Backend (Multi-Model AI Edition + Auto Scheduler)

Deploy ke Railway: https://railway.app

BARU di versi ini:
✅ Scheduler otomatis 3x sehari (05:00 / 12:00 / 19:00 WIB)
✅ POST /auto-run — trigger pipeline penuh 1 klik
✅ GET /scheduler-status — lihat status & jadwal berikutnya
✅ Upload otomatis ke Telegram / YouTube
✅ Notifikasi Telegram setiap sukses/gagal

FIX v2 (2026-05):
✅ gTTS lang="en" — script sekarang bahasa Inggris
✅ generate_script prompt force English narration
✅ Image provider: Pollinations only (Pixabay dihapus)
✅ Font TTF fallback — drawtext pakai dejavu dari nixpacks

FIX v3.6 (2026-05) — PARALLEL PIPELINE:
✅ do_render: semua slide diproses PARALEL via asyncio.gather
✅ Per slide: voiceover + image download berjalan BERSAMAAN
✅ Semaphore(3) mencegah terlalu banyak koneksi ke Pollinations
✅ Waktu render turun dari ~10 menit → ~90 detik untuk 6 slide
✅ 30 topik baru Islamic facts di topics.json

UPDATE v4.1 (2026-05) — FIX VOICE + VISUAL UPGRADE:
✅ Suara: OpenAI TTS API (echo voice, laki-laki natural & energik)
✅ Gambar: fal.ai FLUX schnell (primary) → Pollinations → Picsum → static fallback
   - edge-tts dibuang: diblokir 403 di Railway (WebSocket ke Microsoft diblokir)
   - OpenAI TTS: suara "echo" — laki-laki, natural, energik, tidak bikin ngantuk
   - Speed 1.05x — sedikit lebih cepat & energik
   - Env: OPENAI_TTS_VOICE (default: echo) | OPENAI_TTS_MODEL (default: tts-1)
   - Fallback: gTTS jika OPENAI_API_KEY tidak ada / error
✅ Caption: text narasi max 3 kata di bagian bawah setiap slide
✅ Color Overlay: transisi warna cinematic per slide (8 palet)
✅ Fade transition: fade-in/out 0.4s per slide
✅ Gambar: Pollinations AI diprioritaskan (relevan dgn tema)

Supported AI Providers:
- Anthropic Claude (claude-sonnet-4-20250514, claude-haiku-4-5-20251001)
- Groq (llama-4-scout-17b-16e-instruct, llama-4-maverick-17b-128e-instruct, llama3-70b-8192)
- Google Gemini (gemini-2.0-flash, gemini-1.5-pro)
- OpenAI (gpt-4.1, gpt-4o-mini)
- Mistral (mistral-large-latest, open-mixtral-8x7b)
"""

import os, uuid, asyncio, json, tempfile, shutil
from pathlib import Path
from typing import Optional, Literal
import httpx
# edge-tts dihapus — diblokir 403 oleh Railway (WebSocket ke Microsoft diblokir cloud VPS)
# Ganti ke OpenAI TTS API (suara laki-laki natural, jalan di Railway)
import imageio_ffmpeg as _iio_ffmpeg

FFMPEG_BIN = _iio_ffmpeg.get_ffmpeg_exe()

import uploader
from fastapi import FastAPI
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

JOBS = JobStore()
OUTPUT_DIR = Path("outputs")
OUTPUT_DIR.mkdir(exist_ok=True)
app.mount("/videos", StaticFiles(directory="outputs"), name="videos")

# ─── Cleanup Config ───────────────────────────────────────────────────────────
VIDEO_MAX_AGE_HOURS   = int(os.getenv("VIDEO_MAX_AGE_HOURS",   "24"))
VIDEO_MAX_FILES       = int(os.getenv("VIDEO_MAX_FILES",       "10"))
VIDEO_MAX_SIZE_MB     = int(os.getenv("VIDEO_MAX_SIZE_MB",     "500"))
CLEANUP_INTERVAL_MIN  = int(os.getenv("CLEANUP_INTERVAL_MIN",  "30"))

# ─── Font detection for drawtext ──────────────────────────────────────────────
import logging
import logging as _logging

_font_logger = _logging.getLogger("font")

def _find_ttf_font() -> str:
    """
    Cari font TTF. Strategy (urutan prioritas):
    1. font.ttf bundled di direktori yang sama dgn main.py (di-commit ke repo)
    2. Path standard Ubuntu/Debian
    3. Glob di /nix/store (Railway Nix)
    4. fc-list jika tersedia
    Return path string atau "" jika tidak ada.
    """
    import glob as _glob

    # ── Strategy 0: /app/font.ttf — Railway deploy ke /app ──────────────────
    for _forced in ["/app/font.ttf", "/app/fonts/font.ttf"]:
        if Path(_forced).exists() and Path(_forced).stat().st_size > 10000:
            _font_logger.info(f"Font Railway: {_forced}")
            return _forced

    # ── Strategy 1: font.ttf bundled di repo ─────────────────────────────────
    bundled = Path(__file__).parent / "font.ttf"
    if bundled.exists() and bundled.stat().st_size > 10000:
        _font_logger.info(f"Font bundled OK: {bundled}")
        return str(bundled)

    # ── Strategy 2: Ubuntu/Debian standard ───────────────────────────────────
    for path in [
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/truetype/crosextra/Carlito-Bold.ttf",
    ]:
        if Path(path).exists():
            _font_logger.info(f"Font std: {path}")
            return path

    # ── Strategy 3: Nix store glob ────────────────────────────────────────────
    for pattern in [
        "/nix/store/*/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/nix/store/*/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/nix/store/*/share/fonts/**/*Bold*.ttf",
    ]:
        matches = _glob.glob(pattern, recursive=True)
        if matches and Path(matches[0]).exists():
            _font_logger.info(f"Font Nix: {matches[0]}")
            return matches[0]

    # ── Strategy 4: fc-list ────────────────────────────────────────────────────
    try:
        import subprocess
        r = subprocess.run(["fc-list", "--format=%{file}\n"],
                           capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            f = line.strip()
            if f and f.endswith(".ttf") and Path(f).exists():
                _font_logger.info(f"Font fc-list: {f}")
                return f
    except Exception:
        pass

    _font_logger.warning("Tidak ada font TTF — drawtext dinonaktifkan")
    return ""

TTF_FONT_PATH = _find_ttf_font()

# ─── Cleanup Engine ───────────────────────────────────────────────────────────
_cleanup_logger = _logging.getLogger("cleanup")

def _get_mp4_files() -> list:
    return sorted(OUTPUT_DIR.glob("*.mp4"), key=lambda f: f.stat().st_mtime)

def _delete_mp4_with_thumb(f: Path) -> None:
    thumb = f.with_name(f.stem + "_thumb.jpg")
    f.unlink(missing_ok=True)
    thumb.unlink(missing_ok=True)

def cleanup_by_age() -> int:
    import time
    cutoff = time.time() - (VIDEO_MAX_AGE_HOURS * 3600)
    deleted = 0
    for f in OUTPUT_DIR.glob("*.mp4"):
        if f.stat().st_mtime < cutoff:
            _delete_mp4_with_thumb(f)
            deleted += 1
    return deleted

def cleanup_by_count() -> int:
    files = _get_mp4_files()
    excess = len(files) - VIDEO_MAX_FILES
    deleted = 0
    for f in files[:max(excess, 0)]:
        _delete_mp4_with_thumb(f)
        deleted += 1
    return deleted

def cleanup_by_size() -> int:
    limit_bytes = VIDEO_MAX_SIZE_MB * 1024 * 1024
    files = _get_mp4_files()
    total_bytes = sum(f.stat().st_size for f in files)
    deleted = 0
    for f in files:
        if total_bytes <= limit_bytes:
            break
        total_bytes -= f.stat().st_size
        _delete_mp4_with_thumb(f)
        deleted += 1
    return deleted

def run_cleanup() -> dict:
    files_before = list(OUTPUT_DIR.glob("*.mp4"))
    before_count = len(files_before)
    before_mb = sum(f.stat().st_size for f in files_before) / 1024 / 1024

    by_age   = cleanup_by_age()
    by_count = cleanup_by_count()
    by_size  = cleanup_by_size()

    files_after = list(OUTPUT_DIR.glob("*.mp4"))
    after_count = len(files_after)
    after_mb = sum(f.stat().st_size for f in files_after) / 1024 / 1024
    total_del = before_count - after_count

    result = {
        "deleted_total": total_del, "deleted_by_age": by_age,
        "deleted_by_count": by_count, "deleted_by_size": by_size,
        "files_before": before_count, "files_after": after_count,
        "size_before_mb": round(before_mb, 2), "size_after_mb": round(after_mb, 2),
    }
    if total_del > 0:
        _cleanup_logger.info(
            f"Hapus {total_del} file ({before_mb:.1f} MB → {after_mb:.1f} MB) | "
            f"age={by_age} count={by_count} size={by_size}"
        )
    return result

async def _cleanup_loop():
    _cleanup_logger.info(f"Loop aktif — interval {CLEANUP_INTERVAL_MIN} menit")
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_MIN * 60)
        try:
            run_cleanup()
        except Exception as exc:
            _cleanup_logger.error(f"Cleanup loop error: {exc}", exc_info=True)

# ─── Startup / Shutdown ───────────────────────────────────────────────────────
@app.on_event("startup")
async def on_startup():
    init_db()
    result = run_cleanup()
    _cleanup_logger.info(f"Boot cleanup: {result}")
    deleted_jobs = delete_old_jobs(max_age_hours=48)
    if deleted_jobs:
        _cleanup_logger.info(f"Boot: hapus {deleted_jobs} job record lama dari DB")
    asyncio.create_task(_cleanup_loop())
    if SCHEDULER_AVAILABLE:
        sched_module.start_scheduler()
        await notifier.notify_startup()

# ─── Active task registry — cegah task dicancel saat shutdown ───────────────
_active_render_tasks: set[asyncio.Task] = set()

def _register_task(coro) -> asyncio.Task:
    """Buat task, daftarkan ke registry, hapus otomatis saat selesai."""
    task = asyncio.create_task(coro)
    _active_render_tasks.add(task)
    task.add_done_callback(_active_render_tasks.discard)
    return task

@app.on_event("shutdown")
async def on_shutdown():
    if SCHEDULER_AVAILABLE:
        sched_module.stop_scheduler()
    # Graceful shutdown: tunggu semua render task selesai (max 600s)
    if _active_render_tasks:
        import logging as _sl
        _sl.getLogger("shutdown").warning(
            f"Shutdown: menunggu {len(_active_render_tasks)} render task selesai..."
        )
        try:
            await asyncio.wait_for(
                asyncio.gather(*_active_render_tasks, return_exceptions=True),
                timeout=600,
            )
        except asyncio.TimeoutError:
            _sl.getLogger("shutdown").error("Shutdown timeout — render task tidak selesai dalam 600s")

# ─── AI Provider Registry ────────────────────────────────────────────────────
from ai_client import AI_MODELS, ENV_KEY_MAP, PROVIDER_COLORS, call_ai, clean_json

# ─── Pydantic Models ──────────────────────────────────────────────────────────
class ScriptRequest(BaseModel):
    topic: str
    title: Optional[str] = ""
    style: Optional[str] = "cinematic"
    num_slides: Optional[int] = 5
    model_key: Optional[str] = "llama-4-scout"

class SlideItem(BaseModel):
    script: str
    image_prompt: str
    duration: int = 5

class RenderRequest(BaseModel):
    title: str
    slides: list[SlideItem]
    voice: str = "en-US-AndrewNeural"
    style: str = "cinematic"
    width: int = 1280
    height: int = 720

class AutoRunRequest(BaseModel):
    slot: Literal["pagi", "siang", "malam"] = "pagi"
    model_key: Optional[str] = None

# ─── Endpoints ────────────────────────────────────────────────────────────────
@app.get("/")
@app.get("/health")
def health_check():
    import time, shutil as _shutil
    checks = {}
    overall = "ok"

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

    try:
        _ = JOBS.count_active()
        checks["db"] = {"status": "ok"}
    except Exception as e:
        checks["db"] = {"status": "error", "detail": str(e)}
        overall = "degraded"

    try:
        total, used, free = _shutil.disk_usage(str(OUTPUT_DIR))
        free_gb = free / 1024 ** 3
        used_pct = used / total * 100
        disk_ok = free_gb > 0.2
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

    try:
        mp4_files  = list(OUTPUT_DIR.glob("*.mp4"))
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

    # ── Font check ──────────────────────────────────────────────────────────
    checks["font"] = {
        "status": "ok" if TTF_FONT_PATH else "warning",
        "path": TTF_FONT_PATH or "none — drawtext disabled",
    }

    from fastapi.responses import JSONResponse
    status_code = 200 if overall == "ok" else 207
    return JSONResponse(
        status_code=status_code,
        content={
            "status": overall,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "service": "Video Content Maker API — Auto Scheduler Edition v2",
            "checks": checks,
        },
    )

@app.get("/models")
def list_models():
    return {
        "models": [
            {
                "key": key,
                "label": cfg["label"],
                "provider": cfg["provider"],
                "provider_label": cfg["provider_label"],
                "tier": cfg["tier"],
                "context": cfg["context"],
                "color": PROVIDER_COLORS.get(cfg["provider"], "#6366f1"),
                "available": bool(os.getenv(ENV_KEY_MAP.get(cfg["provider"], ""), "")),
            }
            for key, cfg in AI_MODELS.items()
        ]
    }

@app.post("/generate-script")
async def generate_script(req: ScriptRequest):
    # FIX v3.4: Casual viral English slang — TikTok/Reels/Shorts style narration
    prompt = f"""You are a viral short-form video scriptwriter for TikTok, Instagram Reels, and YouTube Shorts. Write a script for a video titled "{req.title}" about: {req.topic}

Create exactly {req.num_slides} slides. Respond ONLY in JSON (no markdown/backticks), format:

{{
  "slides": [
    {{
      "script": "Casual spoken English narration, 1-3 sentences max. Use viral TikTok tone: punchy, conversational, shocking facts, rhetorical questions. Like you are talking directly to the viewer. No formal language.",
      "image_prompt": "Detailed cinematic visual description in English matching the slide content, {req.style} style",
      "duration": 5
    }}
  ]
}}

STRICT RULES for script field:
- Write in casual spoken English — contractions OK (didn't, wasn't, they've)
- Open with a hook: "Bro...", "Wait, what?", "Nobody talks about this but...", "This fact will break your brain:", "Here's something wild:", "Did you know that...", "Plot twist:"
- Keep sentences SHORT and punchy — like you're telling a friend something crazy
- Use rhetorical questions to keep viewer hooked: "And get this...", "But here's the kicker:", "So why does nobody talk about this?"
- End the LAST slide with a call-to-action like: "Follow for more wild Islamic history facts." or "Drop a comment if this blew your mind."
- NEVER use formal academic language, passive voice, or long complex sentences
- image_prompt: detailed visual description in English, {req.style} style, cinematic composition
- duration: 4-8 seconds depending on script length
- Do NOT add any explanation outside the JSON"""

    model_key = req.model_key or "llama-4-scout"
    raw = await call_ai(model_key, prompt)
    try:
        data = json.loads(clean_json(raw))
    except (json.JSONDecodeError, ValueError) as exc:
        from fastapi import HTTPException
        preview = raw[:200].replace("\n", " ")
        raise HTTPException(
            status_code=502,
            detail=f"AI return non-JSON response ({exc}). Preview: {preview!r}"
        )
    cfg = AI_MODELS.get(model_key, {})
    data["model_used"]    = cfg.get("label", model_key)
    data["provider_used"] = cfg.get("provider_label", "")
    return data

@app.post("/render-video")
async def render_video(req: RenderRequest):
    # v3.6.3: asyncio.create_task (bukan BackgroundTasks) agar tidak dicancel
    # saat Railway restart/shutdown request. Task hidup di event loop utama.
    job_id = str(uuid.uuid4())[:12]
    from job_store import create_job
    create_job(job_id, title=req.title)
    _register_task(do_render(job_id, req))
    return {"job_id": job_id}

@app.get("/job-status/{job_id}")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        return {"status": "not_found"}
    return {
        "status": job["status"],
        "video_url": job.get("video_url"),
        "message": job.get("message"),
    }

@app.get("/jobs")
def list_all_jobs(limit: int = 20):
    from job_store import list_jobs
    return {"jobs": list_jobs(limit=limit)}

@app.get("/scheduler-status")
def get_scheduler_status():
    if not SCHEDULER_AVAILABLE:
        return {"enabled": False, "reason": "apscheduler tidak terinstall"}
    return sched_module.get_scheduler_status()

@app.post("/auto-run")
async def manual_auto_run(req: AutoRunRequest):
    # v3.6.3: asyncio.create_task agar pipeline tidak dicancel mid-render
    if not SCHEDULER_AVAILABLE:
        return {"error": "Scheduler module tidak tersedia"}
    _register_task(sched_module.run_full_pipeline(req.slot, req.model_key))
    return {
        "status": "started",
        "slot": req.slot,
        "model": req.model_key or os.getenv("DEFAULT_MODEL", "llama-4-scout"),
        "message": f"Pipeline untuk slot '{req.slot}' dimulai di background",
    }

@app.get("/topics")
def list_topics():
    import json as _json
    from pathlib import Path as P
    try:
        with open(P(__file__).parent / "topics.json") as f:
            data = _json.load(f)
        from datetime import date
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
    files = _get_mp4_files()
    total_bytes = 0
    file_list = []
    import time
    now = time.time()
    for f in reversed(files):
        try:
            st = f.stat()
        except FileNotFoundError:
            continue
        age_hours  = (now - st.st_mtime) / 3600
        size_bytes = st.st_size
        total_bytes += size_bytes
        file_list.append({
            "name": f.name,
            "size_mb": round(size_bytes / 1024 / 1024, 2),
            "age_hours": round(age_hours, 1),
            "expires_in_hours": round(max(VIDEO_MAX_AGE_HOURS - age_hours, 0), 1),
        })
    return {
        "files": file_list,
        "total_files": len(files),
        "total_size_mb": round(total_bytes / 1024 / 1024, 2),
        "limits": {
            "max_age_hours": VIDEO_MAX_AGE_HOURS,
            "max_files": VIDEO_MAX_FILES,
            "max_size_mb": VIDEO_MAX_SIZE_MB,
            "cleanup_interval_min": CLEANUP_INTERVAL_MIN,
        },
        "usage_pct": {
            "by_count": round(len(files) / VIDEO_MAX_FILES * 100, 1),
            "by_size": round(total_bytes / (VIDEO_MAX_SIZE_MB * 1024 * 1024) * 100, 1),
        },
    }

@app.post("/cleanup")
def trigger_cleanup():
    result = run_cleanup()
    return {"status": "ok", **result}

# ─── Render Pipeline ──────────────────────────────────────────────────────────
async def do_render(job_id: str, req: RenderRequest):
    from job_store import set_job_status
    set_job_status(job_id, "processing")
    work_dir = Path(tempfile.mkdtemp())
    try:
        fallback_slides = []
        total = len(req.slides)

        # ── FIX v3.6: Parallel slide processing ──────────────────────────────
        # Sebelumnya: sequential — 6 slides × (gTTS + Pollinations 90s) = timeout 600s
        # Sekarang: semua slide diproses BERSAMAAN → total waktu ≈ 1 slide saja
        # Semaphore 3 mencegah terlalu banyak koneksi simultan ke Pollinations
        _img_semaphore = asyncio.Semaphore(1)  # v3.6.2: sequential image download cegah 429

        async def _process_one_slide(i: int, slide) -> dict:
            """Process satu slide: voiceover + image download secara paralel."""
            slide_dir = work_dir / f"slide_{i:02d}"
            slide_dir.mkdir(exist_ok=True)
            audio_path = slide_dir / "audio.mp3"
            img_path   = slide_dir / "image.jpg"
            prompt     = f"{slide.image_prompt}, {req.style} style"

            async def _fetch_image():
                async with _img_semaphore:
                    await download_image(prompt, req.width, req.height, str(img_path))

            # Jalankan voiceover + image download PARALEL dalam satu slide
            await asyncio.gather(
                gen_voiceover(slide.script, req.voice, str(audio_path)),
                _fetch_image(),
            )

            if not audio_path.exists() or audio_path.stat().st_size < 100:
                raise FileNotFoundError(
                    f"Slide {i+1}: audio gagal dibuat — "
                    f"file={'ada' if audio_path.exists() else 'tidak ada'}, "
                    f"size={audio_path.stat().st_size if audio_path.exists() else 0} bytes"
                )

            valid, _ = _is_valid_image(str(img_path))
            is_fallback = not valid

            _img_logger.debug(f"Slide {i+1}/{total} siap (fallback={is_fallback})")
            return {
                "idx": i,
                "img": str(img_path),
                "audio": str(audio_path),
                "script": slide.script,  # v4.0: untuk caption overlay
                "is_fallback": is_fallback,
            }

        # Proses SEMUA slide secara paralel sekaligus
        _img_logger.info(f"Job {job_id}: memproses {total} slide secara paralel...")
        results = await asyncio.gather(*[
            _process_one_slide(i, slide)
            for i, slide in enumerate(req.slides)
        ])

        # Urutkan kembali berdasarkan index (gather tidak jamin urutan jika ada error)
        results_sorted = sorted(results, key=lambda r: r["idx"])
        inputs_for_ffmpeg = [{"img": r["img"], "audio": r["audio"], "script": r["script"]} for r in results_sorted]
        fallback_slides   = [r["idx"] + 1 for r in results_sorted if r["is_fallback"]]

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
                # font_path tidak diperlukan — uploader.py auto-detect via _find_ttf_font()
            )
            if ok:
                thumbnail_path = thumb_out
                _img_logger.info(f"Thumbnail siap: {thumb_name}")

        extra = {"fallback_slides": fallback_slides} if fallback_slides else {}
        if thumbnail_path:
            extra["thumbnail_path"] = thumbnail_path

        set_job_status(
            job_id, "done", video_url=video_url,
            message=(f"{len(fallback_slides)} slide pakai gambar fallback" if fallback_slides else None),
            extra_json=__import__("json").dumps(extra) if extra else None,
        )
        run_cleanup()

    except Exception as e:
        import traceback
        logging.getLogger("render").error(
            f"do_render [{job_id}] error: {e}\n{traceback.format_exc()}"
        )
        set_job_status(job_id, "error", message=str(e))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# v4.1: OpenAI TTS — suara laki-laki natural, jalan di Railway (tidak diblokir seperti edge-tts)
# Voice options: alloy, echo, fable, onyx, nova, shimmer
# onyx = laki-laki dalam & berwibawa | echo = laki-laki natural & energik (default) | fable = ekspresif
OPENAI_TTS_VOICE = os.getenv("OPENAI_TTS_VOICE", "echo")   # default: echo (laki-laki, natural, tidak bikin ngantuk)
OPENAI_TTS_MODEL = os.getenv("OPENAI_TTS_MODEL", "tts-1")  # tts-1 (cepat) | tts-1-hd (kualitas tinggi)

async def gen_voiceover(text: str, voice: str, out_path: str):
    """
    Generate voiceover dengan fallback chain:
    1. ElevenLabs TTS — suara pria natural & gratis (10k char/bulan)
    2. OpenAI TTS — suara pria jika OPENAI_API_KEY tersedia
    3. gTTS (Google TTS) — fallback terakhir (suara wanita, robot)
    """
    _vo_log = logging.getLogger("voiceover")

    # ── Provider 1: ElevenLabs — suara pria natural, gratis ──────────────────
    el_key = os.getenv("ELEVENLABS_API_KEY", "")
    if el_key:
        try:
            import httpx as _hx
            # Adam: suara pria natural & jelas, cocok untuk narasi edukasi
            EL_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "pNInz6obpgDQGcFmaJgB")  # Adam
            EL_MODEL    = os.getenv("ELEVENLABS_MODEL", "eleven_turbo_v2_5")  # cepat & hemat kuota
            el_headers = {
                "xi-api-key": el_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            }
            el_payload = {
                "text": text,
                "model_id": EL_MODEL,
                "voice_settings": {
                    "stability": 0.5,
                    "similarity_boost": 0.75,
                    "style": 0.3,
                    "use_speaker_boost": True,
                }
            }
            async with _hx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"https://api.elevenlabs.io/v1/text-to-speech/{EL_VOICE_ID}",
                    headers=el_headers,
                    json=el_payload,
                )
            if resp.status_code == 200:
                with open(out_path, "wb") as f:
                    f.write(resp.content)
                size_kb = Path(out_path).stat().st_size // 1024
                if size_kb > 1:
                    _vo_log.info(f"ElevenLabs TTS OK (Adam, {EL_MODEL}) → {size_kb} KB")
                    return
                _vo_log.warning(f"ElevenLabs output terlalu kecil ({size_kb} KB) — fallback")
            else:
                _vo_log.warning(f"ElevenLabs HTTP {resp.status_code}: {resp.text[:200]} — fallback")
        except Exception as exc:
            _vo_log.warning(f"ElevenLabs error: {exc} — fallback OpenAI")
    else:
        _vo_log.info("ELEVENLABS_API_KEY tidak ada — skip ElevenLabs")

    # ── Provider 2: OpenAI TTS ────────────────────────────────────────────────
    openai_key = os.getenv("OPENAI_API_KEY", "")
    if openai_key:
        try:
            import httpx as _hx
            tts_voice = OPENAI_TTS_VOICE
            tts_model = OPENAI_TTS_MODEL
            headers = {
                "Authorization": f"Bearer {openai_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": tts_model,
                "input": text,
                "voice": tts_voice,
                "response_format": "mp3",
                "speed": 1.05,
            }
            async with _hx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/audio/speech",
                    headers=headers,
                    json=payload,
                )
            if resp.status_code == 200:
                with open(out_path, "wb") as f:
                    f.write(resp.content)
                size_kb = Path(out_path).stat().st_size // 1024
                if size_kb > 1:
                    _vo_log.info(f"OpenAI TTS OK ({tts_voice}, {tts_model}) → {size_kb} KB")
                    return
                _vo_log.warning(f"OpenAI TTS output terlalu kecil ({size_kb} KB) — fallback gTTS")
            else:
                _vo_log.warning(f"OpenAI TTS HTTP {resp.status_code}: {resp.text[:200]} — fallback gTTS")
        except Exception as exc:
            _vo_log.warning(f"OpenAI TTS error: {exc} — fallback gTTS")
    else:
        _vo_log.info("OPENAI_API_KEY tidak ada — skip OpenAI TTS")

    # ── Provider 3: gTTS (Google TTS) — fallback terakhir ───────────────────
    try:
        from gtts import gTTS
        _vo_log.warning("Menggunakan gTTS fallback (suara wanita — semua provider gagal)")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: gTTS(text=text, lang="en", slow=False).save(out_path))
        _vo_log.info(f"gTTS OK → {Path(out_path).stat().st_size // 1024} KB")
    except Exception as exc:
        _vo_log.error(f"gTTS juga gagal: {exc}")

# ─── Image Download: Pollinations Only (Pixabay removed) ─────────────────────
def _make_fallback_jpeg(width: int = 1280, height: int = 720) -> bytes:
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

# v3.6: Timeout diturunkan 90→45s, retries 3→2
# Karena sekarang paralel, cepat gagal = lebih baik daripada menunggu lama
IMAGE_DOWNLOAD_RETRIES = int(os.getenv("IMAGE_DOWNLOAD_RETRIES", "5"))
IMAGE_DOWNLOAD_TIMEOUT = int(os.getenv("IMAGE_DOWNLOAD_TIMEOUT", "45"))
IMAGE_MIN_BYTES        = int(os.getenv("IMAGE_MIN_BYTES",        "2048"))
IMAGE_RETRY_DELAY      = float(os.getenv("IMAGE_RETRY_DELAY",   "15"))

def _is_valid_image(path: str) -> tuple[bool, str]:
    p = Path(path)
    if not p.exists():
        return False, "file tidak ditemukan"
    size = p.stat().st_size
    if size < IMAGE_MIN_BYTES:
        return False, f"ukuran terlalu kecil ({size} bytes < {IMAGE_MIN_BYTES})"
    with open(path, "rb") as f:
        header = f.read(8)
    if header[:2] == b"\xFF\xD8":
        return True, "JPEG valid"
    if header[:4] == b"\x89PNG":
        return True, "PNG valid"
    preview = header.decode("ascii", errors="replace")
    return False, f"bukan gambar (header: {preview!r})"

async def _try_download_url(url: str, out_path: str, label: str) -> bool:
    """
    Coba download satu URL gambar. Return True jika berhasil dan valid.
    Pisah connect/read timeout — mencegah Pollinations yang 'connect OK tapi
    streaming sangat lambat' dari menggantung selama IMAGE_DOWNLOAD_TIMEOUT penuh.
    """
    _timeout = httpx.Timeout(connect=10.0, read=IMAGE_DOWNLOAD_TIMEOUT, write=10.0, pool=5.0)
    try:
        async with httpx.AsyncClient(timeout=_timeout, follow_redirects=True) as c:
            r = await c.get(url)
        if r.status_code != 200:
            _img_logger.warning(f"[{label}] HTTP {r.status_code}")
            return False
        with open(out_path, "wb") as f:
            f.write(r.content)
        valid, reason = _is_valid_image(out_path)
        if not valid:
            _img_logger.warning(f"[{label}] validasi gagal: {reason}")
            return False
        _img_logger.debug(f"[{label}] OK — {Path(out_path).stat().st_size // 1024} KB")
        return True
    except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
        _img_logger.warning(f"[{label}] Network error: {type(exc).__name__}")
        return False
    except Exception as exc:
        _img_logger.warning(f"[{label}] Error: {exc}")
        return False


async def download_image(prompt: str, width: int, height: int, out_path: str):
    """
    Download gambar dengan strategi 4-tier:
    1. Unsplash API — foto HD relevan berdasarkan keyword (primary, 50 req/jam gratis)
    2. Pollinations AI — AI image gratis
    3. Picsum dengan seed dari hash prompt — foto HD reliable
    4. Static fallback JPEG — tidak pernah gagal

    v4.8: Unsplash sebagai primary provider — foto real berkualitas tinggi.
    UNSPLASH_ACCESS_KEY env var wajib ada untuk mengaktifkan Unsplash.
    """
    import urllib.parse, hashlib, re
    encoded = urllib.parse.quote(prompt)
    prompt_seed = int(hashlib.md5(prompt.encode()).hexdigest(), 16) % 1000

    # ── Provider 1: Unsplash API — foto HD relevan ────────────────────────────
    unsplash_key = os.getenv("UNSPLASH_ACCESS_KEY", "")
    if unsplash_key:
        try:
            # Ekstrak keyword penting dari prompt (max 5 kata)
            # Buang kata-kata umum yang tidak relevan untuk pencarian foto
            stopwords = {"a","an","the","of","in","on","at","to","for","with",
                        "and","or","is","was","are","were","be","been","being",
                        "have","has","had","do","does","did","will","would",
                        "could","should","may","might","shall","can","this",
                        "that","these","those","from","by","as","into","through",
                        "during","before","after","above","below","between","photo",
                        "image","picture","showing","depicting","scene","view"}
            words = re.findall(r"[a-zA-Z]+", prompt.lower())
            keywords = [w for w in words if w not in stopwords and len(w) > 3][:5]
            search_query = " ".join(keywords) if keywords else prompt[:50]

            unsplash_url = (
                f"https://api.unsplash.com/photos/random"
                f"?query={urllib.parse.quote(search_query)}"
                f"&orientation={'landscape' if width > height else 'portrait'}"
                f"&content_filter=high"
            )
            _uh_timeout = httpx.Timeout(connect=8.0, read=20.0, write=8.0, pool=5.0)
            async with httpx.AsyncClient(timeout=_uh_timeout) as c:
                resp = await c.get(
                    unsplash_url,
                    headers={"Authorization": f"Client-ID {unsplash_key}"},
                )
            if resp.status_code == 200:
                data = resp.json()
                # Ambil URL foto resolusi tinggi sesuai ukuran
                raw_url = data["urls"].get("regular") or data["urls"].get("full")
                # Tambah parameter resize agar sesuai ukuran slide
                img_url = f"{raw_url}&w={width}&h={height}&fit=crop&crop=entropy"
                async with httpx.AsyncClient(timeout=_uh_timeout) as c:
                    img_resp = await c.get(img_url, follow_redirects=True)
                if img_resp.status_code == 200:
                    with open(out_path, "wb") as f:
                        f.write(img_resp.content)
                    valid, reason = _is_valid_image(out_path)
                    if valid:
                        size_kb = Path(out_path).stat().st_size // 1024
                        photographer = data.get("user", {}).get("name", "unknown")
                        _img_logger.info(f"Unsplash OK ({search_query!r}, by {photographer}) — {size_kb} KB")
                        return
                    _img_logger.warning(f"Unsplash image invalid: {reason} — fallback Pollinations")
                else:
                    _img_logger.warning(f"Unsplash download HTTP {img_resp.status_code} — fallback")
            elif resp.status_code == 403:
                _img_logger.warning("Unsplash 403 — rate limit atau key salah, fallback Pollinations")
            else:
                _img_logger.warning(f"Unsplash HTTP {resp.status_code}: {resp.text[:100]} — fallback")
        except Exception as exc:
            _img_logger.warning(f"Unsplash error: {exc} — fallback Pollinations")
    else:
        _img_logger.info("UNSPLASH_ACCESS_KEY tidak ada — skip Unsplash, pakai Pollinations")

    # ── Provider 2: Pollinations AI — AI image gratis ─────────────────────────
    seed = uuid.uuid4().int % 99999
    pol_url = (
        f"https://image.pollinations.ai/prompt/{encoded}"
        f"?width={width}&height={height}&nologo=true&seed={seed}&enhance=true"
    )
    _pol_timeout = httpx.Timeout(connect=8.0, read=25.0, write=8.0, pool=5.0)
    try:
        async with httpx.AsyncClient(timeout=_pol_timeout, follow_redirects=True) as c:
            r = await c.get(pol_url)
        if r.status_code == 200:
            with open(out_path, "wb") as f:
                f.write(r.content)
            valid, reason = _is_valid_image(out_path)
            if valid:
                _img_logger.info(f"Pollinations OK — {Path(out_path).stat().st_size // 1024} KB")
                return
            _img_logger.warning(f"Pollinations invalid: {reason} — fallback Picsum")
        else:
            _img_logger.warning(f"Pollinations HTTP {r.status_code} — langsung Picsum")
    except Exception as exc:
        _img_logger.warning(f"Pollinations {type(exc).__name__} — langsung Picsum")

    # ── Provider 3: Picsum dengan seed dari hash prompt ───────────────────────
    picsum_url = f"https://picsum.photos/seed/{prompt_seed}/{width}/{height}"
    if await _try_download_url(picsum_url, out_path, "Picsum"):
        _img_logger.info(f"Picsum OK (seed={prompt_seed})")
        return

    # ── Provider 4: Static JPEG fallback ─────────────────────────────────────
    _img_logger.error("Semua provider gagal — pakai static fallback JPEG")
    with open(out_path, "wb") as f:
        f.write(_FALLBACK_JPEG)


async def get_audio_duration(audio_path: str) -> float:
    """
    Ukur durasi audio dengan ffprobe. Fallback ke mutagen, lalu ke 8.0s default.
    Log hasilnya agar mudah debug jika ada slide terpotong.
    """
    _render_log = logging.getLogger("render")
    # Method 1: mutagen (pure Python, tidak butuh ffprobe binary)
    # ffprobe dinonaktifkan — imageio_ffmpeg tidak bundel ffprobe binary
    try:
        from mutagen.mp3 import MP3
        audio = MP3(audio_path)
        dur = audio.info.length
        if dur > 0.5:
            _render_log.info(f"audio_dur mutagen: {dur:.2f}s — {audio_path}")
            return dur
    except Exception as e:
        _render_log.warning(f"mutagen gagal: {e}")

    # Method 3: file size heuristic (128kbps mp3 = ~16KB/s)
    try:
        size_bytes = os.path.getsize(audio_path)
        dur = size_bytes / 16000.0
        if dur > 0.5:
            _render_log.warning(f"audio_dur heuristic: {dur:.2f}s — {audio_path}")
            return dur
    except Exception:
        pass

    _render_log.error(f"audio_dur fallback 8.0s — {audio_path}")
    return 8.0


async def concat_slides(slides: list, width: int, height: int, output: str):
    if not slides:
        raise ValueError("concat_slides: tidak ada slide untuk digabung")

    # ── v4.0: Warna overlay per slide — mengikuti mood narasi ────────────────
    # Palet pasangan warna untuk dual-color overlay cinematic
    # Format: (warna_atas, warna_bawah) — gradient 2 warna per slide
    SLIDE_OVERLAY_PAIRS = [
        ("0x1a1a2e@0.30", "0x533483@0.45"),   # Navy → Purple
        ("0x0f3460@0.30", "0x1a1a2e@0.45"),   # Royal Blue → Navy
        ("0x2d132c@0.30", "0x0f3460@0.45"),   # Burgundy → Blue
        ("0x1b262c@0.30", "0x0a3d62@0.45"),   # Dark Teal → Ocean Blue
        ("0x533483@0.30", "0x2d132c@0.45"),   # Purple → Burgundy
        ("0x0a3d62@0.30", "0x533483@0.45"),   # Ocean Blue → Purple
        ("0x16213e@0.30", "0x1b262c@0.45"),   # Midnight Blue → Dark Teal
        ("0x1e3a5f@0.30", "0x0f3460@0.45"),   # Denim → Royal Blue
    ]
    # Ken Burns directions: zoom+pan bervariasi tiap slide
    KB_EFFECTS = [
        {"zoom_start": 1.0,  "zoom_end": 1.08, "pan_x": 0,    "pan_y": 0   },  # zoom in center
        {"zoom_start": 1.08, "zoom_end": 1.0,  "pan_x": -0.03,"pan_y": 0   },  # zoom out + pan right
        {"zoom_start": 1.0,  "zoom_end": 1.08, "pan_x": 0.03, "pan_y": 0   },  # zoom in + pan left
        {"zoom_start": 1.05, "zoom_end": 1.0,  "pan_x": 0,    "pan_y": -0.02}, # zoom out + pan down
        {"zoom_start": 1.0,  "zoom_end": 1.06, "pan_x": -0.02,"pan_y": 0.02},  # zoom in + diagonal
        {"zoom_start": 1.08, "zoom_end": 1.0,  "pan_x": 0.02, "pan_y": -0.02}, # zoom out + diagonal
    ]

    def _make_caption(script: str, max_words: int = 3) -> str:
        """Ambil max 3 kata pertama yang meaningful dari script sebagai caption."""
        import re
        # Hapus filler pembuka viral (Bro, Wait, Did you, etc.) lalu ambil kata inti
        cleaned = re.sub(
            r"^(Bro[,\.\.\.]?|Wait[,\.\.\.]?|So[,\.]?|And[,\.]?|But[,\.]?|Here's|Did you know that|Plot twist:|This fact|Nobody|Get this:?|Okay so|Listen[,:]?)\s*",
            "", script, flags=re.IGNORECASE
        ).strip()
        words = cleaned.split()[:max_words]
        caption = " ".join(words)
        # Hapus tanda baca di akhir
        caption = re.sub(r"[,.!?:;]+$", "", caption).strip()
        return caption.upper() if caption else "NEXT FACT"

    def _escape_ffmpeg(text: str) -> str:
        """Escape teks untuk FFmpeg drawtext — strategi strip karakter berbahaya.

        Apostrophe seperti di "AL-IDRISI'S" adalah penyebab utama error
        karena membuka/menutup string di filter_complex. Solusi paling aman:
        hapus/ganti karakter bermasalah daripada escape (escape sering salah).
        """
        text = text.replace("'", "")    # apostrophe → hapus (AL-IDRISI'S → AL-IDRISIS)
        text = text.replace('"', "")    # double quote → hapus
        text = text.replace("\\", "") # backslash → hapus (cegah escape sequence rusak)
        text = text.replace(":", " ")   # colon → spasi
        text = text.replace(",", " ")   # comma → spasi
        text = text.replace("[", "")    # bracket → hapus
        text = text.replace("]", "")    # bracket → hapus
        text = text.replace("%", "")    # percent → hapus
        text = text.replace("{", "")    # curly brace → hapus
        text = text.replace("}", "")    # curly brace → hapus
        text = text.replace(";", "")    # semicolon → hapus (breaks filter_complex)
        text = text.replace("=", "")    # equals → hapus (breaks filter args)
        return text.strip()

    tmp = Path(tempfile.mkdtemp())
    segment_paths = []
    _render_log = logging.getLogger("render")

    try:
        for i, s in enumerate(slides):
            seg = str(tmp / f"seg_{i:02d}.mp4")
            audio_dur = await get_audio_duration(s["audio"])
            slide_dur = round(audio_dur + 1.5, 3)  # +1.5s tail (edge-tts lebih presisi dari gTTS)
            s["_dur"] = slide_dur

            # ── Script text untuk narasi layar (max 12 kata, 2 baris) ──────────
            script_text = s.get("script", "")
            # Narasi: ambil max 12 kata pertama yang meaningful
            import re as _re
            cleaned_script = _re.sub(
                r"^(Bro[,\.]?|Wait[,\.]?|So[,\.]?|And[,\.]?|But[,\.]?|Here's|Did you know|Plot twist:|Get this:?|Okay so|Listen[,:]?)\s*",
                "", script_text, flags=_re.IGNORECASE
            ).strip()
            words_12 = cleaned_script.split()[:12]
            # Bagi jadi 2 baris max 6 kata
            line1_words = words_12[:6]
            line2_words = words_12[6:]
            line1 = _escape_ffmpeg(" ".join(line1_words)).upper()
            line2 = _escape_ffmpeg(" ".join(line2_words)).upper() if line2_words else ""

            # ── Dual overlay colors per slide ─────────────────────────────────
            overlay_top, overlay_bot = SLIDE_OVERLAY_PAIRS[i % len(SLIDE_OVERLAY_PAIRS)]

            # ── Ken Burns effect parameters ───────────────────────────────────
            kb = KB_EFFECTS[i % len(KB_EFFECTS)]
            z_start = kb["zoom_start"]
            z_end   = kb["zoom_end"]
            px      = kb["pan_x"]
            py      = kb["pan_y"]
            # Ken Burns: zoompan filter
            # fps=25, total frames = slide_dur * 25
            total_frames = int(slide_dur * 25)
            # zoompan: zoom interpolasi dari z_start ke z_end selama slide
            # x/y offset untuk pan effect
            kb_filter = (
                f"scale={width*2}:{height*2},"  # scale 2x dulu supaya ada room untuk zoom/pan
                f"zoompan="
                f"z='min(zoom+({z_end-z_start:.4f}/{total_frames}),{max(z_start,z_end):.3f})':"
                f"x='iw/2-(iw/zoom/2)+({px:.4f}*iw)':"
                f"y='ih/2-(ih/zoom/2)+({py:.4f}*ih)':"
                f"d={total_frames}:s={width}x{height}:fps=25,"
                f"setsar=1"
            )

            # ── Fade transition ───────────────────────────────────────────────
            fade_dur = 0.4
            fade_out_start = max(slide_dur - fade_dur, slide_dur / 2)

            # ── Build filter_complex ──────────────────────────────────────────
            has_font = bool(TTF_FONT_PATH)

            if has_font:
                font_path_esc = TTF_FONT_PATH.replace(":", "\\:")
                # Dual overlay: bar atas tipis + bar bawah tebal untuk teks
                # Teks narasi 2 baris di bagian bawah
                line1_y = height - 130
                line2_y = height - 75
                text_filters = (
                    # Bar overlay atas (tipis, mood)
                    f"drawbox=x=0:y=0:w={width}:h=60:color={overlay_top}:t=fill,"
                    # Bar overlay bawah (tebal, untuk teks)
                    f"drawbox=x=0:y={height-160}:w={width}:h=160:color={overlay_bot}:t=fill,"
                    # Garis aksen warna di atas bar bawah
                    f"drawbox=x=0:y={height-162}:w={width}:h=3:color=white@0.6:t=fill,"
                    # Teks baris 1
                    f"drawtext=fontfile='{font_path_esc}':text='{line1}':"
                    f"fontsize=36:fontcolor=white:shadowcolor=black@0.8:shadowx=2:shadowy=2:"
                    f"x=(w-text_w)/2:y={line1_y}"
                )
                if line2:
                    text_filters += (
                        f",drawtext=fontfile='{font_path_esc}':text='{line2}':"
                        f"fontsize=36:fontcolor=white@0.9:shadowcolor=black@0.8:shadowx=2:shadowy=2:"
                        f"x=(w-text_w)/2:y={line2_y}"
                    )
                caption_filter = text_filters
            else:
                # Tanpa font: dual color bar saja (atas + bawah)
                caption_filter = (
                    f"drawbox=x=0:y=0:w={width}:h=8:color={overlay_top}:t=fill,"
                    f"drawbox=x=0:y={height-8}:w={width}:h=8:color={overlay_bot}:t=fill"
                )

            # Build filter_complex dengan Ken Burns + dual overlay + narasi
            filter_complex = (
                f"[0:v]{kb_filter},"
                f"fade=t=in:st=0:d={fade_dur}:alpha=0,"
                f"fade=t=out:st={fade_out_start}:d={fade_dur}:alpha=0,"
                f"{caption_filter}[v];"
                f"[1:a]apad=pad_dur=1[a]"
            )

            cmd = [
                FFMPEG_BIN, "-y",
                "-loop", "1", "-t", str(slide_dur), "-i", s["img"],
                "-i", s["audio"],
                "-filter_complex", filter_complex,
                "-map", "[v]", "-map", "[a]",
                "-t", str(slide_dur),
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage",
                "-c:a", "aac", "-b:a", "128k",
                "-pix_fmt", "yuv420p",
                seg,
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
            if proc.returncode != 0:
                err_msg = stderr.decode()[-400:].strip()
                _render_log.error(f"ffmpeg slide {i} error: {err_msg}")
                raise RuntimeError(
                    f"ffmpeg gagal encode slide {i} (exit {proc.returncode}): {err_msg}"
                )
            _render_log.info(f"Slide {i+1}/{len(slides)} encoded — line1: '{line1}' | dur: {slide_dur:.1f}s")
            segment_paths.append(seg)

        list_file = str(tmp / "list.txt")
        with open(list_file, "w") as f:
            for seg in segment_paths:
                f.write(f"file '{seg}'\n")

        total_dur = sum(s.get("_dur", 0) for s in slides)
        _render_log.info(f"Concat {len(slides)} slides — total durasi estimasi: {total_dur:.1f}s")

        concat_cmd = [
            FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0",
            "-i", list_file,
            "-c", "copy",
            "-movflags", "+faststart",
            output,
        ]
        proc = await asyncio.create_subprocess_exec(
            *concat_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
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
