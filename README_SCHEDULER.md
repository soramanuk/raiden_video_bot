# 🤖 Raiden Auto Video Maker — Auto Scheduler Edition

Upload video otomatis **3x sehari tanpa interaksi user** ke Telegram / YouTube.

```
🌅 05:00 WIB — Video pagi (motivasi, lifestyle, produktivitas)
☀️ 12:00 WIB — Video siang (edukasi, sains, teknologi)
🌙 19:00 WIB — Video malam (relaksasi, resep, wellness)
```

---

## 📁 Struktur File

```
├── main.py          ← FastAPI backend (update: +scheduler endpoints)
├── scheduler.py     ← APScheduler 3x sehari (BARU)
├── topics.py        ← Topic rotation engine (BARU)
├── topics.json      ← 7 topik × 3 slot = 21 topik unik (BARU)
├── uploader.py      ← Auto upload Telegram & YouTube (BARU)
├── notifier.py      ← Notifikasi Telegram (BARU)
├── oauth_setup.py   ← Setup YouTube OAuth2 sekali saja (BARU)
├── requirements.txt ← Update: +apscheduler, +google-api-python-client
└── nixpacks.toml    ← Railway build config (tidak berubah)
```

---

## 🚀 Deploy ke Railway (Step by Step)

### 1. Upload semua file ke Railway

Upload file berikut ke Railway project:
- `main.py`
- `scheduler.py`
- `topics.py`
- `topics.json`
- `uploader.py`
- `notifier.py`
- `requirements.txt`
- `nixpacks.toml`

### 2. Set Environment Variables

**Wajib (minimal 1 AI API key):**
```
ANTHROPIC_API_KEY = sk-ant-xxxxx
GEMINI_API_KEY    = AIza_xxxxx     ← Direkomendasikan (paling hemat)
GROQ_API_KEY      = gsk_xxxxx
OPENAI_API_KEY    = sk-xxxxx
MISTRAL_API_KEY   = xxxxx
```

**Scheduler config:**
```
SCHEDULER_ENABLED = true
DEFAULT_MODEL     = gemini-2-flash   ← Model untuk auto-run (hemat)
TIMEZONE          = Asia/Jakarta
SCHEDULE_PAGI     = 5                ← Jam 05:00 WIB
SCHEDULE_SIANG    = 12               ← Jam 12:00 WIB
SCHEDULE_MALAM    = 19               ← Jam 19:00 WIB
```

**Notifikasi Telegram (sangat direkomendasikan):**
```
TELEGRAM_BOT_TOKEN  = 123456:ABCdef...   ← dari @BotFather
TELEGRAM_CHAT_ID    = -1001234567890     ← personal atau channel
```

**Upload target:**
```
UPLOAD_TARGET = telegram    ← telegram | youtube | both | none
```

**Untuk Telegram Channel (upload video):**
```
TELEGRAM_CHANNEL_ID = @channelkamu    ← atau angka -100xxxxxxxxxx
```

**Untuk YouTube (opsional — butuh setup OAuth2):**
```
YOUTUBE_CLIENT_ID       = xxxxx.apps.googleusercontent.com
YOUTUBE_CLIENT_SECRET   = GOCSPX-xxxxx
YOUTUBE_REFRESH_TOKEN   = 1//xxxxx
YOUTUBE_DEFAULT_PRIVACY = public
UPLOAD_TARGET           = youtube
```

### 3. Setup YouTube (opsional, hanya jika upload ke YouTube)

Jalankan **sekali** di komputer lokal kamu:
```bash
pip install google-auth-oauthlib
python oauth_setup.py
```
Ikuti instruksi, login via browser, copy refresh_token ke Railway env vars.

### 4. Deploy & Verifikasi

Setelah deploy, test endpoint:
```
GET  https://your-app.railway.app/                  → status
GET  https://your-app.railway.app/scheduler-status  → jadwal aktif
GET  https://your-app.railway.app/topics            → topik hari ini
POST https://your-app.railway.app/auto-run          → trigger manual
     body: {"slot": "pagi"}
```

---

## 🔄 Alur Kerja Otomatis

```
[05:00 / 12:00 / 19:00 WIB]
        ↓
  topics.py → pilih topik berdasarkan hari ke-N (rotasi 7 hari)
        ↓
  /generate-script → AI model generate script 6 slide
        ↓
  /render-video → Edge-TTS + Pollinations + FFmpeg → .mp4
        ↓
  uploader.py → upload ke Telegram Channel / YouTube
        ↓
  notifier.py → kirim notif sukses/gagal ke Telegram
```

---

## 📋 Endpoint Baru

| Method | Endpoint | Fungsi |
|--------|----------|--------|
| GET | `/scheduler-status` | Status scheduler & jadwal berikutnya |
| POST | `/auto-run` | Trigger pipeline manual |
| GET | `/topics` | Lihat topik terdaftar & topik hari ini |

### Contoh POST /auto-run
```json
{
  "slot": "malam",
  "model_key": "gemini-2-flash"
}
```

---

## 🗓️ Rotasi Topik

Sistem memilih topik berdasarkan **hari ke-N dalam setahun**:
- 7 topik per slot × 3 slot = **21 konten unik per siklus**
- Siklus ulang setiap 7 hari
- Edit `topics.json` untuk kustomisasi topik

Atau aktifkan **AI-generated topics** untuk topik yang selalu segar:
```
TOPIC_MODE = ai_generate
```

---

## 💰 Estimasi Biaya Harian

| Item | Biaya |
|------|-------|
| Railway hosting | Gratis (500 jam/bulan) |
| Gemini 2.0 Flash (3 video/hari) | ~$0.001/hari |
| Pollinations AI (gambar) | Gratis |
| Edge-TTS (voice) | Gratis |
| Telegram upload | Gratis |
| YouTube upload | Gratis (quota 6 video/hari) |
| **Total dengan Gemini** | **~$0.03/bulan** |

---

## 🛠️ Kustomisasi

### Ubah jadwal upload
```
SCHEDULE_PAGI  = 7    → jam 07:00
SCHEDULE_SIANG = 13   → jam 13:00
SCHEDULE_MALAM = 20   → jam 20:00
```

### Tambah topik baru
Edit `topics.json`, tambahkan objek di array `pagi`, `siang`, atau `malam`:
```json
{
  "title": "Judul Video Kamu",
  "topic": "Deskripsi topik detail untuk AI scriptwriter",
  "style": "cinematic",
  "voice": "id-ID-ArdiNeural",
  "num_slides": 6,
  "tags": ["tag1", "tag2"],
  "ratio": "16:9"
}
```

### Nonaktifkan scheduler sementara
```
SCHEDULER_ENABLED = false
```
