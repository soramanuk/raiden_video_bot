"""
uploader.py — Auto Upload ke YouTube & Telegram

Mendukung upload video hasil render ke berbagai platform.

ENV VARS:
  UPLOAD_TARGET = "telegram" | "youtube" | "both" | "none"  (default: telegram)

  # Telegram (paling mudah):
  TELEGRAM_BOT_TOKEN  = token dari @BotFather
  TELEGRAM_CHANNEL_ID = @channelname atau -100xxxxxxxx

  # YouTube (butuh OAuth2 setup awal):
  YOUTUBE_CLIENT_ID        = dari Google Cloud Console
  YOUTUBE_CLIENT_SECRET    = dari Google Cloud Console
  YOUTUBE_REFRESH_TOKEN    = dapatkan via oauth_setup.py
  YOUTUBE_DEFAULT_PRIVACY  = "public" | "unlisted" | "private"  (default: public)
  YOUTUBE_DEFAULT_CATEGORY = angka category ID  (default: "22" = People & Blogs)
"""

import os
import glob
import asyncio
import logging
import tempfile
from pathlib import Path

import httpx
import imageio_ffmpeg as _iio_ffmpeg   # FIX #1: dipindah ke top-level import

logger = logging.getLogger(__name__)

UPLOAD_TARGET = os.getenv("UPLOAD_TARGET", "telegram")

# ─── Telegram Uploader ────────────────────────────────────────────────────────

async def upload_to_telegram(
    video_url: str,
    title: str,
    tags: list[str] = None,
    slot: str = "",
    local_path: str = "",
) -> dict:
    """
    Upload video ke Telegram Channel.
    Jika local_path disediakan, file dipakai langsung (tidak download ulang).
    """
    bot_token  = os.getenv("TELEGRAM_BOT_TOKEN", "")
    channel_id = os.getenv("TELEGRAM_CHANNEL_ID", os.getenv("TELEGRAM_CHAT_ID", ""))

    if not bot_token or not channel_id:
        raise ValueError(
            "TELEGRAM_BOT_TOKEN dan TELEGRAM_CHANNEL_ID wajib diisi untuk upload Telegram"
        )

    slot_emoji = {"pagi": "🌅", "siang": "☀️", "malam": "🌙"}.get(slot, "🎬")
    tag_text   = " ".join(f"#{t.replace(' ', '_')}" for t in (tags or []))
    caption    = f"{slot_emoji} <b>{title}</b>\n\n{tag_text}"
    api_url    = f"https://api.telegram.org/bot{bot_token}"

    # ── Pakai file lokal yang sudah di-download ───────────────────────────
    if local_path and Path(local_path).exists():
        logger.info(f"Telegram: pakai file lokal: {local_path}")
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                with open(local_path, "rb") as f:
                    r = await client.post(
                        f"{api_url}/sendDocument",
                        data={"chat_id": channel_id, "caption": caption, "parse_mode": "HTML"},
                        files={"document": (f"{title}.mp4", f, "video/mp4")},
                    )
                r.raise_for_status()
                result = r.json()["result"]
            logger.info(f"Upload Telegram berhasil via file lokal: {title}")
            return {
                "platform": "telegram",
                "success": True,
                "message_id": result.get("message_id"),
                "url": "",
            }
        except Exception as e:
            logger.error(f"Upload Telegram dari file lokal gagal: {e}")
            raise

    # ── Coba kirim via URL langsung ───────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(f"{api_url}/sendVideo", json={
                "chat_id": channel_id,
                "video": video_url,
                "caption": caption,
                "parse_mode": "HTML",
                "supports_streaming": True,
            })
        if r.status_code == 200 and r.json().get("ok"):
            result = r.json()["result"]
            logger.info(f"Upload Telegram berhasil via URL: {title}")
            return {
                "platform": "telegram",
                "success": True,
                "message_id": result.get("message_id"),
                "url": f"https://t.me/{channel_id.lstrip('@')}/{result.get('message_id', '')}",
            }
    except Exception as e:
        logger.warning(f"Upload Telegram via URL gagal, coba download dulu: {e}")

    # ── Fallback: download lalu upload sebagai file ───────────────────────
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        async with httpx.AsyncClient(timeout=180) as client:
            async with client.stream("GET", video_url, follow_redirects=True) as r:
                r.raise_for_status()
                with open(tmp_path, "wb") as fout:
                    async for chunk in r.aiter_bytes(chunk_size=1024 * 256):
                        fout.write(chunk)

            with open(tmp_path, "rb") as f:
                r2 = await client.post(
                    f"{api_url}/sendDocument",
                    data={"chat_id": channel_id, "caption": caption, "parse_mode": "HTML"},
                    files={"document": (f"{title}.mp4", f, "video/mp4")},
                )
            r2.raise_for_status()
            result = r2.json()["result"]

        logger.info(f"Upload Telegram berhasil via download+file: {title}")
        return {
            "platform": "telegram",
            "success": True,
            "message_id": result.get("message_id"),
            "url": "",
        }
    finally:
        Path(tmp_path).unlink(missing_ok=True)


# ─── YouTube Uploader ─────────────────────────────────────────────────────────

async def _refresh_youtube_token() -> str:
    client_id     = os.getenv("YOUTUBE_CLIENT_ID", "")
    client_secret = os.getenv("YOUTUBE_CLIENT_SECRET", "")
    refresh_token = os.getenv("YOUTUBE_REFRESH_TOKEN", "")

    if not all([client_id, client_secret, refresh_token]):
        raise ValueError(
            "YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, dan YOUTUBE_REFRESH_TOKEN wajib diisi"
        )

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post("https://oauth2.googleapis.com/token", data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        })
        r.raise_for_status()
        return r.json()["access_token"]


async def upload_to_youtube(
    video_url: str,
    title: str,
    description: str = "",
    tags: list[str] = None,
    slot: str = "",
    thumbnail_path: str = "",
    local_path: str = "",
) -> dict:
    privacy  = os.getenv("YOUTUBE_DEFAULT_PRIVACY", "public")
    category = os.getenv("YOUTUBE_DEFAULT_CATEGORY", "22")

    access_token = await _refresh_youtube_token()

    slot_desc = {
        "pagi": "konten pagi hari",
        "siang": "konten siang hari",
        "malam": "konten malam hari",
    }.get(slot, "")

    full_desc = description or f"Video otomatis dibuat oleh Raiden Auto Video Maker. {slot_desc}"
    if tags:
        full_desc += "\n\n" + " ".join(f"#{t}" for t in tags)

    metadata = {
        "snippet": {
            "title": title,
            "description": full_desc,
            "tags": tags or [],
            "categoryId": category,
            "defaultLanguage": "id",
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }

    _owns_tmp = False
    if local_path and Path(local_path).exists():
        tmp_path = local_path
        logger.info(f"YouTube: pakai file lokal: {local_path}")
    else:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp_path = tmp.name
        _owns_tmp = True

    try:
        if _owns_tmp:
            async with httpx.AsyncClient(timeout=180) as client:
                async with client.stream("GET", video_url, follow_redirects=True) as r:
                    r.raise_for_status()
                    with open(tmp_path, "wb") as fout:
                        async for chunk in r.aiter_bytes(chunk_size=1024 * 256):
                            fout.write(chunk)

        file_size = Path(tmp_path).stat().st_size
        logger.info(f"Video ready: {file_size / 1024 / 1024:.1f} MB")

        async with httpx.AsyncClient(timeout=60) as client:
            init_r = await client.post(
                "https://www.googleapis.com/upload/youtube/v3/videos"
                "?uploadType=resumable&part=snippet,status",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                    "X-Upload-Content-Type": "video/mp4",
                    "X-Upload-Content-Length": str(file_size),
                },
                json=metadata,
            )
            init_r.raise_for_status()
            upload_url = init_r.headers["Location"]

        async with httpx.AsyncClient(timeout=600) as client:
            with open(tmp_path, "rb") as f:
                upload_r = await client.put(
                    upload_url,
                    content=f,
                    headers={
                        "Content-Type": "video/mp4",
                        "Content-Length": str(file_size),
                    },
                )
            upload_r.raise_for_status()
            video_data = upload_r.json()

        video_id = video_data.get("id", "")
        logger.info(f"Upload YouTube berhasil: https://youtu.be/{video_id}")

        thumbnail_uploaded = False
        if thumbnail_path and video_id:
            thumbnail_uploaded = await set_youtube_thumbnail(video_id, thumbnail_path, access_token)

        return {
            "platform": "youtube",
            "success": True,
            "video_id": video_id,
            "url": f"https://youtu.be/{video_id}",
            "thumbnail_uploaded": thumbnail_uploaded,
        }
    finally:
        if _owns_tmp:
            Path(tmp_path).unlink(missing_ok=True)


# ─── YouTube Thumbnail ────────────────────────────────────────────────────────

async def set_youtube_thumbnail(video_id: str, thumbnail_path: str, access_token: str) -> bool:
    thumb = Path(thumbnail_path)
    if not thumb.exists():
        logger.warning(f"Thumbnail tidak ditemukan: {thumbnail_path}")
        return False

    mime = "image/jpeg" if thumb.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            with open(thumbnail_path, "rb") as f:
                r = await client.post(
                    f"https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
                    f"?videoId={video_id}&uploadType=media",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": mime,
                    },
                    content=f.read(),
                )
        if r.status_code in (200, 201):
            logger.info(f"Thumbnail berhasil diupload untuk video {video_id}")
            return True
        logger.warning(f"Thumbnail upload gagal: HTTP {r.status_code} — {r.text[:200]}")
        return False
    except Exception as e:
        logger.error(f"Thumbnail upload error: {e}")
        return False


# ─── Thumbnail Generator ──────────────────────────────────────────────────────

def _find_font() -> str:
    """
    FIX #2: Cari font TTF yang tersedia di container (Nix/Debian).
    Return path absolut ke font, atau string kosong jika tidak ada.
    """
    candidates = [
        # Nix (nixpacks dengan dejavu_fonts)
        "/run/current-system/sw/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/run/current-system/sw/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        # Debian/Ubuntu (apt install fonts-dejavu-core)
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        # Glob fallback — temukan TTF manapun
        *glob.glob("/run/current-system/sw/share/fonts/**/*.ttf", recursive=True),
        *glob.glob("/usr/share/fonts/**/*.ttf", recursive=True),
    ]
    for path in candidates:
        if Path(path).exists():
            return path
    return ""


async def generate_thumbnail(
    image_path: str,
    title: str,
    out_path: str,
    width: int = 1280,
    height: int = 720,
) -> bool:
    """
    Buat thumbnail YouTube dari gambar slide pertama + overlay judul via FFmpeg.

    FIX #1: import imageio_ffmpeg dipindah ke top-level (sudah tidak ada di sini).
    FIX #2: font path eksplisit agar drawtext tidak gagal di container tanpa fontconfig.
    """
    _FFMPEG = _iio_ffmpeg.get_ffmpeg_exe()

    # Sanitasi judul
    safe_title = (
        title
        .replace("'", "")
        .replace(":", " -")
        .replace("\\", "")
        .replace("[", "(")
        .replace("]", ")")
        .replace("%", "pct")
        .replace("=", " ")
    )
    if len(safe_title) > 60:
        safe_title = safe_title[:57] + "..."

    box_y = height - 180

    # FIX #2: tambahkan fontfile eksplisit jika ditemukan
    font_path = _find_font()
    if font_path:
        logger.info(f"Menggunakan font: {font_path}")
        font_arg = f":fontfile='{font_path}'"
    else:
        logger.warning("Tidak ada font TTF ditemukan — drawtext mungkin gagal di container tanpa fontconfig")
        font_arg = ""

    filters = [
        f"scale={width}:{height}:force_original_aspect_ratio=increase",
        f"crop={width}:{height}:(iw-{width})/2:(ih-{height})/2",
        f"drawbox=x=0:y={box_y}:w={width}:h=180:color=black@0.55:t=fill",
        (
            f"drawtext=text='{safe_title}'"
            f"{font_arg}"
            f":fontsize=52:fontcolor=white"
            f":x=(w-text_w)/2:y={height - 110}"
            f":shadowcolor=black@0.8:shadowx=3:shadowy=3"
        ),
    ]
    filter_str = ",".join(filters)

    cmd = [
        _FFMPEG, "-y",
        "-i", image_path,
        "-vf", filter_str,
        "-vframes", "1",
        "-q:v", "2",
        out_path,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            err_msg = stderr.decode()[-400:]
            logger.warning(f"FFmpeg thumbnail gagal: {err_msg}")
            return False
        size_kb = Path(out_path).stat().st_size // 1024
        logger.info(f"Thumbnail dibuat: {out_path} ({size_kb} KB)")
        return True
    except Exception as e:
        logger.error(f"generate_thumbnail error: {e}")
        return False


# ─── Dispatcher ───────────────────────────────────────────────────────────────

async def upload(
    video_url: str,
    title: str,
    tags: list[str] = None,
    slot: str = "",
    description: str = "",
    thumbnail_path: str = "",
) -> list[dict]:
    """
    Upload ke platform sesuai env UPLOAD_TARGET.
    Saat UPLOAD_TARGET=both, video di-download SEKALI lalu diteruskan ke masing-masing uploader.
    """
    target  = UPLOAD_TARGET.lower().strip()
    results = []

    # ── Pre-download untuk mode "both" ───────────────────────────────────
    shared_local_path = ""
    if target == "both":
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            shared_local_path = tmp.name
        try:
            logger.info(f"Pre-download video untuk upload ganda: {video_url}")
            async with httpx.AsyncClient(timeout=180) as client:
                async with client.stream("GET", video_url, follow_redirects=True) as r:
                    r.raise_for_status()
                    with open(shared_local_path, "wb") as fout:
                        async for chunk in r.aiter_bytes(chunk_size=1024 * 256):
                            fout.write(chunk)
            size_mb = Path(shared_local_path).stat().st_size / 1024 / 1024
            logger.info(f"Video pre-download selesai: {size_mb:.1f} MB")
        except Exception as e:
            logger.error(f"Pre-download gagal: {e} — fallback ke download per-platform")
            Path(shared_local_path).unlink(missing_ok=True)
            shared_local_path = ""

    try:
        if target in ("telegram", "both"):
            try:
                res = await upload_to_telegram(
                    video_url, title, tags=tags, slot=slot,
                    local_path=shared_local_path,
                )
                results.append(res)
            except Exception as e:
                logger.error(f"Upload Telegram gagal: {e}")
                results.append({"platform": "telegram", "success": False, "error": str(e)})

        if target in ("youtube", "both"):
            try:
                res = await upload_to_youtube(
                    video_url, title,
                    description=description,
                    tags=tags,
                    slot=slot,
                    thumbnail_path=thumbnail_path,
                    local_path=shared_local_path,
                )
                results.append(res)
            except Exception as e:
                logger.error(f"Upload YouTube gagal: {e}")
                results.append({"platform": "youtube", "success": False, "error": str(e)})

        if target == "none":
            logger.info("UPLOAD_TARGET=none — video tidak diupload")
            results.append({"platform": "none", "success": True, "url": video_url})

    finally:
        if shared_local_path:
            Path(shared_local_path).unlink(missing_ok=True)

    return results
