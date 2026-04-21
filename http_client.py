"""Client HTTP partagé avec retry automatique et timeouts par défaut."""
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# Timeout par défaut pour TOUS les appels réseau (en secondes)
DEFAULT_TIMEOUT = 15


def build_session() -> requests.Session:
    """Session requests avec retry exponentiel sur les erreurs serveur et réseau."""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1.0,  # 0s, 1s, 2s, 4s
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
