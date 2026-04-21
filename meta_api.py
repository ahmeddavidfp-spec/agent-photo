"""Publication Instagram + Threads avec polling du statut au lieu de sleep fixes."""
import logging
import re
import time
from typing import Tuple

from ai import split_content
from http_client import safe_get, safe_post
from settings import (
    FB_API,
    IG_ACCESS_TOKEN,
    IG_CLIENT_SECRET,
    IG_USER_ID,
    TH_API,
    THREADS_ACCESS_TOKEN,
    THREADS_CLIENT_SECRET,
    THREADS_USER_ID,
)

logger = logging.getLogger(__name__)

# Polling max : 90s pour IG (vidéos lentes), 60s pour images suffit largement
POLL_INTERVAL = 3
POLL_MAX_ATTEMPTS = 30


# =========================================================================
# RENOUVELLEMENT DES TOKENS LONGUE DURÉE (60 jours)
# =========================================================================

def renew_threads_token() -> Tuple[bool, object]:
    """Échange le token Threads actuel contre un nouveau valide 60j."""
    if not (THREADS_ACCESS_TOKEN and THREADS_CLIENT_SECRET):
        return False, "THREADS_CLIENT_SECRET ou THREADS_ACCESS_TOKEN manquant"
    try:
        r = safe_get(
            "https://graph.threads.net/access_token",
            params={
                "grant_type": "th_exchange_token",
                "client_secret": THREADS_CLIENT_SECRET,
                "access_token": THREADS_ACCESS_TOKEN,
            },
        )
        res = r.json()
        if "access_token" in res:
            days = res.get("expires_in", 0) // 86400
            return True, (res["access_token"], days)
        return False, res
    except Exception as e:
        logger.error("Renew Threads error: %s", e)
        return False, str(e)


def renew_instagram_token() -> Tuple[bool, object]:
    """Échange le token IG actuel contre un nouveau valide 60j."""
    if not (IG_ACCESS_TOKEN and IG_CLIENT_SECRET):
        return False, "IG_CLIENT_SECRET ou IG_ACCESS_TOKEN manquant"
    try:
        r = safe_get(
            "https://graph.facebook.com/v21.0/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_secret": IG_CLIENT_SECRET,
                "fb_exchange_token": IG_ACCESS_TOKEN,
            },
        )
        res = r.json()
        if "access_token" in res:
            days = res.get("expires_in", 0) // 86400
            return True, (res["access_token"], days)
        return False, res
    except Exception as e:
        logger.error("Renew IG error: %s", e)
        return False, str(e)


def token_status() -> str:
    """Affiche le nombre de jours restants pour chaque token."""
    import datetime as dt

    msg = ["📊 **ETAT**"]
    for label, token, debug_url in [
        ("IG", IG_ACCESS_TOKEN, "https://graph.facebook.com/debug_token"),
        ("TH", THREADS_ACCESS_TOKEN, "https://graph.threads.net/debug_token"),
    ]:
        if not token:
            msg.append(f"❌ {label} : Manquant")
            continue
        try:
            r = safe_get(
                debug_url,
                params={"input_token": token, "access_token": token},
                timeout=5,
            )
            exp = r.json().get("data", {}).get("expires_at")
            if exp:
                days = (dt.datetime.fromtimestamp(exp) - dt.datetime.now()).days
                msg.append(f"✅ {label} : {days}j")
            else:
                msg.append(f"✅ {label} : OK (pas d'expiration retournée)")
        except Exception as e:
            logger.warning("debug_token %s: %s", label, e)
            msg.append(f"⚠️ {label} : Erreur API")
    return "\n".join(msg)


# =========================================================================
# POLLING DU STATUT DES CONTENEURS MEDIA
# =========================================================================

def _poll_container_status(container_id: str, access_token: str, base_url: str) -> bool:
    """Sonde status_code jusqu'à FINISHED, ERROR ou timeout."""
    url = f"https://graph.facebook.com/v21.0/{container_id}" if "facebook" in base_url else f"https://graph.threads.net/v1.0/{container_id}"
    for attempt in range(POLL_MAX_ATTEMPTS):
        try:
            r = safe_get(
                url,
                params={"fields": "status_code,status", "access_token": access_token},
            )
            data = r.json()
            status = data.get("status_code") or data.get("status")
            if status in ("FINISHED", "PUBLISHED"):
                return True
            if status in ("ERROR", "EXPIRED"):
                logger.error("Container %s status=%s data=%s", container_id, status, data)
                return False
        except Exception as e:
            logger.warning("Poll attempt %d failed: %s", attempt + 1, e)
        time.sleep(POLL_INTERVAL)
    logger.error("Container %s polling timeout", container_id)
    return False


# =========================================================================
# PUBLICATION INSTAGRAM
# =========================================================================

def publish_to_instagram(image_url: str, full_text: str) -> Tuple[bool, str]:
    """Publie sur Instagram. Le lien davidahmed.me est remplacé par 'Link in Bio'."""
    if not (IG_ACCESS_TOKEN and IG_USER_ID):
        return False, "IG_ACCESS_TOKEN ou IG_USER_ID manquant"

    caption, _alt = split_content(full_text)
    # IG ne rend pas les liens cliquables dans la caption
    caption = re.sub(r"davidahmed\.me[^\s\n]*", "🔗 Link in Bio for full series", caption)

    try:
        r = safe_post(
            f"{FB_API}{IG_USER_ID}/media",
            data={
                "image_url": image_url,
                "caption": caption,
                "access_token": IG_ACCESS_TOKEN,
            },
        )
        data = r.json()
        container_id = data.get("id")
        if not container_id:
            return False, str(data)

        # Polling au lieu de sleep fixe
        if not _poll_container_status(container_id, IG_ACCESS_TOKEN, FB_API):
            return False, "Container IG pas FINISHED"

        pub = safe_post(
            f"{FB_API}{IG_USER_ID}/media_publish",
            data={"creation_id": container_id, "access_token": IG_ACCESS_TOKEN},
        )
        if pub.status_code != 200:
            return False, str(pub.json())
        return True, "OK"
    except Exception as e:
        logger.exception("publish_to_instagram error")
        return False, str(e)


# =========================================================================
# PUBLICATION THREADS
# =========================================================================

def publish_to_threads(image_url: str, full_text: str) -> Tuple[bool, str]:
    """Publie sur Threads avec image native. Les liens restent cliquables."""
    if not (THREADS_ACCESS_TOKEN and THREADS_USER_ID):
        return False, "THREADS_ACCESS_TOKEN ou THREADS_USER_ID manquant"

    caption, _alt = split_content(full_text)
    # Esthétique : on enlève le "https://" visible, Threads rend quand même cliquable
    caption = caption.replace("https://", "").strip()
    if len(caption) > 495:
        caption = caption[:490] + "..."

    # Squarespace : on demande une version 1000w pour accélérer l'upload Meta
    clean_image = image_url.split("?")[0] + "?format=1000w"

    try:
        r = safe_post(
            f"{TH_API}{THREADS_USER_ID}/threads",
            json={
                "media_type": "IMAGE",
                "image_url": clean_image,
                "text": caption,
                "access_token": THREADS_ACCESS_TOKEN,
            },
        )
        data = r.json()
        container_id = data.get("id")
        if not container_id:
            return False, str(data)

        if not _poll_container_status(container_id, THREADS_ACCESS_TOKEN, TH_API):
            return False, "Container TH pas FINISHED"

        pub = safe_post(
            f"{TH_API}{THREADS_USER_ID}/threads_publish",
            data={"creation_id": container_id, "access_token": THREADS_ACCESS_TOKEN},
        )
        if pub.status_code != 200:
            return False, str(pub.json())
        return True, "OK"
    except Exception as e:
        logger.exception("publish_to_threads error")
        return False, str(e)
