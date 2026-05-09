"""
topics.py — Topic Rotation Engine
Memilih topik secara rotasi berdasarkan hari ke-N dan slot waktu.
Setiap hari topik berbeda, tidak repetitif dalam satu siklus penuh.

Mode rotasi (set via env TOPIC_MODE):
  - "sequential"  : berurutan dari index 0, 1, 2, dst (default)
  - "ai_generate" : minta AI generate topik baru setiap hari (butuh API key)

Env vars:
  TOPIC_MODE      = sequential | ai_generate  (default: sequential)
  DEFAULT_MODEL   = model key untuk mode ai_generate (default: gemini-2-flash)
"""

import os
import json
import logging
from pathlib import Path
from datetime import date, datetime

logger = logging.getLogger(__name__)

TOPICS_FILE = Path(__file__).parent / "topics.json"
TOPIC_MODE  = os.getenv("TOPIC_MODE", "sequential")   # sequential | ai_generate


def load_topics() -> dict:
    with open(TOPICS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Hapus key metadata yang diawali underscore
    return {k: v for k, v in data.items() if not k.startswith("_")}


def get_topic_for_slot(slot: str) -> dict:
    """
    Pilih topik untuk slot tertentu (pagi/siang/malam) berdasarkan hari ini.
    Rotasi sequential: hari ke-N → topics[N % len(topics)]

    Catatan: menggunakan date.toordinal() (hari sejak epoch) bukan tm_yday
    agar distribusi merata di tahun kabisat.
    """
    topics_data = load_topics()
    slot_topics = topics_data.get(slot, [])

    if not slot_topics:
        raise ValueError(f"Slot '{slot}' tidak ditemukan di topics.json")

    # Pakai hari sejak epoch — distribusi adil lintas tahun kabisat
    day_index = date.today().toordinal()
    idx = day_index % len(slot_topics)

    chosen = slot_topics[idx].copy()
    chosen["_slot"]  = slot
    chosen["_index"] = idx
    chosen["_date"]  = date.today().isoformat()

    logger.info(
        f"[{slot.upper()}] Topik dipilih (idx={idx}/{len(slot_topics)-1}): {chosen['title']}"
    )
    return chosen


async def get_ai_generated_topic(slot: str) -> dict:
    """
    Mode AI Generate: minta AI buat topik baru yang relevan untuk hari ini.
    Hasil langsung dipakai — tidak dibuang ke void.

    Import call_ai dari ai_client (bukan main) untuk menghindari circular import.
    Fallback ke sequential jika AI gagal.
    """
    from ai_client import call_ai, clean_json

    # DEFAULT_MODEL dibaca saat fungsi dipanggil (bukan saat module load)
    # supaya override os.environ["DEFAULT_MODEL"] di runtime selalu terbaca.
    model_key = os.getenv("DEFAULT_MODEL", "gemini-2-flash")

    slot_context = {
        "pagi":  "motivasi pagi, produktivitas, kesehatan, lifestyle positif, sarapan, olahraga pagi",
        "siang": "edukasi, sains, teknologi, sejarah, fakta unik, informasi menarik, pengetahuan umum",
        "malam": "relaksasi, resep masakan, tips tidur, refleksi hari, hobi, hiburan ringan",
    }.get(slot, "konten umum yang menarik dan informatif")

    today = datetime.now().strftime("%A, %d %B %Y")

    prompt = f"""Hari ini: {today}
Slot waktu: {slot} (tema: {slot_context})

Buat 1 ide topik video pendek yang menarik untuk channel YouTube berbahasa Indonesia.
Respond HANYA dengan JSON tanpa markdown:
{{
  "title": "Judul video yang menarik dan clickable (max 60 karakter)",
  "topic": "Deskripsi detail topik untuk scriptwriter, 2-3 kalimat",
  "style": "cinematic",
  "voice": "id-ID-ArdiNeural",
  "num_slides": 6,
  "tags": ["tag1", "tag2", "tag3"],
  "ratio": "16:9"
}}

Aturan:
- Judul: pakai angka atau kata kuat (5 Tips, Rahasia, Fakta, Cara, dll)
- Topik aktual dan relevan dengan tanggal hari ini jika memungkinkan
- Tags: 3-5 kata kunci populer"""

    try:
        raw  = await call_ai(model_key, prompt)
        data = json.loads(clean_json(raw))

        # Validasi field wajib — jika AI return JSON tidak lengkap, fallback
        if not data.get("title") or not data.get("topic"):
            raise ValueError("Respons AI tidak mengandung 'title' atau 'topic'")

        # Isi field opsional dengan default yang aman
        data.setdefault("style",      "cinematic")
        data.setdefault("voice",      "id-ID-ArdiNeural")
        data.setdefault("num_slides", 6)
        data.setdefault("tags",       [])
        data.setdefault("ratio",      "16:9")

        data["_slot"]         = slot
        data["_date"]         = date.today().isoformat()
        data["_ai_generated"] = True
        data["_model_used"]   = model_key

        logger.info(f"[{slot.upper()}] Topik AI-generated ({model_key}): {data['title']}")
        return data

    except json.JSONDecodeError as e:
        logger.warning(f"[{slot.upper()}] AI return JSON tidak valid ({e}), fallback ke sequential")
        return get_topic_for_slot(slot)
    except Exception as e:
        logger.warning(f"[{slot.upper()}] AI generate topic gagal ({e}), fallback ke sequential")
        return get_topic_for_slot(slot)


async def get_topic(slot: str) -> dict:
    """Entry point utama — pilih topik berdasarkan TOPIC_MODE (dibaca fresh dari env)."""
    topic_mode = os.getenv("TOPIC_MODE", "sequential")
    if topic_mode == "ai_generate":
        return await get_ai_generated_topic(slot)
    return get_topic_for_slot(slot)
