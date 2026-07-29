"""Copilote — Planificateur : transforme les stats en PLAN d'action.

À partir des perfs passées (post_metrics) + de l'anti-répétition + des meilleures
heures, propose N publications pour la semaine à venir : quoi (galerie), quand
(jour + heure), et dans quel format. Le rendu est un message Telegram + une
structure exploitable par « Tout programmer ».

Calcul PUR (aucun scraping) : le choix réel des photos + la légende se font au
moment de programmer (voir app.py / _schedule_plan).
"""
import datetime as dt
import logging
from typing import List, Optional

from db import best_posting_hour, galerie_last_use
from settings import load_yaml_config
from timezones import TZ, now_local

logger = logging.getLogger(__name__)

_FR_DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
# Cadence de David : un CARROUSEL tous les 3 jours (→ 3 sur la semaine), publié
# sur IG + Threads + Facebook (légendes natives), galerie différente à chaque
# fois. Les Reels quotidiens sont gérés à part (montés + envoyés, pas programmés).
_DAY_OFFSETS = [1, 4, 7]      # tous les 3 jours
_DEFAULT_HOUR = 18            # le soir performe le mieux (FB veut 18h)
_N_POSTS = 3


def _ranked_galleries(config: dict, rows: List[dict], now: dt.datetime):
    """Galeries classées : d'abord les plus performantes (reach IG moyen), puis
    les moins récemment publiées (anti-répétition). Retourne (liste, perf)."""
    galeries = config.get("galeries") or []
    reach: dict = {}
    for r in rows:
        if r.get("platform") == "IG":
            reach.setdefault(r["galerie"], []).append(r.get("reach", 0))
    perf = {g: sum(v) / len(v) for g, v in reach.items() if v and g in galeries}
    performers = sorted(perf, key=lambda g: perf[g], reverse=True)

    def age_days(g: str) -> int:
        last = galerie_last_use(g)
        return (now.replace(tzinfo=None) - last).days if last else 9999

    rest = sorted((g for g in galeries if g not in perf), key=age_days, reverse=True)
    return performers + rest, perf


def build_weekly_plan(now: Optional[dt.datetime] = None) -> Optional[dict]:
    """Construit le plan de la semaine. Retourne {items, text} ou None si aucune
    galerie configurée. `items` = [{when, galerie, nom, fmt, reason}]."""
    now = now or now_local()
    config = load_yaml_config()
    names = config.get("gallery_names") or {}
    galeries = config.get("galeries") or []
    if not galeries:
        return None

    try:
        from analyzer import _load_rows
        rows = _load_rows()
    except Exception as e:
        logger.warning("Plan : lecture metrics KO : %s", e)
        rows = []

    ranked, perf = _ranked_galleries(config, rows, now)
    chosen = ranked[:_N_POSTS] if len(ranked) >= _N_POSTS else ranked

    items = []
    for i, g in enumerate(chosen):
        nom = names.get(g) or g.replace("-", " ").replace("_", " ").title()
        hour = best_posting_hour(g) or best_posting_hour() or _DEFAULT_HOUR
        offset = _DAY_OFFSETS[i] if i < len(_DAY_OFFSETS) else (3 * i + 1)
        when = (now + dt.timedelta(days=offset)).replace(
            hour=hour, minute=0, second=0, microsecond=0)
        fmt = "carousel"   # cadence : carrousels multi-réseau (IG+Threads+FB)

        # Raison (honnête selon la donnée dispo)
        if g in perf:
            rank_txt = "meilleure" if i == 0 else ("2ᵉ meilleure" if i == 1 else "bonne")
            reason = f"{rank_txt} portée ({int(perf[g])} vues/post)"
        else:
            last = galerie_last_use(g)
            if last:
                wks = max(1, (now.replace(tzinfo=None) - last).days // 7)
                reason = f"pas postée depuis {wks} sem." if wks >= 1 else "à remettre en avant"
            else:
                reason = "jamais publiée"
        items.append({"when": when, "galerie": g, "nom": nom, "fmt": fmt, "reason": reason})

    return {"items": items, "text": _render(now, items, perf)}


def _render(now: dt.datetime, items: List[dict], perf: dict) -> str:
    start = now + dt.timedelta(days=1)
    lines = ["📅 *TON PLAN — semaine du "
             f"{start.day} {['','janv.','févr.','mars','avr.','mai','juin','juil.','août','sept.','oct.','nov.','déc.'][start.month]}*"]
    if perf:
        top = max(perf, key=lambda g: perf[g])
        lines.append(f"_Basé sur tes perfs : {top.replace('-',' ').title()} cartonne._")
    else:
        lines.append("_Peu de données pour l'instant → rotation des galeries + créneaux du soir._")
    lines.append("")
    lines.append("*🖼️ Carrousels (tous les 3 j · IG + Threads + Facebook)*")
    for it in items:
        d = _FR_DAYS[it["when"].weekday()]
        lines.append(f"• *{d.capitalize()} {it['when'].hour}h* — *{it['nom']}* (6 photos)")
        lines.append(f"   _{it['reason']}_")
    lines.append("")
    lines.append("*🎬 Reels (1/jour · IG + TikTok + Facebook)*")
    lines.append("_Je monte un Reel chaque jour (galerie différente, homogène "
                 "N&B ou couleur) et te l'envoie — tu le postes à la main._")
    return "\n".join(lines)
