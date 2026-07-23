"""Copilote — Analyseur : lit post_metrics et compose le rapport hebdomadaire.

Données réelles disponibles (table post_metrics) : platform (IG/TH), galerie,
published_at (UTC), metrics_json (reach/saves/likes/comments selon plateforme).
Les Reels (Chemin B, publiés à la main) ne sont PAS tracés — le rapport le dit.

Principe d'HONNÊTETÉ statistique : avec peu de posts par semaine, les échantillons
sont bruités. On affiche les chiffres bruts, on ne tire de "tendance" que si
l'écart est net ET l'échantillon suffisant, et on marque le reste "à confirmer".
"""
import datetime as dt
import json
import logging
from typing import List, Optional

from db import _engagement_score, connection
from timezones import TZ, from_utc_str

logger = logging.getLogger(__name__)

_FR_DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
_FR_MONTHS = ["", "janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.",
              "août", "sept.", "oct.", "nov.", "déc."]


def _load_rows() -> List[dict]:
    """Publications avec insights collectés, normalisées + datées en local."""
    with connection() as conn:
        rows = conn.execute(
            "SELECT platform, galerie, caption, published_at, metrics_json "
            "FROM post_metrics WHERE metrics_json IS NOT NULL"
        ).fetchall()
    out = []
    for platform, galerie, caption, published_at, mj in rows:
        try:
            m = json.loads(mj) if mj else {}
        except (json.JSONDecodeError, TypeError):
            m = {}
        try:
            when = from_utc_str(published_at).astimezone(TZ)
        except Exception:
            continue
        out.append({
            "platform": platform or "?",
            "galerie": (galerie or "inconnue"),
            "caption": caption or "",
            "when": when,
            "reach": int(m.get("reach") or m.get("views") or 0),
            "saves": int(m.get("saved") or m.get("reposts") or 0),
            "likes": int(m.get("like_count") or m.get("likes") or 0),
            "comments": int(m.get("comments_count") or m.get("replies") or 0),
            "eng": _engagement_score(m),
        })
    return out


def _pending_count() -> int:
    with connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM post_metrics WHERE metrics_json IS NULL"
        ).fetchone()[0]


def _avg(nums) -> float:
    nums = [n for n in nums if n is not None]
    return sum(nums) / len(nums) if nums else 0.0


def _fmt(n) -> str:
    if isinstance(n, float) and not n.is_integer():
        return f"{n:.0f}"
    return f"{int(n):,}".replace(",", " ")


def _delta(cur: float, prev: float) -> str:
    if prev <= 0:
        return ""
    pct = round((cur - prev) / prev * 100)
    if pct == 0:
        return " (stable)"
    return f" ({'+' if pct > 0 else ''}{pct}% vs S-1)"


def build_report(now: Optional[dt.datetime] = None) -> str:
    """Compose le rapport hebdo (texte Telegram Markdown)."""
    now = now or dt.datetime.now(tz=TZ)
    rows = _load_rows()
    pending = _pending_count()

    week = [r for r in rows if 0 <= (now - r["when"]).days < 7]
    prev = [r for r in rows if 7 <= (now - r["when"]).days < 14]
    ig = [r for r in week if r["platform"] == "IG"]

    start = (now - dt.timedelta(days=6))
    header = (f"📬 *RAPPORT DE LA SEMAINE*\n"
              f"_{start.day} {_FR_MONTHS[start.month]} → {now.day} {_FR_MONTHS[now.month]}_")

    if not week:
        note = ("\n\nAucune publication avec stats cette semaine."
                if not rows else "\n\nPas encore de stats collectées pour cette semaine "
                "(les insights arrivent ~24h après publication).")
        extra = f"\n🕒 {pending} publication(s) en attente de collecte." if pending else ""
        return header + note + extra

    lines = [header, ""]

    # --- Vue d'ensemble (Instagram = le cœur) ---
    if ig:
        reach_now = sum(r["reach"] for r in ig)
        reach_prev = sum(r["reach"] for r in prev if r["platform"] == "IG")
        lines.append("📊 *Vue d'ensemble* (Instagram)")
        lines.append(f"• {len(ig)} post(s) · reach total *{_fmt(reach_now)}*{_delta(reach_now, reach_prev)}")
        lines.append(f"• Reach moyen {_fmt(_avg([r['reach'] for r in ig]))} · "
                     f"saves {_fmt(sum(r['saves'] for r in ig))} · "
                     f"engagement {_fmt(_avg([r['eng'] for r in ig]))}%")
    th = [r for r in week if r["platform"] == "TH"]
    if th:
        lines.append(f"• Threads : {len(th)} post(s), reach moyen "
                     f"{_fmt(_avg([r['reach'] for r in th]))}")
    lines.append("")

    src = ig or week  # base d'analyse

    # --- Top post ---
    top = max(src, key=lambda r: r["reach"])
    if top["reach"] > 0:
        snippet = top["caption"].strip().split("\n", 1)[0][:55]
        lines.append("🏆 *Top post*")
        lines.append(f"_{snippet}…_")
        lines.append(f"→ {_fmt(top['reach'])} vues · {top['saves']} saves · "
                     f"{top['galerie'].title()}")
        lines.append("")

    # --- Par galerie (si ≥ 2 galeries avec data) ---
    gal: dict = {}
    for r in src:
        gal.setdefault(r["galerie"], []).append(r["reach"])
    if len(gal) >= 2:
        ranked = sorted(((g, _avg(v), len(v)) for g, v in gal.items()),
                        key=lambda x: x[1], reverse=True)
        lines.append("🌍 *Par galerie* (reach moyen)")
        for g, avg, n in ranked[:5]:
            lines.append(f"• {g.title()} {_fmt(avg)}" + (f" (×{n})" if n > 1 else ""))
        lines.append("")

    # --- Créneaux (si ≥ 5 posts, sinon trop bruité) ---
    if len(src) >= 5:
        eve = [r for r in src if 17 <= r["when"].hour <= 21]
        other = [r for r in src if not (17 <= r["when"].hour <= 21)]
        if eve and other:
            e, o = _avg([r["eng"] for r in eve]), _avg([r["eng"] for r in other])
            if abs(e - o) > 0.5:
                lines.append("⏰ *Créneaux*")
                lines.append(f"• Le soir (17-21h) {'>' if e > o else '<'} le reste "
                             f"({_fmt(e)}% vs {_fmt(o)}%)")
                lines.append("")

    # --- Enseignements (prudents) ---
    ens = []
    if ig and prev:
        rp = sum(r["reach"] for r in prev if r["platform"] == "IG")
        rn = sum(r["reach"] for r in ig)
        if rp > 0 and rn > rp * 1.15:
            ens.append("📈 Reach en hausse — continue sur cette lancée.")
        elif rp > 0 and rn < rp * 0.85:
            ens.append("📉 Reach en baisse cette semaine — varie villes/horaires.")
    if len(src) < 4:
        ens.append("ℹ️ Peu de posts cette semaine : chiffres indicatifs, "
                   "les tendances se confirmeront sur 2-3 semaines.")
    if ig and sum(r["saves"] for r in ig) == 0:
        ens.append("💾 0 save cette semaine — les carrousels et les images fortes "
                   "en génèrent le plus (signal n°1 de l'algo).")
    if ens:
        lines.append("💡 *Enseignements*")
        lines += [f"• {e}" for e in ens]
        lines.append("")

    if pending:
        lines.append(f"🕒 {pending} publication(s) attendent leurs stats (J+1).")
    lines.append("_Reels non inclus (publiés à la main). Surveille aussi le % "
                 "de non-abonnés dans l'app Instagram._")
    return "\n".join(lines).strip()
