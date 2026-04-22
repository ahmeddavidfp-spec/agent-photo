"""Boucle planificateur : scanne scheduled_posts et publie quand l'heure est passée.

Exécute aussi :
- check quotidien d'expiration des tokens Meta (voir alerts.py) ;
- collecte d'insights 24h+ après publish pour la boucle d'apprentissage.
"""
import datetime as dt
import logging
import threading
import time
from typing import Optional

from alerts import send_token_alert_if_needed
from db import (
    due_scheduled_posts, mark_photo_as_sent, posts_pending_metrics,
    record_published_post, save_metrics, set_scheduled_status,
)
from meta_api import (
    fetch_ig_insights, fetch_th_insights,
    publish_to_instagram, publish_to_threads,
)
from telegram_bot import send_message
from timezones import now_local, now_utc

logger = logging.getLogger(__name__)

POLL_SECONDS = 30
# Heure locale (Europe/Brussels) à partir de laquelle on fait le check des tokens
ALERT_HOUR_LOCAL = 9
# Intervalle de collecte des insights (en secondes) — sobre pour Meta rate limits
INSIGHTS_COLLECT_INTERVAL = 3600  # 1×/h

# État du check quotidien (pas besoin de lock : un seul thread scheduler)
_last_alert_check_date: Optional[dt.date] = None
_last_insights_run: float = 0.0


def _collect_pending_insights() -> None:
    """Récupère les insights Meta pour les posts publiés >24h sans metrics."""
    global _last_insights_run
    now_ts = time.time()
    if now_ts - _last_insights_run < INSIGHTS_COLLECT_INTERVAL:
        return
    _last_insights_run = now_ts

    pending = posts_pending_metrics(older_than_hours=24)
    if not pending:
        return
    logger.info("Insights : %d post(s) à collecter", len(pending))
    for post_id, platform, media_id in pending:
        try:
            if platform == "IG":
                metrics = fetch_ig_insights(media_id)
            elif platform == "TH":
                metrics = fetch_th_insights(media_id)
            else:
                continue
            # Si Meta n'a rien renvoyé, on sauve quand même un dict vide pour
            # éviter de re-tenter indéfiniment (expired token, post supprimé, etc.)
            save_metrics(post_id, metrics or {})
            logger.info("Insights %s(%s) : %s", platform, post_id, metrics)
        except Exception as e:
            logger.warning("Insights %s(%s) failed: %s", platform, post_id, e)


def _run_daily_token_check() -> None:
    """1×/jour à partir de ALERT_HOUR_LOCAL, vérifie l'expiration des tokens."""
    global _last_alert_check_date
    nowl = now_local()
    today = nowl.date()
    if _last_alert_check_date == today:
        return
    if nowl.hour < ALERT_HOUR_LOCAL:
        return
    _last_alert_check_date = today
    try:
        send_token_alert_if_needed()
    except Exception as e:
        logger.exception("Daily token check failed: %s", e)


def _gallery_from_url(image_url: str) -> str:
    """Déduit le slug de galerie depuis l'URL (fallback 'inconnue')."""
    try:
        from urllib.parse import urlparse
        parts = [p for p in urlparse(image_url).path.split("/") if p]
        return parts[0] if parts else "inconnue"
    except Exception:
        return "inconnue"


def _process_due_posts() -> None:
    from ai import split_content  # import local pour éviter cycle

    rows = due_scheduled_posts(now_utc().replace(tzinfo=None))
    for post_id, chat_id, image_url, caption, run_at in rows:
        logger.info("⏰ Scheduled post %s due (run_at=%s)", post_id, run_at)
        ok_ig, res_ig = publish_to_instagram(image_url, caption)
        ok_th, res_th = publish_to_threads(image_url, caption)

        visible_caption, _alt = split_content(caption)
        galerie = _gallery_from_url(image_url)

        if ok_ig and isinstance(res_ig, str) and res_ig:
            try:
                record_published_post("IG", res_ig, image_url, visible_caption, galerie)
            except Exception as e:
                logger.warning("record_published_post IG failed: %s", e)
        if ok_th and isinstance(res_th, str) and res_th:
            try:
                record_published_post("TH", res_th, image_url, visible_caption, galerie)
            except Exception as e:
                logger.warning("record_published_post TH failed: %s", e)

        if ok_ig or ok_th:
            set_scheduled_status(post_id, "sent")
            mark_photo_as_sent(image_url, galerie)
        else:
            set_scheduled_status(post_id, "error")

        msg = (
            "⏰ **Post Programmé Exécuté !**\n"
            f"IG: {'✅' if ok_ig else '❌ ' + str(res_ig)[:80]}\n"
            f"TH: {'✅' if ok_th else '❌ ' + str(res_th)[:80]}"
        )
        send_message(chat_id, msg)


def scheduler_loop() -> None:
    while True:
        try:
            _process_due_posts()
            _run_daily_token_check()
            _collect_pending_insights()
        except Exception as e:
            logger.exception("Scheduler loop error: %s", e)
        time.sleep(POLL_SECONDS)


def start_scheduler() -> None:
    """Démarre le scheduler en thread démon si on est en process web unique."""
    t = threading.Thread(target=scheduler_loop, daemon=True, name="scheduler")
    t.start()
    logger.info("Scheduler démarré en thread démon.")
