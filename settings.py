"""Centralisation de la configuration et des variables d'environnement."""
import logging
import os
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# --- Secrets / IDs (toujours via variables d'environnement) ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
# Secret optionnel qu'on fait contrôler par Telegram sur chaque webhook
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
# Filtre optionnel : seul ce chat ID peut piloter le bot (anti-abus)
# Accepte ALLOWED_CHAT_ID (v2) ou TELEGRAM_CHAT_ID (nom historique).
ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID", "")

IG_ACCESS_TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
# Accepte IG_USER_ID (v2) ou INSTAGRAM_BUSINESS_ID (nom historique Render).
IG_USER_ID = os.environ.get("IG_USER_ID") or os.environ.get("INSTAGRAM_BUSINESS_ID", "")
IG_CLIENT_SECRET = os.environ.get("IG_CLIENT_SECRET", "")

THREADS_ACCESS_TOKEN = os.environ.get("THREADS_ACCESS_TOKEN", "")
THREADS_USER_ID = os.environ.get("THREADS_USER_ID", "")
THREADS_CLIENT_SECRET = os.environ.get("THREADS_CLIENT_SECRET", "")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")

# --- Anthropic (pass 2 writing, optionnel) ---
# Si ANTHROPIC_API_KEY est présente, on utilise Claude pour écrire la caption
# à partir de la description générée par OpenAI (pass 1 vision).
# Sinon on retombe sur OpenAI pour les deux passes.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

# Jeton optionnel pour sécuriser l'endpoint /cron/*
CRON_SECRET = os.environ.get("CRON_SECRET", "")

# --- URLs API ---
TG_API = "https://api.telegram.org/bot"
FB_API = "https://graph.facebook.com/v21.0/"
TH_API = "https://graph.threads.net/v1.0/"

# --- Base de données ---
# Sur Render le disque persistant est monté dans /data
DB_PATH = "/data/photos.db" if Path("/data").exists() else "photos.db"

# --- Fuseau horaire ---
LOCAL_TZ = "Europe/Brussels"


def load_yaml_config(path: str = "config.yaml") -> dict:
    """Charge config.yaml avec valeurs par défaut sûres."""
    default = {
        "site_url": "https://www.davidmertens.me",
        "galeries": [],
        "custom_hashtag": "",
    }
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return {**default, **data}
    except FileNotFoundError:
        logger.warning("config.yaml absent, valeurs par défaut utilisées.")
        return default
    except Exception as e:
        logger.error("Erreur chargement config.yaml : %s", e)
        return default
