"""Client HTTP partagé avec retry automatique et timeouts par défaut."""
import logging
import os
import threading
import time
from urllib.parse import quote, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# --- Relais optionnel (contourne un blocage d'IP côté Squarespace) -----------
# Si FETCH_PROXY est défini (URL d'un Worker Cloudflare relais), les requêtes
# vers les hôtes listés (le site) passent par ce relais dont l'IP n'est pas
# bloquée. Inactif si la variable n'existe pas → comportement direct habituel.
_FETCH_PROXY = os.environ.get("FETCH_PROXY", "").strip().rstrip("/")
_FETCH_PROXY_HOSTS = {
    h.strip().lower()
    for h in os.environ.get(
        "FETCH_PROXY_HOSTS", "www.davidmertens.com,davidmertens.com"
    ).split(",")
    if h.strip()
}

# Timeout par défaut : (connect, read). Le connect court fait échouer VITE quand
# l'hôte est injoignable (au lieu de bloquer 15-25 s et d'empiler les threads).
DEFAULT_TIMEOUT = (8, 20)

# User-Agent d'un vrai navigateur. Squarespace filtre `python-requests` comme un
# bot (→ blocage/timeout de l'IP). Une signature navigateur évite le flag.
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_DEFAULT_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}


def build_session(retry: "Retry") -> requests.Session:
    session = requests.Session()
    session.headers.update(_DEFAULT_HEADERS)
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# GET/HEAD sont idempotents → on réessaie sur erreurs serveur ET réseau.
SESSION = build_session(Retry(
    total=3, connect=1, read=2, backoff_factor=0.5,
    status_forcelist=(500, 502, 503, 504, 520, 522, 524),
    allowed_methods=("HEAD", "GET"),
    raise_on_status=False,
))

# POST est NON idempotent (publier = créer un post !). On ne réessaie QUE sur
# échec de CONNEXION (la requête n'a jamais atteint le serveur → sûr de rejouer).
# JAMAIS sur read-timeout ni 5xx : le serveur a pu traiter la requête et une
# 2e tentative publierait en double (cas confirmé sur FB /photos, Telegram…).
_SESSION_POST = build_session(Retry(
    total=1, connect=1, read=0, status=0,
    status_forcelist=(), allowed_methods=frozenset(["POST"]),
    raise_on_status=False,
))


# =============================================================================
# DISJONCTEUR PAR HÔTE (circuit breaker)
# -----------------------------------------------------------------------------
# Quand un hôte (ex. www.davidmertens.com bloqué par Squarespace) refuse les
# connexions, réessayer en boucle : (1) empile des threads, (2) PROLONGE le
# blocage d'IP côté Squarespace. Après _CB_THRESHOLD échecs de connexion, on
# « ouvre » le disjoncteur : pendant _CB_COOLDOWN, tout appel vers cet hôte
# échoue INSTANTANÉMENT sans toucher le réseau. Une seule requête sonde ensuite
# la reprise. N'affecte QUE l'hôte fautif (Meta/Telegram/OpenAI intacts).
# =============================================================================
_CB_LOCK = threading.Lock()
_CB_FAILS: dict = {}        # host -> nb d'échecs de connexion consécutifs
_CB_OPEN_UNTIL: dict = {}   # host -> timestamp de fin de cooldown
_CB_THRESHOLD = 3
_CB_COOLDOWN = 900          # 15 min


class HostCircuitOpen(requests.exceptions.ConnectionError):
    """Disjoncteur ouvert pour cet hôte : on n'essaie même pas de se connecter."""


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def _apply_proxy(url: str) -> str:
    """Route via le relais si configuré ET si l'hôte cible est bloqué."""
    if not _FETCH_PROXY:
        return url
    if _host(url) in _FETCH_PROXY_HOSTS:
        return f"{_FETCH_PROXY}/?url={quote(url, safe='')}"
    return url


def _cb_guard(host: str) -> None:
    if not host:
        return
    with _CB_LOCK:
        until = _CB_OPEN_UNTIL.get(host, 0)
    if until and time.time() < until:
        raise HostCircuitOpen(
            f"{host} injoignable (disjoncteur ouvert, reprise dans "
            f"{int(until - time.time())}s)")


def _cb_success(host: str) -> None:
    if not host:
        return
    with _CB_LOCK:
        # pop des DEUX (pas de `or` : il court-circuiterait et laisserait
        # _CB_OPEN_UNTIL en place quand _CB_FAILS est non vide).
        had_fails = _CB_FAILS.pop(host, None) is not None
        was_open = _CB_OPEN_UNTIL.pop(host, None) is not None
        if had_fails or was_open:
            logger.info("Disjoncteur %s refermé (connexion OK).", host)


def _cb_failure(host: str) -> None:
    if not host:
        return
    with _CB_LOCK:
        n = _CB_FAILS.get(host, 0) + 1
        _CB_FAILS[host] = n
        if n >= _CB_THRESHOLD and host not in _CB_OPEN_UNTIL:
            _CB_OPEN_UNTIL[host] = time.time() + _CB_COOLDOWN
            logger.warning(
                "🔌 Disjoncteur OUVERT pour %s (%d échecs) : pause %ds pour ne "
                "pas prolonger le blocage.", host, n, _CB_COOLDOWN)


def _request(method: str, url: str, **kwargs):
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    url = _apply_proxy(url)          # relais si configuré
    host = _host(url)                # disjoncteur suit l'hôte réellement contacté
    _cb_guard(host)  # lève HostCircuitOpen si le disjoncteur est ouvert
    sess = _SESSION_POST if method.upper() == "POST" else SESSION
    try:
        r = sess.request(method, url, **kwargs)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        _cb_failure(host)   # connexion refusée OU hôte qui accepte puis pend
        raise
    _cb_success(host)
    return r


def safe_get(url: str, **kwargs):
    """GET avec timeout par défaut + disjoncteur par hôte."""
    return _request("GET", url, **kwargs)


def safe_post(url: str, **kwargs):
    """POST avec timeout par défaut + disjoncteur par hôte."""
    return _request("POST", url, **kwargs)


def circuit_state() -> dict:
    """État du disjoncteur : {open: {host: secondes_restantes}, fails: {host: n}}."""
    now = time.time()
    with _CB_LOCK:
        return {
            "open": {h: int(u - now) for h, u in _CB_OPEN_UNTIL.items() if u > now},
            "fails": dict(_CB_FAILS),
        }


def proxy_state() -> dict:
    """État du relais : {enabled, url, hosts}. Sert au diagnostic (/diag)."""
    return {
        "enabled": bool(_FETCH_PROXY),
        "url": _FETCH_PROXY,
        "hosts": sorted(_FETCH_PROXY_HOSTS),
    }


def probe(url: str, timeout=(5, 10)) -> tuple:
    """Sonde RÉELLE (ignore l'état ouvert du disjoncteur) pour tester la reprise.
    Met quand même à jour le disjoncteur : un succès le referme. Retourne
    (ok: bool, info: dict) avec status/ms ou error/ms."""
    url = _apply_proxy(url)          # teste le vrai chemin (relais inclus)
    host = _host(url)
    t0 = time.time()
    try:
        r = SESSION.request("GET", url, timeout=timeout)
        _cb_success(host)
        return True, {"status": r.status_code, "ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        _cb_failure(host)
        return False, {"error": type(e).__name__, "ms": int((time.time() - t0) * 1000)}
