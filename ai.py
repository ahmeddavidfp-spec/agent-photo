"""Génération des captions en pipeline 2 passes.

Pass 1 — OpenAI Vision (gpt-4o) : description factuelle de la photo.
Pass 2 — Claude Sonnet si ANTHROPIC_API_KEY, sinon OpenAI : caption bilingue + alt.

Si un pass plante, on retombe proprement sur un fallback pour ne jamais bloquer la publication.
"""
import logging
import re
import time
from pathlib import Path
from typing import List, Optional, Tuple

from openai import APIConnectionError, APITimeoutError, OpenAI, RateLimitError

from settings import (
    ANTHROPIC_API_KEY,
    ANTHROPIC_MODEL,
    OPENAI_API_KEY,
    OPENAI_MODEL,
    load_yaml_config,
)

logger = logging.getLogger(__name__)

# Paramètres retry (uniquement erreurs transitoires réseau / rate limit)
MAX_ATTEMPTS = 3
BACKOFF_BASE = 2.0  # 2s, 4s, 8s

SEPARATOR = "|||"
PROMPTS_DIR = Path(__file__).parent / "prompts"

# Fallback global si aucun safe_mentions dans config.yaml
DEFAULT_SAFE_ACCOUNTS = [
    "archdaily", "architecture_hunter", "streetclassics", "urbanromantix",
    "raw_urbanshots", "bnw_planet", "lensculture", "magnumphotos",
    "somewheremagazine", "artofvisuals", "moodygrams",
    "streetphotographyinternational", "leica_camera", "leica_street",
]

DEFAULT_HASHTAGS = (
    "#streetphotography #spicollective #street_perfection #burnmyeye "
    "#bnw_demand #leicaphotography #davidmertens #leica #bnw"
)


# =============================================================================
# UTILITAIRES
# =============================================================================

def _load_prompt(name: str) -> str:
    """Charge un prompt depuis prompts/<name>."""
    path = PROMPTS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Prompt manquant : {path}")
    return path.read_text(encoding="utf-8")


def split_content(text: str) -> Tuple[str, str]:
    """Sépare la légende du alt text."""
    if SEPARATOR in text:
        caption, alt = text.split(SEPARATOR, 1)
        return caption.strip(), alt.strip()
    return text.strip(), "Fine art photography by David Mertens."


def _clean_link_duplicates(caption: str, display_link: str) -> str:
    """Supprime les liens du site qui ne pointent pas vers `display_link`."""
    out = []
    for line in caption.split("\n"):
        line_stripped = line.strip()
        if ("davidmertens.me" in line_stripped or "davidahmed.me" in line_stripped) \
                and display_link not in line_stripped:
            continue
        out.append(line)
    return "\n".join(out).strip()


def _strip_labels(text: str) -> str:
    """Supprime les labels résiduels de type 'Hook:' 'EN:' etc."""
    cleaned = text.replace("```markdown", "").replace("```", "").strip()
    cleaned = re.sub(
        r"^\s*(Titre|Title|Caption|English|French|Français|Hook|Story|Question|EN|FR|Alt\s*Text|Alt)\s*:\s*",
        "",
        cleaned,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    return cleaned


def _resolve_gallery_assets(galerie: str, config: dict) -> Tuple[List[str], List[str]]:
    """Retourne (hashtags, mentions) pour une galerie donnée.

    Priorité : config.per_gallery[galerie] > config global > défauts du code.
    """
    per_gallery = (config.get("per_gallery") or {}).get(galerie, {}) or {}

    # Hashtags
    tags = per_gallery.get("hashtags")
    if not tags:
        global_tags = config.get("hashtags", DEFAULT_HASHTAGS)
        if isinstance(global_tags, str):
            tags = [t for t in global_tags.split() if t.startswith("#")]
        elif isinstance(global_tags, list):
            tags = list(global_tags)
        else:
            tags = DEFAULT_HASHTAGS.split()

    # Mentions
    mentions = per_gallery.get("mentions")
    if not mentions:
        mentions = config.get("safe_mentions") or DEFAULT_SAFE_ACCOUNTS

    return list(tags), list(mentions)


def _voice_examples(config: dict) -> str:
    """Extrait les exemples de voix du config, ou chaîne vide."""
    raw = config.get("voice_examples") or ""
    return raw.strip() or "(aucun exemple fourni — reste factuel, court, cinématographique)"


def _gallery_context(galerie: str, config: dict) -> str:
    """Contexte curatorial spécifique à la galerie (NY/Tokyo/Milan...).

    Injecté dans le prompt Pass 2 pour donner à Claude la vision artistique
    de David sur cette ville précise. Si pas de contexte pour la galerie,
    retourne un message neutre qui ne perturbe pas le prompt.
    """
    contexts = config.get("gallery_context") or {}
    text = contexts.get(galerie)
    if not text:
        return "(aucun contexte spécifique pour cette galerie — appuie-toi sur la description factuelle)"
    return text.strip()


def _display_name(galerie: str, config: dict) -> str:
    """Nom propre pour l'affichage (ex. 'new-york' -> 'New York').

    Utilise `gallery_names` du config si présent, sinon fallback
    heuristique : remplace les tirets par des espaces + title-case.
    """
    names = config.get("gallery_names") or {}
    if galerie in names and names[galerie]:
        return str(names[galerie])
    return galerie.replace("-", " ").replace("_", " ").title()


# =============================================================================
# PASS 1 — DESCRIPTION FACTUELLE (OpenAI Vision)
# =============================================================================

def _describe_image(image_url: str, display_name: str) -> Optional[str]:
    """Retourne une description factuelle ~80 mots. None si échec complet."""
    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY absente, pas de description.")
        return None

    try:
        prompt = _load_prompt("describe.txt").format(galerie=display_name)
    except FileNotFoundError as e:
        logger.error("Prompt describe introuvable : %s", e)
        return None

    client = OpenAI(api_key=OPENAI_API_KEY, timeout=60.0)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url",
                         "image_url": {"url": image_url, "detail": "high"}},
                    ],
                }],
                max_tokens=300,
                temperature=0.3,  # plus factuel
            )
            desc = (response.choices[0].message.content or "").strip()
            # Nettoyage léger
            desc = desc.replace("```", "").strip()
            if desc:
                logger.info("Pass 1 (describe) OK : %d chars", len(desc))
                return desc
            logger.warning("Pass 1 retourne vide (tentative %d)", attempt)
        except (APIConnectionError, APITimeoutError, RateLimitError) as e:
            wait = BACKOFF_BASE ** attempt
            logger.warning("Pass 1 retry %d/%d dans %.1fs : %s",
                           attempt, MAX_ATTEMPTS, wait, e)
            if attempt < MAX_ATTEMPTS:
                time.sleep(wait)
        except Exception as e:
            logger.error("Pass 1 erreur non-retryable : %s", e)
            return None

    logger.error("Pass 1 a échoué après %d tentatives.", MAX_ATTEMPTS)
    return None


# =============================================================================
# PASS 2 — CAPTION BILINGUE (Claude en priorité, fallback OpenAI)
# =============================================================================

def _format_top_performers(limit: int = 3) -> str:
    """Pull les meilleures captions historiques et formate pour le prompt.

    Renvoie un message "pas encore de data" si la DB est vide (graceful
    degradation pour les premiers posts, avant que la boucle ne soit amorcée).
    """
    try:
        from db import top_performers
    except ImportError:
        return "(no data yet — this is the first learning cycle)"
    try:
        tops = top_performers(limit=limit)
    except Exception as e:
        logger.warning("top_performers failed: %s", e)
        return "(no data yet — this is the first learning cycle)"
    if not tops:
        return "(no data yet — this is the first learning cycle)"
    lines = []
    for i, p in enumerate(tops, 1):
        cap = (p.get("caption") or "").strip().replace("\n", " ")
        if len(cap) > 400:
            cap = cap[:400] + "..."
        score = p.get("score", 0)
        galerie = p.get("galerie", "?")
        lines.append(f'{i}. [{galerie} · score {score:.1f}] "{cap}"')
    return "\n".join(lines)


def _build_caption_prompt(
    description: str, galerie: str, display_name: str,
    display_link: str, config: dict,
) -> str:
    """Assemble le prompt pass 2 à partir du template et du config.

    `galerie` est le slug (pour la résolution config.per_gallery).
    `display_name` est le nom affichable (pour le texte de la caption).
    """
    template = _load_prompt("caption.txt")
    tags, mentions = _resolve_gallery_assets(galerie, config)
    return template.format(
        galerie=display_name,
        sep=SEPARATOR,
        display_link=display_link,
        description=description,
        voice_examples=_voice_examples(config),
        gallery_context=_gallery_context(galerie, config),
        top_performers=_format_top_performers(limit=3),
        accounts=", ".join(mentions),
        hashtags=" ".join(tags),
    )


def _write_caption_with_claude(prompt: str) -> Optional[str]:
    """Pass 2 via Claude. Retourne le texte brut ou None en cas d'échec."""
    if not ANTHROPIC_API_KEY:
        return None
    try:
        from anthropic import Anthropic, APIConnectionError as AntConn, APITimeoutError as AntTime, RateLimitError as AntRate  # noqa
    except ImportError:
        logger.warning("anthropic non installé, fallback OpenAI pour pass 2.")
        return None

    client = Anthropic(api_key=ANTHROPIC_API_KEY, timeout=60.0)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            msg = client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=1500,
                temperature=0.7,
                messages=[{"role": "user", "content": prompt}],
            )
            # msg.content est une liste de blocs (text blocks)
            parts = [
                getattr(b, "text", "") for b in (msg.content or [])
                if getattr(b, "type", "") == "text"
            ]
            out = "\n".join(p for p in parts if p).strip()
            if out:
                logger.info("Pass 2 (Claude) OK : %d chars", len(out))
                return out
            logger.warning("Pass 2 Claude retourne vide (tentative %d)", attempt)
        except (AntConn, AntTime, AntRate) as e:  # type: ignore[misc]
            wait = BACKOFF_BASE ** attempt
            logger.warning("Pass 2 Claude retry %d/%d dans %.1fs : %s",
                           attempt, MAX_ATTEMPTS, wait, e)
            if attempt < MAX_ATTEMPTS:
                time.sleep(wait)
        except Exception as e:
            logger.error("Pass 2 Claude erreur non-retryable : %s", e)
            return None

    return None


def _write_caption_with_openai(prompt: str) -> Optional[str]:
    """Pass 2 via OpenAI (texte seul, pas d'image). Fallback si Claude absent."""
    if not OPENAI_API_KEY:
        return None
    client = OpenAI(api_key=OPENAI_API_KEY, timeout=60.0)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1500,
                temperature=0.7,
            )
            out = (response.choices[0].message.content or "").strip()
            if out:
                logger.info("Pass 2 (OpenAI) OK : %d chars", len(out))
                return out
        except (APIConnectionError, APITimeoutError, RateLimitError) as e:
            wait = BACKOFF_BASE ** attempt
            logger.warning("Pass 2 OpenAI retry %d/%d dans %.1fs : %s",
                           attempt, MAX_ATTEMPTS, wait, e)
            if attempt < MAX_ATTEMPTS:
                time.sleep(wait)
        except Exception as e:
            logger.error("Pass 2 OpenAI erreur non-retryable : %s", e)
            return None

    return None


# =============================================================================
# ORCHESTRATION
# =============================================================================

def generate_caption(image_url: str, galerie: str) -> str:
    """Retourne `caption|||alt`. Fallback propre si tout casse."""
    config = load_yaml_config()
    base_url = (
        config.get("site_url", "davidmertens.me")
        .replace("https://", "")
        .replace("http://", "")
        .replace("www.", "")  # lien affiché sans www (cohérent avec voice_examples)
        .rstrip("/")
    )
    display_link = f"{base_url}/{galerie}"
    display_name = _display_name(galerie, config)

    # --- Pass 1 : description factuelle
    description = _describe_image(image_url, display_name)
    if not description:
        logger.warning("Pas de description, fallback caption.")
        return _fallback_caption(display_name, display_link)

    # --- Pass 2 : écriture de la caption
    prompt = _build_caption_prompt(
        description, galerie, display_name, display_link, config,
    )

    raw = _write_caption_with_claude(prompt)
    if not raw:
        logger.info("Pass 2 : fallback sur OpenAI.")
        raw = _write_caption_with_openai(prompt)

    if not raw:
        logger.error("Pass 2 a totalement échoué, fallback caption.")
        return _fallback_caption(display_name, display_link)

    # Nettoyage post-génération
    raw = _strip_labels(raw)
    caption, alt = split_content(raw)
    caption = _clean_link_duplicates(caption, display_link)
    alt = alt or f"Fine art photography of {galerie} by David Mertens."
    return f"{caption}{SEPARATOR}{alt}"


def _fallback_caption(display_name: str, display_link: str) -> str:
    """Caption minimale mais publiable en cas d'échec total de l'IA."""
    return (
        f"Photo of {display_name}\n"
        f"Photo de {display_name}\n\n"
        f"Full series / Série complète 👇\n"
        f"{display_link}"
        f"{SEPARATOR}"
        f"Fine art photography by David Mertens from the {display_name} gallery."
    )
