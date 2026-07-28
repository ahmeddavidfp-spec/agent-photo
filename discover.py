"""Détection automatique des nouvelles galeries sur le site Squarespace.

Lit le sitemap.xml, garde les pages de 1er niveau (slug unique), écarte celles
déjà connues (config + déjà découvertes + denylist), et confirme qu'une
candidate est bien une galerie via le nombre de photos (≥ MIN_IMAGES).

Seules les galeries confirmées (≥ MIN_IMAGES photos) sont enregistrées
'pending' et retournées pour alerte Telegram. Les slugs sous le seuil ne sont
jamais persistés (un fetch partiel/throttlé ne peut donc pas blacklister une
vraie galerie) ; l'utilisateur peut ensuite 'ignorer' une galerie proposée.
"""
import logging
import re
from urllib.parse import urlparse

from http_client import safe_get

logger = logging.getLogger(__name__)

MIN_IMAGES = 15  # ≥15 photos CDN = galerie (calibré : non-galeries ≤ 3)


def scan_new_galleries() -> list:
    """Retourne les nouvelles galeries détectées : [{slug, name, count}]."""
    from settings import (NON_GALLERY_SLUGS, auto_gallery_assets,
                          load_yaml_config)
    from db import known_discovered, record_discovered_gallery
    from gallery import fetch_gallery_photos

    config = load_yaml_config()
    site = config["site_url"]
    known = (set(config.get("galeries") or [])
             | {s.lower() for s in NON_GALLERY_SLUGS}
             | known_discovered())

    try:
        r = safe_get(site.rstrip("/") + "/sitemap.xml", timeout=(8, 25))
        r.raise_for_status()
    except Exception as e:
        logger.warning("Scan galeries : sitemap inaccessible : %s", e)
        return []

    slugs = set()
    for loc in re.findall(r"<loc>([^<]+)</loc>", r.text):
        parts = [p for p in urlparse(loc).path.split("/") if p]
        if len(parts) == 1:                    # page de 1er niveau uniquement
            slugs.add(parts[0].lower())

    candidates = sorted(slugs - known)
    if candidates:
        logger.info("Scan galeries : %d candidate(s) à vérifier : %s",
                    len(candidates), candidates)
    found = []
    for slug in candidates:
        try:
            n = len(fetch_gallery_photos(site, slug, force_refresh=True))
        except Exception:
            n = 0
        # On n'enregistre QUE les galeries confirmées (≥ MIN_IMAGES). Un slug
        # sous le seuil (vraie page sans photos OU fetch partiel/throttlé) n'est
        # jamais persisté : au pire il est re-testé au prochain scan (1 requête).
        # Ainsi un hoquet réseau ne peut jamais blacklister une vraie galerie.
        if n >= MIN_IMAGES and record_discovered_gallery(slug):  # nouvelle → pending
            name, _ = auto_gallery_assets(slug)
            found.append({"slug": slug, "name": name, "count": n})
            logger.info("🆕 Galerie détectée : %s (%d photos)", slug, n)
    return found
