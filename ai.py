"""Génération des captions via OpenAI."""
import logging
import re
import time
from pathlib import Path
from typing import Tuple

from openai import APIConnectionError, APITimeoutError, OpenAI, RateLimitError

from settings import OPENAI_API_KEY, OPENAI_MODEL, load_yaml_config

logger = logging.getLogger(__name__)

# Paramètres retry (uniquement erreurs transitoires)
MAX_ATTEMPTS = 3
BACKOFF_BASE = 2.0  # 2s, 4s, 8s

# Comptes influents (Street/Architecture) — pas trop, pour rester pertinent
SAFE_ACCOUNTS = [
    "archdaily", "architecture_hunter", "streetclassics", "urbanromantix",
    "raw_urbanshots", "bnw_planet", "lensculture", "magnumphotos",
    "somewheremagazine", "artofvisuals", "moodygrams",
    "streetphotographyinternational", "leica_camera", "leica_street",
]

SEPARATOR = "|||"


def _load_prompt_template() -> str:
    """Le prompt est externalisé dans prompts/caption.txt pour pouvoir itérer sans redéployer."""
    path = Path(__file__).parent / "prompts" / "caption.txt"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return DEFAULT_TEMPLATE


DEFAULT_TEMPLATE = """You are David Ahmed, fine art photographer. Analyze this photo of {galerie}.
TASK: Create a high-engagement caption with Hook, Story, Question, and SEO.

CRITICAL RULES:
1. START with English. THEN French.
2. KEEP IT SHORT. Total output under 490 characters (Strict).
3. NO labels (Title:, Caption:, etc.).
4. NO Markdown.
5. SEPARATOR "{sep}" for Alt Text at the end.
6. DO NOT invent technical specs (EXIFs).

CONTENT BLOCKS:
1. THE HOOK: A stop-scrolling title (Journalistic/Emotional).
2. THE STORY: 1 sentence context (EN then FR).
3. THE QUESTION: A short, open-ended question to provoke comments. EN & FR.
4. THE CTA: A short invitation pointing down (e.g. "Full series / Série complète 👇").
5. MENTIONS: Pick the 2 most relevant accounts from: [{accounts}].
6. HASHTAGS (3-Tier Strategy):
   - Tier 1 (Niche)
   - Tier 2 (Specific Location)
   - Tier 3 (Vibe/Style)
   *Max 5 hashtags.*

VISUAL STYLE:
- Use subtle emojis to structure (e.g. 📍 for location in the story, 👇 for CTA).
- Keep it airy (line breaks).

STRUCTURE:
[HOOK EN]
[Story EN]
[Question EN]

[HOOK FR]
[Story FR]
[Question FR]

[Short CTA]
{display_link}
(cc @account1 @account2)
[Hashtags]
{sep}
[Visual description]"""


def split_content(text: str) -> Tuple[str, str]:
    """Sépare la légende du alt text."""
    if SEPARATOR in text:
        caption, alt = text.split(SEPARATOR, 1)
        return caption.strip(), alt.strip()
    return text.strip(), "Art photography by David Ahmed"


def _clean_link_duplicates(caption: str, display_link: str) -> str:
    """Supprime les liens davidahmed.me autres que celui voulu."""
    out = []
    for line in caption.split("\n"):
        line_stripped = line.strip()
        if "davidahmed.me" in line_stripped and display_link not in line_stripped:
            continue
        out.append(line)
    return "\n".join(out).strip()


def generate_caption(image_url: str, galerie: str) -> str:
    """Retourne `caption|||alt`. En cas d'échec, renvoie un fallback propre."""
    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY absente, caption par défaut.")
        return _fallback_caption(galerie)

    config = load_yaml_config()
    base_url = (
        config.get("site_url", "davidahmed.me")
        .replace("https://", "")
        .replace("http://", "")
        .rstrip("/")
    )
    display_link = f"{base_url}/{galerie}"
    template = _load_prompt_template()
    prompt = template.format(
        galerie=galerie,
        sep=SEPARATOR,
        accounts=", ".join(SAFE_ACCOUNTS),
        display_link=display_link,
    )

    client = OpenAI(api_key=OPENAI_API_KEY, timeout=60.0)
    response = None
    last_err: Exception | None = None

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
                max_tokens=700,
                temperature=0.7,
            )
            break
        except (APIConnectionError, APITimeoutError, RateLimitError) as e:
            last_err = e
            wait = BACKOFF_BASE ** attempt
            logger.warning("OpenAI retry %d/%d dans %.1fs : %s",
                           attempt, MAX_ATTEMPTS, wait, e)
            if attempt < MAX_ATTEMPTS:
                time.sleep(wait)
        except Exception as e:
            logger.error("OpenAI erreur non-retryable : %s", e)
            return _fallback_caption(galerie)

    if response is None:
        logger.error("OpenAI a échoué après %d tentatives : %s", MAX_ATTEMPTS, last_err)
        return _fallback_caption(galerie)

    raw = response.choices[0].message.content or ""
    raw = raw.replace("```markdown", "").replace("```", "").strip()
    # Retire les labels résiduels ("Title:", "Hook:", etc.)
    raw = re.sub(
        r"^(Titre|Title|Caption|English|French|Hook|Story|Question)\s*:\s*",
        "",
        raw,
        flags=re.IGNORECASE | re.MULTILINE,
    )

    caption, alt = split_content(raw)
    caption = _clean_link_duplicates(caption, display_link)
    alt = alt or f"Fine art photography of {galerie} by David Ahmed."
    return f"{caption}{SEPARATOR}{alt}"


def _fallback_caption(galerie: str) -> str:
    config = load_yaml_config()
    base = config.get("site_url", "davidahmed.me").replace("https://", "").replace("http://", "").rstrip("/")
    return (
        f"Photo of {galerie}\nPhoto de {galerie}\n\n{base}/{galerie}"
        f"{SEPARATOR}Art photography by David Ahmed"
    )
