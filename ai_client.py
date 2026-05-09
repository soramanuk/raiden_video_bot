"""
ai_client.py — Shared AI Provider Dispatch Layer

Berisi AI_MODELS registry, call_ai(), dan clean_json() yang dipakai bersama
oleh main.py, topics.py, dan modul lain.

Memisahkan logika ini ke sini menghilangkan circular import antara topics.py
dan main.py: topics.py dulu terpaksa `from main import call_ai` yang menyebabkan
seluruh FastAPI app ter-load saat module topic di-import.

ENV VARS:
  ANTHROPIC_API_KEY, GROQ_API_KEY, GEMINI_API_KEY, OPENAI_API_KEY, MISTRAL_API_KEY
"""

import os
import logging

logger = logging.getLogger(__name__)

# ─── Model Registry ───────────────────────────────────────────────────────────

AI_MODELS: dict = {
    "claude-sonnet-4": {
        "provider":       "anthropic",
        "model_id":       "claude-sonnet-4-20250514",
        "label":          "Claude Sonnet 4",
        "provider_label": "Anthropic",
        "tier":           "premium",
        "context":        200000,
    },
    "claude-haiku-4": {
        "provider":       "anthropic",
        "model_id":       "claude-haiku-4-5-20251001",
        "label":          "Claude Haiku 4.5",
        "provider_label": "Anthropic",
        "tier":           "fast",
        "context":        200000,
    },
    "llama-4-scout": {
        "provider":       "groq",
        "model_id":       "meta-llama/llama-4-scout-17b-16e-instruct",
        "label":          "Llama 4 Scout 17B",
        "provider_label": "Groq",
        "tier":           "fast",
        "context":        128000,
    },
    "llama-4-maverick": {
        "provider":       "groq",
        "model_id":       "meta-llama/llama-4-maverick-17b-128e-instruct",
        "label":          "Llama 4 Maverick 17B",
        "provider_label": "Groq",
        "tier":           "premium",
        "context":        128000,
    },
    "llama3-70b-groq": {
        "provider":       "groq",
        "model_id":       "llama3-70b-8192",
        "label":          "Llama 3 70B",
        "provider_label": "Groq",
        "tier":           "balanced",
        "context":        8192,
    },
    "gemini-2-flash": {
        "provider":       "gemini",
        "model_id":       "gemini-2.0-flash",
        "label":          "Gemini 2.0 Flash",
        "provider_label": "Google",
        "tier":           "fast",
        "context":        1000000,
    },
    "gemini-1-5-pro": {
        "provider":       "gemini",
        "model_id":       "gemini-1.5-pro",
        "label":          "Gemini 1.5 Pro",
        "provider_label": "Google",
        "tier":           "premium",
        "context":        2000000,
    },
    "gpt-4.1": {
        "provider":       "openai",
        "model_id":       "gpt-4.1",
        "label":          "GPT-4.1",
        "provider_label": "OpenAI",
        "tier":           "premium",
        "context":        128000,
    },
    "gpt-4o-mini": {
        "provider":       "openai",
        "model_id":       "gpt-4o-mini",
        "label":          "GPT-4o Mini",
        "provider_label": "OpenAI",
        "tier":           "fast",
        "context":        128000,
    },
    "mistral-large": {
        "provider":       "mistral",
        "model_id":       "mistral-large-latest",
        "label":          "Mistral Large",
        "provider_label": "Mistral AI",
        "tier":           "premium",
        "context":        128000,
    },
    "mixtral-8x7b": {
        "provider":       "mistral",
        "model_id":       "open-mixtral-8x7b",
        "label":          "Mixtral 8x7B",
        "provider_label": "Mistral AI",
        "tier":           "balanced",
        "context":        32000,
    },
}

ENV_KEY_MAP: dict = {
    "anthropic": "ANTHROPIC_API_KEY",
    "groq":      "GROQ_API_KEY",
    "gemini":    "GEMINI_API_KEY",
    "openai":    "OPENAI_API_KEY",
    "mistral":   "MISTRAL_API_KEY",
}

PROVIDER_COLORS: dict = {
    "anthropic": "#d97706",
    "groq":      "#16a34a",
    "gemini":    "#2563eb",
    "openai":    "#7c3aed",
    "mistral":   "#dc2626",
}


# ─── JSON Cleaner ─────────────────────────────────────────────────────────────

def clean_json(raw: str) -> str:
    """
    Bersihkan respons AI menjadi JSON murni yang bisa di-parse.

    Menangani semua kasus umum:
      1. JSON bersih tanpa wrapper  → langsung return
      2. Fence ```json ... ``` di awal  → strip fence
      3. Fence ``` ... ``` di awal      → strip fence
      4. Preamble teks + fence di tengah → ambil konten fence
      5. Preamble teks + JSON langsung   → cari kurung buka { atau [
    """
    raw = raw.strip()

    # Case 2 & 3: fence di awal baris
    if raw.startswith("```"):
        parts = raw.split("```")
        if len(parts) >= 2:
            inner = parts[1]
            if inner.startswith("json"):
                inner = inner[4:]
            return inner.strip()

    # Case 4: fence di tengah (setelah preamble teks)
    if "```" in raw:
        parts = raw.split("```")
        # parts: ["preamble\n", "json\n{...}\n", ""]
        # Lewati bagian sebelum fence pertama, ambil isi fence
        for part in parts[1:]:
            candidate = part
            if candidate.startswith("json"):
                candidate = candidate[4:]
            candidate = candidate.strip()
            if candidate.startswith(("{", "[")):
                return candidate

    # Case 1 & 5: JSON murni atau JSON setelah preamble teks biasa
    for i, ch in enumerate(raw):
        if ch in ("{", "["):
            return raw[i:].strip()

    return raw


# ─── AI Dispatch ──────────────────────────────────────────────────────────────

async def call_ai(model_key: str, prompt: str) -> str:
    """
    Panggil AI provider yang sesuai dengan model_key.
    Raise ValueError jika API key tidak diset atau provider tidak dikenal.
    """
    import httpx

    cfg      = AI_MODELS.get(model_key, AI_MODELS["claude-sonnet-4"])
    provider = cfg["provider"]
    model_id = cfg["model_id"]

    if provider == "anthropic":
        import anthropic
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set")
        # FIX F: gunakan AsyncAnthropic agar tidak memblokir event loop uvicorn.
        # anthropic.Anthropic().messages.create() adalah sync blocking call —
        # di dalam async function ini ia akan freeze seluruh event loop selama
        # request berlangsung (bisa 5–30 detik), memblokir semua request lain.
        client = anthropic.AsyncAnthropic(api_key=api_key)
        msg = await client.messages.create(
            model=model_id,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()

    elif provider == "groq":
        api_key = os.getenv("GROQ_API_KEY", "")
        if not api_key:
            raise ValueError("GROQ_API_KEY not set")
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model_id,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 2000,
                    "temperature": 0.7,
                },
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()

    elif provider == "gemini":
        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            raise ValueError("GEMINI_API_KEY not set")
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent?key={api_key}",
                headers={"Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": prompt}]}]},
            )
            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

    elif provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            raise ValueError("OPENAI_API_KEY not set")
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model_id,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 2000,
                },
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()

    elif provider == "mistral":
        api_key = os.getenv("MISTRAL_API_KEY", "")
        if not api_key:
            raise ValueError("MISTRAL_API_KEY not set")
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                "https://api.mistral.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model_id,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 2000,
                },
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()

    raise ValueError(f"Unknown provider: {provider}")
