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
import os
import re
import time
from typing import List, Optional, Tuple

from db import get_stored_token, save_token
from http_client import safe_get, safe_post
from settings import PINTEREST_APP_ID, PINTEREST_APP_SECRET, TELEGRAM_TOKEN

logger = logging.getLogger(__name__)

API = "https://api.pinterest.com/v5"
API_SANDBOX = "https://api-sandbox.pinterest.com/v5"   # Trial peut créer des pins ICI
OAUTH_URL = "https://www.pinterest.com/oauth/"
SCOPES = "boards:read,boards:write,pins:read,pins:write"

ACCESS_KEY = "pinterest_access_token"
SANDBOX_ACCESS_KEY = "pinterest_sandbox_access_token"   # jeton OAuth échangé côté sandbox
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


def authorize_url(redirect_uri: str, sandbox: bool = False) -> str:
    from urllib.parse import urlencode
    # sandbox marqué par un préfixe 'sb-' dans le state (renvoyé tel quel par
    # Pinterest) → le callback échange alors le code contre le serveur sandbox.
    state = ("sb-" + make_state()) if sandbox else make_state()
    return OAUTH_URL + "?" + urlencode({
        "client_id": PINTEREST_APP_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
    })


def exchange_code(code: str, redirect_uri: str, sandbox: bool = False) -> bool:
    base = API_SANDBOX if sandbox else API
    try:
        r = safe_post(
            f"{base}/oauth/token",
            headers={"Authorization": _basic_auth(),
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "authorization_code", "code": code,
                  "redirect_uri": redirect_uri},
            timeout=15,
        )
        data = r.json() or {}
        if data.get("access_token"):
            if sandbox:
                save_token(SANDBOX_ACCESS_KEY, data["access_token"])
                logger.info("✅ Pinterest SANDBOX connecté (jeton stocké)")
            else:
                _save_tokens(data)
                logger.info("✅ Pinterest connecté (tokens stockés en DB)")
            return True
        logger.warning("Pinterest exchange refusé (sandbox=%s) : %s", sandbox, str(data)[:200])
    except Exception as e:
        logger.exception("Pinterest exchange KO : %s", e)
    return False


# =========================================================================
# TABLEAUX & ÉPINGLES
# =========================================================================

def _get_or_create_board(display_name: str, base: str = API,
                         headers: Optional[dict] = None) -> Optional[str]:
    """Retourne l'id du tableau 'Street <Ville>' (créé si absent). `base`/`headers`
    permettent de viser la PRODUCTION ou le SANDBOX (cache indexé par base)."""
    h = headers or _headers()
    if not h:
        return None
    name = f"Street {display_name}"
    ck = (base, name)
    if ck in _BOARDS_CACHE:
        return _BOARDS_CACHE[ck]
    try:
        r = safe_get(f"{base}/boards", headers=h, params={"page_size": 100}, timeout=15)
        for b in (r.json() or {}).get("items", []):
            _BOARDS_CACHE[(base, b.get("name", ""))] = b.get("id")
        if ck in _BOARDS_CACHE:
            return _BOARDS_CACHE[ck]
        r = safe_post(f"{base}/boards", headers=h, json={
            "name": name,
            "description": f"Street photography in {display_name} by David Mertens "
                           f"— fine art prints at davidmertens.com",
            "privacy": "PUBLIC",
        }, timeout=15)
        data = r.json() or {}
        if data.get("id"):
            _BOARDS_CACHE[ck] = data["id"]
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


def pin_photos(image_urls: List[str], ig_caption: str,
               sandbox: bool = False) -> Tuple[int, int]:
    """Épingle jusqu'à 4 photos avec le VRAI titre/description SEO (via _pin_meta)
    et le tableau par ville. `sandbox=True` → vise api-sandbox avec le jeton
    sandbox (production bloquée en Trial). La galerie est déduite du LIEN
    davidmertens.com/x présent dans la légende. Retourne (ok, tentées)."""
    base = API_SANDBOX if sandbox else API
    if sandbox:
        tok = _sandbox_token()
        h = ({"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
             if tok else None)
    else:
        h = _headers() if connected() else None
    if not h:
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
    board_id = _get_or_create_board(display_name, base=base, headers=h)
    if not board_id:
        return 0, len(urls)
    title, desc = _pin_meta(ig_caption, display_name, link)
    tag = " SANDBOX" if sandbox else ""
    ok = 0
    for u in urls:
        try:
            r = safe_post(f"{base}/pins", headers=h, json={
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
                logger.warning("Pinterest%s : pin KO (%s) : %s", tag, u[:60], r.text[:150])
        except Exception as e:
            logger.warning("Pinterest%s : pin exception : %s", tag, e)
    logger.info("Pinterest%s : %d/%d épingles créées → « Street %s »", tag, ok, len(urls), display_name)
    return ok, len(urls)


def pin_best_effort(image_urls: List[str], ig_caption: str) -> Tuple[int, int, str]:
    """Épingle en PRODUCTION d'abord (marchera dès l'accès Standard obtenu) ; si le
    Trial bloque (0 épingle), repli automatique sur le SANDBOX. Titre/description
    SEO identiques dans les deux cas. Retourne (ok, tentées, environnement)."""
    if connected():
        ok, total = pin_photos(image_urls, ig_caption, sandbox=False)
        if ok > 0:
            return ok, total, "production"
    if sandbox_connected():
        ok, total = pin_photos(image_urls, ig_caption, sandbox=True)
        return ok, total, "sandbox"
    return 0, 0, "aucun"


def diagnose(sample_url: str = "") -> str:
    """Teste la création d'un tableau PUIS d'une épingle (avec `sample_url`) et
    renvoie les statuts/erreurs Pinterest exacts. Pinpointe pourquoi une épingle
    échoue : permission, image injoignable par Pinterest, payload…"""
    h = _headers()
    if not h:
        return "pas de token Pinterest"
    parts = []
    # get-or-create : réutilise le tableau existant (gère le doublon "Street Test")
    board_id = _get_or_create_board("Test")
    parts.append("tableau: OK" if board_id else "tableau: ÉCHEC (création/lecture)")
    if board_id and sample_url:
        pin_url = sample_url.split("?")[0] + "?format=1500w"
        try:
            r = safe_post(f"{API}/pins", headers=h, json={
                "board_id": board_id, "title": "Test", "description": "Test",
                "media_source": {"source_type": "image_url", "url": pin_url},
            }, timeout=25)
            parts.append(f"épingle: HTTP {r.status_code}")
            if r.status_code not in (200, 201):
                parts.append(str(r.text)[:260])
            parts.append(f"(image testée: {pin_url[:70]})")
        except Exception as e:
            parts.append(f"épingle KO: {str(e)[:120]}")
    return " | ".join(parts)


def _sandbox_token() -> str:
    """Jeton pour le SANDBOX : le jeton sandbox obtenu par OAuth (clé dédiée),
    sinon PINTEREST_SANDBOX_TOKEN. PAS le jeton de production (rejeté : 401)."""
    return (get_stored_token(SANDBOX_ACCESS_KEY)
            or os.environ.get("PINTEREST_SANDBOX_TOKEN", "").strip() or "")


def sandbox_connected() -> bool:
    return bool(get_stored_token(SANDBOX_ACCESS_KEY)
                or os.environ.get("PINTEREST_SANDBOX_TOKEN", "").strip())


def test_sandbox(image_url: str) -> Tuple[bool, str]:
    """Crée un tableau + une VRAIE épingle dans le SANDBOX Pinterest (où le Trial
    autorise l'écriture) → sert à filmer la démo pour l'accès Standard.
    Retourne (ok, message lisible)."""
    tok = _sandbox_token()
    if not tok:
        return False, ("Aucun jeton sandbox. Génère un « sandbox access token » dans "
                       "ton dashboard Pinterest et mets-le dans Render sous "
                       "PINTEREST_SANDBOX_TOKEN.")
    h = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    name = "Street Test Sandbox"
    board_id = None
    try:
        r = safe_get(f"{API_SANDBOX}/boards", headers=h, params={"page_size": 50}, timeout=15)
        if r.status_code == 200:
            for b in (r.json() or {}).get("items", []):
                if b.get("name") == name:
                    board_id = b.get("id")
        if not board_id:
            r = safe_post(f"{API_SANDBOX}/boards", headers=h,
                          json={"name": name, "privacy": "PUBLIC"}, timeout=15)
            if r.status_code in (200, 201):
                board_id = (r.json() or {}).get("id")
            else:
                return False, f"Tableau sandbox refusé : HTTP {r.status_code} — {str(r.text)[:200]}"
    except Exception as e:
        return False, f"Tableau sandbox KO : {str(e)[:150]}"
    if not board_id:
        return False, "Tableau sandbox non créé."
    try:
        r = safe_post(f"{API_SANDBOX}/pins", headers=h, json={
            "board_id": board_id, "title": "Street photography test",
            "description": "Test — Agent Photo",
            "media_source": {"source_type": "image_url",
                             "url": image_url.split("?")[0] + "?format=1500w"},
        }, timeout=25)
        if r.status_code in (200, 201) and (r.json() or {}).get("id"):
            return True, "✅ Épingle créée dans le SANDBOX ! Filme cet écran (+ le tableau) pour ta démo Pinterest."
        return False, f"Épingle sandbox refusée : HTTP {r.status_code} — {str(r.text)[:220]}"
    except Exception as e:
        return False, f"Épingle sandbox KO : {str(e)[:150]}"


# =========================================================================
# ROUTES FLASK (connexion OAuth)
# =========================================================================

def register_pinterest(app, redirect_uri: str) -> None:
    from flask import jsonify, request

    @app.route("/pinterest/callback")
    def pinterest_callback():
        state = request.args.get("state", "")
        code = request.args.get("code", "")
        sandbox = state.startswith("sb-")       # marqueur posé par authorize_url
        real_state = state[3:] if sandbox else state
        if not check_state(real_state):
            return "State invalide ou expiré — relance la connexion depuis le Studio.", 403
        if not code:
            return "Autorisation refusée côté Pinterest.", 400
        ok = exchange_code(code, redirect_uri, sandbox=sandbox)
        if ok:
            return ("<div style='font:17px sans-serif;padding:40px;text-align:center'>"
                    "✅ <b>Pinterest connecté !</b><br>Tu peux fermer cette page — "
                    "les épingles partiront automatiquement à chaque publication.</div>")
        return "Échec de l'échange de tokens — regarde les logs Render.", 500

    logger.info("Pinterest : routes enregistrées (callback %s)", redirect_uri)
