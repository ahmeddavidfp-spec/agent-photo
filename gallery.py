"""Scraping du site davidmertens.me pour lister les photos d'une galerie.

Un cache TTL mémoire évite de re-scraper 18 fois la même page à chaque ouverture
de menu Telegram (14s → <1s une fois chaud).
"""
import logging
import random
import threading
import time
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from db import already_sent_urls, galerie_last_use
from http_client import safe_get
from timezones import now_local

logger = logging.getLogger(__name__)

# Cache : { (site_url, galerie) -> (expire_ts, list_of_urls) }
_CACHE: dict[tuple[str, str], tuple[float, list[str]]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 600  # 10 minutes


def _canonical(url: str) -> str:
    """URL canonique : sans query string ni fragment.

    Squarespace sert la même photo sous plusieurs URLs (thumbnail,
    lightbox, variantes `?format=500w`/`?format=1000w`). Pour le compte
    et la déduplication, seule la partie path compte.
    """
    return url.split("?", 1)[0].split("#", 1)[0]


def _fetch_live(site_url: str, galerie: str) -> list[str]:
    """Appel réseau réel, sans cache. Dédup par URL canonique."""
    url = f"{site_url.rstrip('/')}/{galerie}"
    for attempt in range(2):
        try:
            r = safe_get(url)
            r.raise_for_status()
            break
        except Exception as e:
            if attempt == 0:
                time.sleep(2)
                continue
            logger.error("Scraping %s échoué : %s", url, e)
            return []

    soup = BeautifulSoup(r.text, "html.parser")
    # dict {canonical_url -> full_url} préserve l'ordre et dédup.
    seen: dict[str, str] = {}
    for img in soup.find_all("img"):
        src = img.get("src")
        if not src:
            continue
        # Squarespace utilise parfois data-src pour le lazy loading
        if src.startswith("data:"):
            src = img.get("data-src") or src
        if not src or src.startswith("data:"):
            continue
        full = src if src.startswith("http") else urljoin(site_url, src)
        canon = _canonical(full)
        if canon not in seen:
            seen[canon] = full
    return list(seen.values())


def fetch_gallery_photos(site_url: str, galerie: str, force_refresh: bool = False) -> list[str]:
    """Renvoie les URLs d'images, avec cache TTL 10min.

    Thread-safe : deux threads concurrents peuvent appeler cette fonction
    sur la même galerie sans double scraping (le lock protège la lecture).
    """
    key = (site_url, galerie)
    now = time.time()

    if not force_refresh:
        with _CACHE_LOCK:
            entry = _CACHE.get(key)
            if entry and entry[0] > now:
                return list(entry[1])  # copie défensive

    fresh = _fetch_live(site_url, galerie)
    if fresh:
        with _CACHE_LOCK:
            _CACHE[key] = (now + _CACHE_TTL, fresh)
    return fresh


def invalidate_cache(site_url: Optional[str] = None, galerie: Optional[str] = None) -> None:
    """Purge le cache. Sans argument : tout. Avec galerie : cette galerie seulement."""
    with _CACHE_LOCK:
        if site_url is None and galerie is None:
            _CACHE.clear()
            return
        keys_to_remove = [k for k in _CACHE if (site_url is None or k[0] == site_url)
                          and (galerie is None or k[1] == galerie)]
        for k in keys_to_remove:
            del _CACHE[k]


def pick_unseen_photo(site_url: str, galerie: str) -> Optional[str]:
    """Choisit une photo jamais publiée dans la galerie.

    La comparaison se fait sur URL canonique pour ne pas resuggérer
    une photo dont une variante (query string) est déjà en DB.
    """
    all_urls = fetch_gallery_photos(site_url, galerie)
    sent_canon = {_canonical(s) for s in already_sent_urls()}
    fresh = [u for u in all_urls if _canonical(u) not in sent_canon]
    if not fresh:
        return None
    return random.choice(fresh)


def counts_for_gallery(site_url: str, galerie: str, sent: set[str]) -> tuple[int, int]:
    """(n_envoyées, n_total) pour affichage dans le menu.

    Compare via URL canonique pour que les compteurs soient corrects
    même si DB contient des variantes de la même photo.
    """
    all_urls = fetch_gallery_photos(site_url, galerie)
    if not all_urls:
        return 0, 0
    sent_canon = {_canonical(s) for s in sent}
    done = sum(1 for u in all_urls if _canonical(u) in sent_canon)
    return done, len(all_urls)


def suggest_next_gallery(galeries: list[str]) -> Optional[str]:
    """Propose la galerie utilisée il y a le plus longtemps (anti-répétition)."""
    if not galeries:
        return None
    now = now_local().replace(tzinfo=None)

    def age(g: str) -> float:
        last = galerie_last_use(g)
        return (now - last).total_seconds() if last else float("inf")

    # La plus ancienne d'abord (age infini = jamais utilisée)
    return max(galeries, key=age)
