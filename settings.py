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

# URL publique de l'app (Render la fournit via RENDER_EXTERNAL_URL).
# Utilisée par la Mini App Telegram (boutons web_app).
APP_BASE_URL = os.environ.get(
    "RENDER_EXTERNAL_URL", "https://agent-photo.onrender.com"
).rstrip("/")

# Page Facebook (déclinaisons natives) — les DEUX vides = fonctionnalité inerte.
# FB_PAGE_ACCESS_TOKEN = token de PAGE longue durée avec pages_manage_posts.
FB_PAGE_ID = os.environ.get("FB_PAGE_ID", "")
FB_PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "")

IG_ACCESS_TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
# Accepte IG_USER_ID (v2) ou INSTAGRAM_BUSINESS_ID (nom historique Render).
IG_USER_ID = os.environ.get("IG_USER_ID") or os.environ.get("INSTAGRAM_BUSINESS_ID", "")
IG_CLIENT_SECRET = os.environ.get("IG_CLIENT_SECRET", "")
# App ID Meta — NON secret (visible dans toutes les URLs du dashboard FB Developers).
# Accepte IG_APP_ID ou FB_APP_ID (même valeur côté Meta).
IG_APP_ID = os.environ.get("IG_APP_ID") or os.environ.get("FB_APP_ID", "")

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
# ARCHITECTURE DB (voir doc §10) : le disque persistant /data (NFS) gèle par
# intermittence au niveau OS → la DB VIVE habite le disque LOCAL de l'instance
# (rapide, fiable), et une SAUVEGARDE part vers /data en arrière-plan (thread
# isolé : si /data gèle, seule la sauvegarde attend — jamais le bot).
# Au boot, db.py restaure depuis la sauvegarde /data la plus récente.
# Trade-off assumé : un crash peut perdre les toutes dernières minutes d'écritures.
if Path("/data").exists():
    DB_PATH = "/tmp/photos_live.db"            # DB vive : disque local
    BACKUP_DB_PATH = "/data/photos_backup.db"  # sauvegarde persistante
    # Sources de restauration au boot, par ordre de préférence (les deux
    # derniers = anciens emplacements, pour la première migration).
    RESTORE_SOURCES = ["/data/photos_backup.db", "/data/photos_v2.db", "/data/photos.db"]
else:
    DB_PATH = "photos_v2.db"                   # dev local : inchangé
    BACKUP_DB_PATH = None
    RESTORE_SOURCES = []

# --- Fuseau horaire ---
LOCAL_TZ = "Europe/Brussels"


def load_yaml_config(path: str = "config.yaml") -> dict:
    """Charge config.yaml avec valeurs par défaut sûres."""
    default = {
        "site_url": "https://www.davidmertens.com",
        "galeries": [],
        "custom_hashtag": "",
        "carousel": {"enabled": False, "count": 5},
        "reel": {"enabled": False, "photos_color": 4, "photos_bw": 4,
                 "seconds": 2.5, "tagline": "", "motion": True},
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
