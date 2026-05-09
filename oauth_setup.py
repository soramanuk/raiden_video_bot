"""
oauth_setup.py — Setup OAuth2 untuk YouTube Upload (jalankan SEKALI saja di lokal)
Setelah dapat refresh_token, tambahkan ke Railway env vars.

CARA PAKAI:
  1. Buka Google Cloud Console: https://console.cloud.google.com
  2. Buat project baru atau pilih yang ada
  3. Enable "YouTube Data API v3"
  4. Buat OAuth2 credentials (type: Desktop App)
  5. Download client_secrets.json
  6. Jalankan: python oauth_setup.py
  7. Login via browser, approve akses
  8. Copy REFRESH_TOKEN yang muncul ke Railway env vars

Ini hanya perlu dijalankan SEKALI. Refresh token berlaku permanen
(kecuali dicabut manual di myaccount.google.com/permissions).
"""

import json
import os
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
SECRETS_FILE = "client_secrets.json"


def main():
    if not os.path.exists(SECRETS_FILE):
        print(f"❌ File '{SECRETS_FILE}' tidak ditemukan!")
        print("Download dari Google Cloud Console → APIs & Services → Credentials")
        print("Pilih credential Desktop App → Download JSON → simpan sebagai client_secrets.json")
        return

    print("🔐 Memulai OAuth2 flow untuk YouTube...")
    print("Browser akan terbuka untuk login. Setelah selesai, kembali ke sini.\n")

    flow = InstalledAppFlow.from_client_secrets_file(SECRETS_FILE, SCOPES)
    creds = flow.run_local_server(port=8080)

    print("\n✅ Berhasil! Tambahkan env vars berikut ke Railway:\n")
    print(f"YOUTUBE_CLIENT_ID     = {creds.client_id}")
    print(f"YOUTUBE_CLIENT_SECRET = {creds.client_secret}")
    print(f"YOUTUBE_REFRESH_TOKEN = {creds.refresh_token}")
    print(f"\nYOUTUBE_DEFAULT_PRIVACY  = public   (atau unlisted/private)")
    print(f"YOUTUBE_DEFAULT_CATEGORY = 22       (22=People & Blogs, 28=Science)")
    print(f"UPLOAD_TARGET            = youtube  (atau telegram/both)")

    # Simpan ke file untuk referensi
    with open("youtube_credentials.json", "w") as f:
        json.dump({
            "client_id":     creds.client_id,
            "client_secret": creds.client_secret,
            "refresh_token": creds.refresh_token,
        }, f, indent=2)
    print(f"\n💾 Juga tersimpan di: youtube_credentials.json (jangan commit ke git!)")


if __name__ == "__main__":
    main()
