"""Publication Instagram + Threads avec polling du statut au lieu de sleep fixes."""
import logging
import time
from typing import Tuple

from ai import split_content
from db import get_stored_token, save_token
from http_client import safe_get, safe_post
from settings import (
    FB_API,
    IG_ACCESS_TOKEN,
    IG_APP_ID,
    IG_CLIENT_SECRET,
    IG_USER_ID,
    TH_API,
    THREADS_ACCESS_TOKEN,
    THREADS_CLIENT_SECRET,
    THREADS_USER_ID,
)

logger = logging.getLogger(__name__)


# =========================================================================
# RESOLUTION DES TOKENS (DB d'abord, env var en fallback/bootstrap)
# =========================================================================
# Les env vars sont le POINT DE DEPART (bootstrap initial). Dès qu'un refresh
# réussit, le nouveau token est stocké en DB (/data/photos.db), qui devient
# la source de vérité. Avantage : pas besoin d'une API Render ni de redéploiement
# pour que le nouveau token soit utilisé — l'effet est immédiat.

IG_TOKEN_KEY = "ig_access_token"
TH_TOKEN_KEY = "th_access_token"


def get_ig_token() -> str:
    return get_stored_token(IG_TOKEN_KEY) or IG_ACCESS_TOKEN


def get_th_token() -> str:
    return get_stored_token(TH_TOKEN_KEY) or THREADS_ACCESS_TOKEN

# Polling :
# - IG renvoie status_code=FINISHED fiable → on peut poller jusqu'à 2 min.
# - Threads renvoie souvent status=None (bug Meta), mais le conteneur EST
#   prêt en ~20s. On poll court (20s) puis on tente la publish quand même.
POLL_INTERVAL = 3
POLL_MAX_ATTEMPTS_IG = 40  # 120s
POLL_MAX_ATTEMPTS_TH = 8   # 24s avant tentative optimiste


# =========================================================================
# RENOUVELLEMENT DES TOKENS LONGUE DURÉE (60 jours)
# =========================================================================

def renew_threads_token() -> Tuple[bool, object]:
    """Rafraîchit le token Threads long-lived contre un nouveau valide 60j.

    Deux endpoints distincts côté Meta Threads :
    - /access_token?grant_type=th_exchange_token : short-lived → long-lived (initial).
    - /refresh_access_token?grant_type=th_refresh_token : long-lived → long-lived (refresh).
    Le token sur Render est déjà long-lived (issu du flux initial), donc on utilise
    l'endpoint refresh. Celui-ci n'exige PAS de client_secret.
    """
    current = get_th_token()
    if not current:
        return False, "THREADS_ACCESS_TOKEN manquant"
    try:
        r = safe_get(
            "https://graph.threads.net/refresh_access_token",
            params={
                "grant_type": "th_refresh_token",
                "access_token": current,
            },
        )
        res = r.json()
        if "access_token" in res:
            new_token = res["access_token"]
            days = res.get("expires_in", 0) // 86400
            # Persistance en DB → prise en compte immédiate sans redéploiement
            save_token(TH_TOKEN_KEY, new_token)
            logger.info("TH token persisté en DB (expire dans %dj)", days)
            return True, (new_token, days)
        return False, res
    except Exception as e:
        logger.error("Renew Threads error: %s", e)
        return False, str(e)


def renew_instagram_token() -> Tuple[bool, object]:
    """Échange le token IG actuel contre un nouveau valide 60j.

    Meta Graph API exige client_id (App ID) ET client_secret pour l'endpoint
    fb_exchange_token. L'App ID est public (visible dans les URLs du dashboard
    Meta for Developers), pas un secret.
    """
    current = get_ig_token()
    missing = [name for name, val in [
        ("IG_ACCESS_TOKEN", current),
        ("IG_CLIENT_SECRET", IG_CLIENT_SECRET),
        ("IG_APP_ID", IG_APP_ID),
    ] if not val]
    if missing:
        return False, f"Variables manquantes : {', '.join(missing)}"
    try:
        r = safe_get(
            "https://graph.facebook.com/v21.0/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": IG_APP_ID,
                "client_secret": IG_CLIENT_SECRET,
                "fb_exchange_token": current,
            },
        )
        res = r.json()
        if "access_token" in res:
            new_token = res["access_token"]
            days = res.get("expires_in", 0) // 86400
            # Persistance en DB → prise en compte immédiate sans redéploiement
            save_token(IG_TOKEN_KEY, new_token)
            logger.info("IG token persisté en DB (expire dans %dj)", days)
            return True, (new_token, days)
        return False, res
    except Exception as e:
        logger.error("Renew IG error: %s", e)
        return False, str(e)


# Cache in-memory pour éviter de marteler Meta /debug_token à chaque rendu
# de menu Telegram. TTL court : 10 minutes. La valeur peut être périmée de
# quelques minutes sans conséquence (juste l'affichage visuel des "jours
# restants" du menu). Les alertes quotidiennes passent le force_refresh=True.
_TOKEN_DAYS_CACHE: dict = {}
_TOKEN_DAYS_TTL = 600  # 10 min


def token_days_left(platform: str, force_refresh: bool = False):
    """Retourne le nombre de jours restants pour IG ou TH, ou None si indéterminé.

    - Cache mémoire 10 min par défaut (évite le blocage menu Telegram).
    - `force_refresh=True` ignore le cache (utilisé par l'alerte quotidienne).
    - Timeout dur de 3s par appel + 1 seule retentative max.
    """
    import datetime as dt
    now_ts = time.time()

    if not force_refresh:
        cached = _TOKEN_DAYS_CACHE.get(platform)
        if cached and now_ts - cached[0] < _TOKEN_DAYS_TTL:
            return cached[1]

    if platform == "IG":
        token = get_ig_token()
        debug_url = "https://graph.facebook.com/debug_token"
    elif platform == "TH":
        token = get_th_token()
        debug_url = "https://graph.threads.net/debug_token"
    else:
        return None
    if not token:
        return None

    # Appel direct avec timeout court : on préfère renvoyer None rapidement
    # (le menu affichera "IG ?") plutôt que bloquer 27s sur un retry.
    import requests as _requests
    try:
        r = _requests.get(
            debug_url,
            params={"input_token": token, "access_token": token},
            timeout=3,
        )
        exp = (r.json() or {}).get("data", {}).get("expires_at")
        if not exp:
            _TOKEN_DAYS_CACHE[platform] = (now_ts, None)
            return None
        days = (dt.datetime.fromtimestamp(exp) - dt.datetime.now()).days
        _TOKEN_DAYS_CACHE[platform] = (now_ts, days)
        return days
    except Exception as e:
        logger.warning("token_days_left %s: %s", platform, e)
        # Renvoie la dernière valeur connue si on en a une, plutôt que None.
        cached = _TOKEN_DAYS_CACHE.get(platform)
        return cached[1] if cached else None


def token_status() -> str:
    """Affiche le nombre de jours restants + date du dernier refresh DB."""
    import datetime as dt
    from db import token_last_update

    msg = ["📊 **ETAT**"]
    for label, token, debug_url, store_key in [
        ("IG", get_ig_token(), "https://graph.facebook.com/debug_token", IG_TOKEN_KEY),
        ("TH", get_th_token(), "https://graph.threads.net/debug_token", TH_TOKEN_KEY),
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
            last = token_last_update(store_key)
            suffix = f" (refresh DB : {last})" if last else " (bootstrap env)"
            if exp:
                days = (dt.datetime.fromtimestamp(exp) - dt.datetime.now()).days
                msg.append(f"✅ {label} : {days}j{suffix}")
            else:
                msg.append(f"✅ {label} : OK (pas d'expiration retournée){suffix}")
        except Exception as e:
            logger.warning("debug_token %s: %s", label, e)
            msg.append(f"⚠️ {label} : Erreur API")
    return "\n".join(msg)


# =========================================================================
# POLLING DU STATUT DES CONTENEURS MEDIA
# =========================================================================

def _poll_container_status(
    container_id: str, access_token: str, base_url: str,
    max_attempts: int = POLL_MAX_ATTEMPTS_IG,
) -> str:
    """Sonde le statut du conteneur.

    Retourne :
      - "finished" : Meta a confirmé FINISHED / PUBLISHED → publish safe
      - "error"    : Meta a explicitement renvoyé ERROR / EXPIRED → ne pas publier
      - "timeout"  : pas de réponse claire après POLL_MAX_ATTEMPTS
                     → tenter la publish quand même (Meta est parfois lent
                     à mettre à jour, comportement historique v1).
    """
    is_instagram = "facebook" in base_url
    url = (
        f"https://graph.facebook.com/v21.0/{container_id}" if is_instagram
        else f"https://graph.threads.net/v1.0/{container_id}"
    )
    # Instagram renvoie `status_code`, Threads renvoie `status`.
    fields = "status_code,status" if is_instagram else "status,error_message"
    last_status = None
    last_data: dict = {}
    for attempt in range(max_attempts):
        try:
            r = safe_get(
                url,
                params={"fields": fields, "access_token": access_token},
            )
            data = r.json() or {}
            last_data = data
            status = data.get("status_code") or data.get("status")
            last_status = status
            if status in ("FINISHED", "PUBLISHED"):
                return "finished"
            if status in ("ERROR", "EXPIRED"):
                logger.error("Container %s status=%s data=%s", container_id, status, data)
                return "error"
            # Log discret toutes les 5 tentatives + dump brut la 1re fois
            if attempt == 0:
                logger.info("Container %s first poll response: %s", container_id, data)
            elif (attempt + 1) % 10 == 0:
                logger.info("Container %s still %s after %ds (payload=%s)",
                            container_id, status, (attempt + 1) * POLL_INTERVAL, data)
        except Exception as e:
            logger.warning("Poll attempt %d failed: %s", attempt + 1, e)
        time.sleep(POLL_INTERVAL)
    logger.warning(
        "Container %s polling timeout (last_status=%s last_data=%s) — tentative publish quand même",
        container_id, last_status, last_data,
    )
    return "timeout"


# =========================================================================
# PUBLICATION INSTAGRAM
# =========================================================================

def _extract_hashtag_block(caption: str) -> tuple[str, str]:
    """Sépare la caption de son bloc de hashtags final.

    Retourne (caption_sans_hashtags, hashtags_str).
    Le bloc hashtags = lignes de fin où tous les tokens commencent par '#'.
    """
    lines = caption.rstrip().split("\n")
    split_idx = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if not stripped:
            split_idx = i
            continue
        tokens = stripped.split()
        if all(t.startswith("#") for t in tokens):
            split_idx = i
        else:
            break
    body = "\n".join(lines[:split_idx]).rstrip()
    hashtags = " ".join(
        t for line in lines[split_idx:] for t in line.split() if t.startswith("#")
    )
    return body, hashtags


def _post_ig_comment(media_id: str, text: str, token: str) -> None:
    """Poste le texte comme premier commentaire sur un post IG.

    Silencieux en cas d'échec — les hashtags en commentaire sont un bonus,
    pas un prérequis à la publication.
    """
    if not text:
        return
    try:
        r = safe_post(
            f"{FB_API}{media_id}/comments",
            data={"message": text, "access_token": token},
        )
        if r.status_code == 200:
            logger.info("IG first comment posted (%d chars)", len(text))
        else:
            logger.warning("IG first comment failed: %s", r.text[:200])
    except Exception as e:
        logger.warning("_post_ig_comment error: %s", e)


def publish_to_instagram(image_url: str, full_text: str) -> Tuple[bool, str]:
    """Publie sur Instagram. Hashtags postés en premier commentaire."""
    ig_token = get_ig_token()
    if not (ig_token and IG_USER_ID):
        return False, "IG_ACCESS_TOKEN ou IG_USER_ID manquant"

    caption, _alt = split_content(full_text)
    # IG ne rend pas les liens cliquables, mais on affiche l'URL en clair
    # (lisible/recopiable, cohérent avec Threads). On retire juste le schéma.
    caption = caption.replace("https://", "").replace("http://", "")
    # Extraire les hashtags → premier commentaire (caption plus propre)
    caption_clean, hashtag_block = _extract_hashtag_block(caption)

    try:
        r = safe_post(
            f"{FB_API}{IG_USER_ID}/media",
            data={
                "image_url": image_url,
                "caption": caption_clean,
                "access_token": ig_token,
            },
        )
        data = r.json()
        container_id = data.get("id")
        if not container_id:
            return False, str(data)

        # Polling. Si timeout, on tente la publish quand même.
        poll = _poll_container_status(container_id, ig_token, FB_API)
        if poll == "error":
            return False, "Container IG ERROR côté Meta"

        pub = safe_post(
            f"{FB_API}{IG_USER_ID}/media_publish",
            data={"creation_id": container_id, "access_token": ig_token},
        )
        if pub.status_code != 200:
            err = str(pub.json())
            if poll == "timeout":
                return False, f"Container IG pas prêt (timeout polling) : {err}"
            return False, err

        media_id = (pub.json() or {}).get("id", "")
        # Hashtags en premier commentaire — non-bloquant
        if media_id and hashtag_block:
            _post_ig_comment(media_id, hashtag_block, ig_token)
        return True, media_id
    except Exception as e:
        logger.exception("publish_to_instagram error")
        return False, str(e)


def publish_carousel_to_instagram(image_urls: list, full_text: str) -> Tuple[bool, str]:
    """Publie un carrousel Instagram (2 à 10 photos).

    Flux Meta : 1 conteneur enfant par photo (is_carousel_item=true) → 1
    conteneur parent media_type=CAROUSEL(children=...) → media_publish.
    Hashtags postés en 1er commentaire (comme le post simple). Si <2 URLs
    valides, bascule automatiquement sur le post simple.
    """
    ig_token = get_ig_token()
    if not (ig_token and IG_USER_ID):
        return False, "IG_ACCESS_TOKEN ou IG_USER_ID manquant"

    urls = [u for u in (image_urls or []) if u][:10]  # IG max 10
    if len(urls) < 2:
        # Pas de quoi faire un carrousel → post simple sur la couverture
        return publish_to_instagram(urls[0] if urls else "", full_text)

    caption, _alt = split_content(full_text)
    # URL affichée en clair (voir publish_to_instagram)
    caption = caption.replace("https://", "").replace("http://", "")
    caption_clean, hashtag_block = _extract_hashtag_block(caption)

    try:
        # 1) Conteneurs enfants (images prêtes quasi-instantanément côté Meta)
        child_ids: list[str] = []
        for url in urls:
            r = safe_post(
                f"{FB_API}{IG_USER_ID}/media",
                data={
                    "image_url": url,
                    "is_carousel_item": "true",
                    "access_token": ig_token,
                },
            )
            cid = (r.json() or {}).get("id")
            if cid:
                child_ids.append(cid)
            else:
                logger.warning("Carrousel : enfant KO (%s) : %s", url[:60], r.text[:150])
        if len(child_ids) < 2:
            # Pas assez d'enfants valides → repli sur un post simple (couverture)
            logger.warning("Carrousel : <2 enfants valides, repli post simple")
            return publish_to_instagram(urls[0], full_text)

        # 2) Conteneur parent CAROUSEL
        r = safe_post(
            f"{FB_API}{IG_USER_ID}/media",
            data={
                "media_type": "CAROUSEL",
                "children": ",".join(child_ids),
                "caption": caption_clean,
                "access_token": ig_token,
            },
        )
        parent_id = (r.json() or {}).get("id")
        if not parent_id:
            return False, str(r.json())

        # 3) Polling parent puis publish
        poll = _poll_container_status(parent_id, ig_token, FB_API)
        if poll == "error":
            return False, "Container carrousel IG ERROR côté Meta"

        pub = safe_post(
            f"{FB_API}{IG_USER_ID}/media_publish",
            data={"creation_id": parent_id, "access_token": ig_token},
        )
        if pub.status_code != 200:
            err = str(pub.json())
            if poll == "timeout":
                return False, f"Carrousel IG pas prêt (timeout polling) : {err}"
            return False, err

        media_id = (pub.json() or {}).get("id", "")
        if media_id and hashtag_block:
            _post_ig_comment(media_id, hashtag_block, ig_token)
        logger.info("Carrousel IG publié : %d photos, media_id=%s", len(child_ids), media_id)
        return True, media_id
    except Exception as e:
        logger.exception("publish_carousel_to_instagram error")
        return False, str(e)


# =========================================================================
# PUBLICATION THREADS
# =========================================================================

def publish_to_threads(image_url: str, full_text: str) -> Tuple[bool, str]:
    """Publie sur Threads avec image native. Les liens restent cliquables."""
    th_token = get_th_token()
    if not (th_token and THREADS_USER_ID):
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
                "access_token": th_token,
            },
        )
        data = r.json()
        container_id = data.get("id")
        if not container_id:
            return False, str(data)

        poll = _poll_container_status(
            container_id, th_token, TH_API,
            max_attempts=POLL_MAX_ATTEMPTS_TH,
        )
        if poll == "error":
            return False, "Container TH ERROR côté Meta"

        pub = safe_post(
            f"{TH_API}{THREADS_USER_ID}/threads_publish",
            data={"creation_id": container_id, "access_token": th_token},
        )
        if pub.status_code != 200:
            err = str(pub.json())
            if poll == "timeout":
                return False, f"Container TH pas prêt (timeout polling) : {err}"
            return False, err
        media_id = (pub.json() or {}).get("id", "")
        return True, media_id
    except Exception as e:
        logger.exception("publish_to_threads error")
        return False, str(e)


# =========================================================================
# INSIGHTS / METRICS — collecte 24h+ après publication
# =========================================================================

def fetch_ig_insights(media_id: str) -> dict:
    """Récupère les metrics IG d'un post publié.

    Meta Graph renvoie likes/comments via /{id}?fields, et reach/saved via
    /{id}/insights. On combine les deux dans un seul dict plat.
    """
    if not media_id:
        return {}
    token = get_ig_token()
    out: dict = {}
    try:
        # likes + comments (fields de base)
        r1 = safe_get(
            f"{FB_API}{media_id}",
            params={"fields": "like_count,comments_count", "access_token": token},
            timeout=10,
        )
        out.update(r1.json() or {})
        # insights : reach, saved, impressions (selon permissions)
        r2 = safe_get(
            f"{FB_API}{media_id}/insights",
            params={"metric": "reach,saved,impressions", "access_token": token},
            timeout=10,
        )
        for item in (r2.json() or {}).get("data", []):
            name = item.get("name")
            values = item.get("values") or []
            if name and values:
                out[name] = values[0].get("value", 0)
    except Exception as e:
        logger.warning("fetch_ig_insights(%s) failed: %s", media_id, e)
    return out


def fetch_th_insights(media_id: str) -> dict:
    """Récupère les metrics Threads d'un post publié.

    Threads Insights : views, likes, replies, reposts, quotes.
    """
    if not media_id:
        return {}
    token = get_th_token()
    out: dict = {}
    try:
        r = safe_get(
            f"{TH_API}{media_id}/insights",
            params={
                "metric": "views,likes,replies,reposts,quotes",
                "access_token": token,
            },
            timeout=10,
        )
        for item in (r.json() or {}).get("data", []):
            name = item.get("name")
            values = item.get("values") or []
            if name and values:
                out[name] = values[0].get("value", 0)
    except Exception as e:
        logger.warning("fetch_th_insights(%s) failed: %s", media_id, e)
    return out
