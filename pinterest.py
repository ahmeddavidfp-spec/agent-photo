"""Pinterest — épinglage automatique des publications (API v5).

Philosophie : Pinterest est un moteur de recherche visuel où une épingle vit
des années et pointe vers le site (tirages). Chaque publication IG crée donc
2-4 épingles SEO dans un tableau par ville ("Street London", ...).

Configuration :
- PINTEREST_APP_ID / PINTEREST_APP_SECRET en env (Render) — sinon inerte.
- Autorisation OAuth : 1 clic depuis le Studio (onglet État) → callback
  /pinterest/callback → tokens stockés en DB (access ~30j + refresh ~1 an,
  rafraîchis automatiquement).

Sécurité : le `state` OAuth est signé HMAC (token du bot) + horodaté (15 min)
→ personne ne peut connecter un autre compte Pinterest à ton bot.
"""
import base64
import hashlib
import hmac
import logging
import re
import time
from typing import List, Optional, Tuple

from db import get_stored_token, save_token
from http_client import safe_get, safe_post
from settings import PINTEREST_APP_ID, PINTEREST_APP_SECRET, TELEGRAM_TOKEN

logger = logging.getLogger(__name__)

API = "https://api.pinterest.com/v5"
OAUTH_URL = "https://www.pinterest.com/oauth/"
SCOPES = "boards:read,boards:write,pins:read,pins:write"

ACCESS_KEY = "pinterest_access_token"
REFRESH_KEY = "pinterest_refresh_token"
EXPIRY_KEY = "pinterest_token_expiry"   # timestamp unix (str)

_MAX_PINS_PER_POST = 4
_STATE_MAX_AGE = 15 * 60

# Cache mémoire {nom_de_tableau: board_id}
_BOARDS_CACHE: dict = {}


# =========================================================================
# CONFIG / TOKENS
# =========================================================================

def app_configured() -> bool:
    return bool(PINTEREST_APP_ID and PINTEREST_APP_SECRET)


def connected() -> bool:
    return app_configured() and bool(get_stored_token(ACCESS_KEY))


def _basic_auth() -> str:
    raw = f"{PINTEREST_APP_ID}:{PINTEREST_APP_SECRET}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _save_tokens(data: dict) -> None:
    if data.get("access_token"):
        save_token(ACCESS_KEY, data["access_token"])
        save_token(EXPIRY_KEY, str(int(time.time()) + int(data.get("expires_in", 2592000))))
    if data.get("refresh_token"):
        save_token(REFRESH_KEY, data["refresh_token"])


def _refresh_if_needed() -> Optional[str]:
    """Retourne un access token valide (rafraîchi si <24h de vie restante)."""
    token = get_stored_token(ACCESS_KEY)
    if not token:
        return None
    try:
        expiry = int(get_stored_token(EXPIRY_KEY) or "0")
    except ValueError:
        expiry = 0
    if expiry - time.time() > 24 * 3600:
        return token
    refresh = get_stored_token(REFRESH_KEY)
    if not refresh:
        return token  # on tente avec l'existant
    try:
        r = safe_post(
            f"{API}/oauth/token",
            headers={"Authorization": _basic_auth(),
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "refresh_token": refresh},
            timeout=15,
        )
        data = r.json() or {}
        if data.get("access_token"):
            _save_tokens(data)
            logger.info("Pinterest : access token rafraîchi")
            return data["access_token"]
        logger.warning("Pinterest refresh refusé : %s", str(data)[:150])
    except Exception as e:
        logger.warning("Pinterest refresh KO : %s", e)
    return token


def _headers() -> Optional[dict]:
    token = _refresh_if_needed()
    if not token:
        return None
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# =========================================================================
# OAUTH (state signé HMAC — pas de session serveur nécessaire)
# =========================================================================

def make_state() -> str:
    ts = str(int(time.time()))
    sig = hmac.new(TELEGRAM_TOKEN.encode(), ts.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{ts}.{sig}"


def check_state(state: str) -> bool:
    try:
        ts, sig = state.split(".", 1)
        good = hmac.new(TELEGRAM_TOKEN.encode(), ts.encode(), hashlib.sha256).hexdigest()[:24]
        return hmac.compare_digest(good, sig) and time.time() - int(ts) < _STATE_MAX_AGE
    except Exception:
        return False


def authorize_url(redirect_uri: str) -> str:
    from urllib.parse import urlencode
    return OAUTH_URL + "?" + urlencode({
        "client_id": PINTEREST_APP_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "state": make_state(),
    })


def exchange_code(code: str, redirect_uri: str) -> bool:
    try:
        r = safe_post(
            f"{API}/oauth/token",
            headers={"Authorization": _basic_auth(),
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "authorization_code", "code": code,
                  "redirect_uri": redirect_uri},
            timeout=15,
        )
        data = r.json() or {}
        if data.get("access_token"):
            _save_tokens(data)
            logger.info("✅ Pinterest connecté (tokens stockés en DB)")
            return True
        logger.warning("Pinterest exchange refusé : %s", str(data)[:200])
    except Exception as e:
        logger.exception("Pinterest exchange KO : %s", e)
    return False


# =========================================================================
# TABLEAUX & ÉPINGLES
# =========================================================================

def _get_or_create_board(display_name: str) -> Optional[str]:
    """Retourne l'id du tableau 'Street <Ville>' (créé si absent)."""
    name = f"Street {display_name}"
    if name in _BOARDS_CACHE:
        return _BOARDS_CACHE[name]
    h = _headers()
    if not h:
        return None
    try:
        r = safe_get(f"{API}/boards", headers=h, params={"page_size": 100}, timeout=15)
        for b in (r.json() or {}).get("items", []):
            _BOARDS_CACHE[b.get("name", "")] = b.get("id")
        if name in _BOARDS_CACHE:
            return _BOARDS_CACHE[name]
        r = safe_post(f"{API}/boards", headers=h, json={
            "name": name,
            "description": f"Street photography in {display_name} by David Mertens "
                           f"— fine art prints at davidmertens.com",
            "privacy": "PUBLIC",
        }, timeout=15)
        data = r.json() or {}
        if data.get("id"):
            _BOARDS_CACHE[name] = data["id"]
            logger.info("Pinterest : tableau créé « %s »", name)
            return data["id"]
        logger.warning("Pinterest : création tableau KO : %s", str(data)[:150])
    except Exception as e:
        logger.warning("Pinterest : tableaux KO : %s", e)
    return None


def _pin_meta(ig_caption: str, display_name: str, link: str) -> Tuple[str, str]:
    """(titre, description) SEO — IA d'abord, gabarit déterministe sinon."""
    try:
        from ai import decline_caption
        out = decline_caption("pinterest", ig_caption)
        if out:
            title, desc = "", ""
            for line in out.splitlines():
                if line.upper().startswith("TITLE:"):
                    title = line.split(":", 1)[1].strip()
                elif line.upper().startswith("DESCRIPTION:"):
                    desc = line.split(":", 1)[1].strip()
            if title and desc:
                return title[:95], desc[:480]
    except Exception as e:
        logger.warning("Pinterest : déclinaison IA KO : %s", e)
    return (
        f"{display_name} Street Photography — Fine Art by David Mertens"[:95],
        f"Street photography in {display_name} by David Mertens. "
        f"Available as fine art prints — see the full series at {link}."[:480],
    )


def pin_photos(image_urls: List[str], ig_caption: str) -> Tuple[int, int]:
    """Épingle jusqu'à 4 photos d'une publication. Retourne (ok, tentées).

    La galerie est déduite du LIEN présent dans la légende (davidmertens.com/x)
    — robuste, indépendant des URLs CDN. Sans lien → tableau générique."""
    if not connected():
        return 0, 0
    urls = [u for u in (image_urls or []) if u][:_MAX_PINS_PER_POST]
    if not urls:
        return 0, 0
    m = re.search(r"davidmertens\.com/([a-z0-9_-]+)", ig_caption or "")
    slug = m.group(1) if m else ""
    from settings import load_yaml_config
    names = (load_yaml_config().get("gallery_names") or {})
    display_name = names.get(slug) or (slug.replace("-", " ").title() if slug else "Photography")
    link = f"https://davidmertens.com/{slug}" if slug else "https://davidmertens.com"
    board_id = _get_or_create_board(display_name)
    if not board_id:
        return 0, len(urls)
    title, desc = _pin_meta(ig_caption, display_name, link)
    h = _headers()
    if not h:
        return 0, len(urls)
    ok = 0
    for u in urls:
        try:
            r = safe_post(f"{API}/pins", headers=h, json={
                "board_id": board_id,
                "title": title,
                "description": desc,
                "link": link,
                "media_source": {"source_type": "image_url",
                                 "url": u.split("?")[0] + "?format=1500w"},
            }, timeout=20)
            if r.status_code in (200, 201) and (r.json() or {}).get("id"):
                ok += 1
            else:
                logger.warning("Pinterest : pin KO (%s) : %s", u[:60], r.text[:150])
        except Exception as e:
            logger.warning("Pinterest : pin exception : %s", e)
    logger.info("Pinterest : %d/%d épingles créées → « Street %s »", ok, len(urls), display_name)
    return ok, len(urls)


# =========================================================================
# ROUTES FLASK (connexion OAuth)
# =========================================================================

def register_pinterest(app, redirect_uri: str) -> None:
    from flask import jsonify, request

    @app.route("/pinterest/callback")
    def pinterest_callback():
        state = request.args.get("state", "")
        code = request.args.get("code", "")
        if not check_state(state):
            return "State invalide ou expiré — relance la connexion depuis le Studio.", 403
        if not code:
            return "Autorisation refusée côté Pinterest.", 400
        ok = exchange_code(code, redirect_uri)
        if ok:
            return ("<div style='font:17px sans-serif;padding:40px;text-align:center'>"
                    "✅ <b>Pinterest connecté !</b><br>Tu peux fermer cette page — "
                    "les épingles partiront automatiquement à chaque publication.</div>")
        return "Échec de l'échange de tokens — regarde les logs Render.", 500

    logger.info("Pinterest : routes enregistrées (callback %s)", redirect_uri)
