"""Client HTTP partagé avec retry automatique et timeouts par défaut."""
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

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


def build_session() -> requests.Session:
    """Session requests avec retry exponentiel sur les erreurs serveur et réseau."""
    session = requests.Session()
    session.headers.update(_DEFAULT_HEADERS)
    retry = Retry(
        total=3,
        connect=1,           # hôte injoignable → 1 seul réessai (échec rapide)
        read=2,
        backoff_factor=0.5,  # 0s, 0.5s, 1s
        status_forcelist=(500, 502, 503, 504, 520, 522, 524),
        allowed_methods=("HEAD", "GET", "POST"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# Session globale réutilisable
SESSION = build_session()


def safe_get(url: str, **kwargs):
    """GET avec timeout par défaut si pas précisé."""
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    return SESSION.get(url, **kwargs)


def safe_post(url: str, **kwargs):
    """POST avec timeout par défaut si pas précisé."""
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    return SESSION.post(url, **kwargs)
