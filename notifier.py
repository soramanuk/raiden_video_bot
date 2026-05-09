"""
notifier.py — Telegram notification module
Kirim notifikasi ke Telegram Bot saat video berhasil/gagal dibuat.

ENV VARS yang dibutuhkan:
  TELEGRAM_BOT_TOKEN  = token dari @BotFather
  TELEGRAM_CHAT_ID    = chat_id tujuan (bisa personal atau channel)

Cara dapat chat_id:
  1. Kirim pesan ke bot kamu
  2. Buka https://api.telegram.org/bot<TOKEN>/getUpdates
  3. Cari nilai "chat" -> "id"

Untuk channel: gunakan @channelname atau angka negatif seperti -1001234567890
"""

import os
import httpx
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# FIX #6: Jangan cache token di module-level — baca di dalam fungsi saat dipakai,
# sama seperti pola di uploader.py. Ini agar override os.environ setelah import
# (misalnya dari /auto-run atau test) ikut terbaca.


async def send(message: str) -> bool:
    """Kirim pesan teks ke Telegram. Return True jika berhasil."""
    # Baca fresh setiap kali — aman terhadap override env di runtime
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram tidak dikonfigurasi — TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID kosong")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
            logger.info(f"Notifikasi Telegram terkirim: {message[:60]}...")
            return True
    except Exception as e:
        logger.error(f"Gagal kirim Telegram: {e}")
        return False


async def notify_success(slot: str, title: str, video_url: str, model_used: str, duration_seconds: float):
    """Notifikasi sukses dengan detail video."""
    tz_label = os.getenv("TIMEZONE", "Asia/Jakarta")
    waktu = datetime.now().strftime(f"%d/%m/%Y %H:%M ({tz_label})")
    slot_emoji = {"pagi": "🌅", "siang": "☀️", "malam": "🌙"}.get(slot, "🎬")

    msg = (
        f"{slot_emoji} <b>Video Berhasil Dibuat!</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>Judul:</b> {title}\n"
        f"🕐 <b>Slot:</b> {slot.capitalize()} ({waktu})\n"
        f"🤖 <b>Model AI:</b> {model_used}\n"
        f"⏱️ <b>Proses:</b> {duration_seconds:.0f} detik\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎥 <a href=\"{video_url}\">Download Video</a>"
    )
    return await send(msg)


async def notify_error(slot: str, title: str, error_message: str):
    """Notifikasi error."""
    tz_label = os.getenv("TIMEZONE", "Asia/Jakarta")
    waktu = datetime.now().strftime(f"%d/%m/%Y %H:%M ({tz_label})")
    slot_emoji = {"pagi": "🌅", "siang": "☀️", "malam": "🌙"}.get(slot, "🎬")

    msg = (
        f"❌ <b>Video Gagal Dibuat</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{slot_emoji} <b>Slot:</b> {slot.capitalize()} ({waktu})\n"
        f"📌 <b>Topik:</b> {title}\n"
        f"⚠️ <b>Error:</b> <code>{error_message[:200]}</code>"
    )
    return await send(msg)


async def notify_startup():
    """Notifikasi saat sistem pertama kali berjalan. Jam dan timezone diambil dari env vars."""
    jam_pagi  = os.getenv("SCHEDULE_PAGI",  "5").zfill(2)
    jam_siang = os.getenv("SCHEDULE_SIANG", "12").zfill(2)
    jam_malam = os.getenv("SCHEDULE_MALAM", "19").zfill(2)
    tz_label  = os.getenv("TIMEZONE", "Asia/Jakarta")
    msg = (
        f"🚀 <b>Raiden Auto Video Maker — AKTIF</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📅 Jadwal upload harian ({tz_label}):\n"
        f"  🌅 Pagi  : {jam_pagi}:00\n"
        f"  ☀️ Siang : {jam_siang}:00\n"
        f"  🌙 Malam : {jam_malam}:00\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Sistem berjalan otomatis. Tidak perlu interaksi."
    )
    return await send(msg)


async def notify_upload_success(slot: str, title: str, platform: str, url: str = ""):
    """Notifikasi setelah berhasil upload ke platform."""
    platform_emoji = {
        "youtube": "▶️",
        "telegram": "📨",
        "tiktok": "🎵",
        "instagram": "📸",
    }.get(platform.lower(), "📤")

    msg = (
        f"{platform_emoji} <b>Berhasil Upload ke {platform.capitalize()}</b>\n"
        f"📌 {title}\n"
        f"🕐 Slot: {slot.capitalize()}"
    )
    if url:
        msg += f"\n🔗 <a href=\"{url}\">Lihat Video</a>"

    return await send(msg)
