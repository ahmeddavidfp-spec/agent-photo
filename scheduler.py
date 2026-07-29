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
    due_scheduled_posts, get_active_daily_autopubs, get_daily_autopub_last_run,
    get_job_last_run, mark_photo_as_sent, posts_pending_metrics,
    record_published_post, save_metrics, schedule_post, set_daily_autopub_last_run,
    set_job_last_run, set_scheduled_status,
)
from meta_api import (
    facebook_page_configured, fetch_ig_insights, fetch_th_insights,
    publish_carousel_to_instagram, publish_to_facebook_page,
    publish_to_instagram, publish_to_threads,
)
from telegram_bot import send_message
from timezones import DEFAULT_SLOTS, next_slot_for_hour, now_local, now_utc, to_utc

logger = logging.getLogger(__name__)

POLL_SECONDS = 30
# Heure locale (Europe/Brussels) à partir de laquelle on fait le check des tokens
ALERT_HOUR_LOCAL = 9
# Intervalle de collecte des insights (en secondes) — sobre pour Meta rate limits
INSIGHTS_COLLECT_INTERVAL = 3600  # 1×/h
# Heure locale minimale pour déclencher l'auto-pub quotidien
DAILY_AUTOPUB_TRIGGER_HOUR = 8

# État en mémoire (un seul thread scheduler). Les gardes-DATES (rapport hebdo,
# check tokens, scan) sont désormais persistées en DB (job_state) → pas de
# doublon après un redémarrage. Restent en mémoire les simples throttles.
_last_insights_run: float = 0.0
_last_scan_attempt: float = 0.0

WEEKLY_REPORT_HOUR = 18  # dimanche à partir de 18h (heure locale)
GALLERY_SCAN_HOUR = 10   # scan des nouvelles galeries 1×/jour à partir de 10h
SCAN_RETRY_INTERVAL = 1800  # si site injoignable, re-tenter au plus toutes les 30 min


def _run_weekly_report() -> None:
    """Envoie le rapport du Copilote le dimanche soir (1×/semaine).

    Garde-date persistée en DB → pas de rapport en double si l'app redémarre
    dimanche soir."""
    from settings import ALLOWED_CHAT_ID
    if not ALLOWED_CHAT_ID:
        return
    nowl = now_local()
    if nowl.weekday() != 6 or nowl.hour < WEEKLY_REPORT_HOUR:  # 6 = dimanche
        return
    today_str = nowl.strftime("%Y-%m-%d")
    if get_job_last_run("weekly_report") == today_str:
        return
    set_job_last_run("weekly_report", today_str)
    try:
        from analyzer import build_report
        send_message(int(ALLOWED_CHAT_ID), build_report())
        logger.info("📬 Rapport hebdo envoyé.")
    except Exception as e:
        logger.exception("Rapport hebdo échoué : %s", e)


def _run_gallery_scan() -> None:
    """1×/jour (≥ GALLERY_SCAN_HOUR) : détecte les nouvelles galeries du site
    et alerte l'utilisateur avec des boutons Ajouter / Ignorer.

    Si le site est injoignable, on re-tente — mais espacé (SCAN_RETRY_INTERVAL)
    pour ne PAS marteler Squarespace (ce qui rallongerait le blocage d'IP)."""
    global _last_scan_attempt
    from settings import ALLOWED_CHAT_ID
    if not ALLOWED_CHAT_ID:
        return
    nowl = now_local()
    today_str = nowl.strftime("%Y-%m-%d")
    if nowl.hour < GALLERY_SCAN_HOUR or get_job_last_run("gallery_scan") == today_str:
        return
    now_ts = time.time()
    if now_ts - _last_scan_attempt < SCAN_RETRY_INTERVAL:
        return
    _last_scan_attempt = now_ts
    try:
        from discover import SiteUnreachable, scan_new_galleries
        try:
            found = scan_new_galleries()
        except SiteUnreachable:
            # Site injoignable : on NE spamme PAS et on NE fige PAS la date.
            # Nouvelle tentative dans SCAN_RETRY_INTERVAL (pas avant).
            logger.warning("Scan galeries reporté : site injoignable.")
            return
        set_job_last_run("gallery_scan", today_str)  # scan réussi → plus rien avant demain
        if not found:
            return
        from app import alert_new_galleries
        alert_new_galleries(int(ALLOWED_CHAT_ID), found)
        logger.info("🆕 %d nouvelle(s) galerie(s) signalée(s).", len(found))
    except Exception as e:
        logger.exception("Scan galeries échoué : %s", e)


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
            # metrics is None = échec transitoire (réseau/rate-limit/token) → on
            # laisse le post EN ATTENTE pour re-tenter à la prochaine passe, au
            # lieu de figer ses metrics à vide définitivement. Un dict (même {})
            # = collecte réussie → on sauve (arrête de re-tenter).
            if metrics is None:
                logger.info("Insights %s(%s) : échec, re-tentative plus tard", platform, post_id)
                continue
            save_metrics(post_id, metrics)
            logger.info("Insights %s(%s) : %s", platform, post_id, metrics)
        except Exception as e:
            logger.warning("Insights %s(%s) failed: %s", platform, post_id, e)


def _run_daily_token_check() -> None:
    """1×/jour à partir de ALERT_HOUR_LOCAL, vérifie l'expiration des tokens.
    Garde-date persistée en DB (pas d'alerte en double après un redémarrage)."""
    nowl = now_local()
    today_str = nowl.strftime("%Y-%m-%d")
    if nowl.hour < ALERT_HOUR_LOCAL:
        return
    if get_job_last_run("token_check") == today_str:
        return
    set_job_last_run("token_check", today_str)
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
    from gallery import unpack_urls

    rows = due_scheduled_posts(now_utc().replace(tzinfo=None))
    for post_id, chat_id, image_url, caption, run_at in rows:
        logger.info("⏰ Scheduled post %s due (run_at=%s)", post_id, run_at)
        # image_url peut contenir plusieurs URLs packées (carrousel).
        urls = unpack_urls(image_url)
        cover = urls[0] if urls else image_url

        visible_caption, _alt = split_content(caption)

        if len(urls) >= 2:
            ok_ig, res_ig = publish_carousel_to_instagram(urls, caption)
        else:
            ok_ig, res_ig = publish_to_instagram(cover, caption)

        # Déclinaison NATIVE Threads (fallback : légende IG si l'IA échoue)
        th_text = None
        try:
            from ai import decline_caption
            th_text = decline_caption("threads", visible_caption)
        except Exception as e:
            logger.warning("Déclinaison Threads KO : %s", e)
        ok_th, res_th = publish_to_threads(cover, th_text or caption)

        # Déclinaison NATIVE Facebook (récit FR) — si la Page est configurée
        if facebook_page_configured():
            try:
                from ai import decline_caption
                fb_text = decline_caption("facebook", visible_caption)
                if fb_text:
                    ok_fb, res_fb = publish_to_facebook_page(urls or [cover], fb_text)
                    logger.info("FB Page (scheduled) : %s (%s)",
                                "OK" if ok_fb else "KO", str(res_fb)[:80])
            except Exception as e:
                logger.warning("Publication Facebook KO : %s", e)

        # Épinglage Pinterest (SEO, tableau par ville) — best effort
        try:
            import pinterest
            if pinterest.connected():
                pinterest.pin_photos(urls or [cover], visible_caption)
        except Exception as e:
            logger.warning("Pinterest KO : %s", e)

        galerie = _gallery_from_url(cover)

        if ok_ig and isinstance(res_ig, str) and res_ig:
            try:
                record_published_post("IG", res_ig, cover, visible_caption, galerie)
            except Exception as e:
                logger.warning("record_published_post IG failed: %s", e)
        if ok_th and isinstance(res_th, str) and res_th:
            try:
                record_published_post("TH", res_th, cover, visible_caption, galerie)
            except Exception as e:
                logger.warning("record_published_post TH failed: %s", e)

        if ok_ig or ok_th:
            set_scheduled_status(post_id, "sent")
            # Ne marquer que les photos RÉELLEMENT publiées :
            #  - carrousel IG réussi (≥2) → les N photos sont sur IG
            #  - sinon (IG simple, ou seulement Threads) → seule la couverture
            published = urls if (ok_ig and len(urls) >= 2) else [cover]
            for u in published:
                mark_photo_as_sent(u, galerie)
        else:
            set_scheduled_status(post_id, "error")

        msg = (
            "⏰ **Post Programmé Exécuté !**\n"
            f"IG: {'✅' if ok_ig else '❌ ' + str(res_ig)[:80]}\n"
            f"TH: {'✅' if ok_th else '❌ ' + str(res_th)[:80]}"
        )
        send_message(chat_id, msg)


def _silent_autopub(chat_id: int, galerie: str) -> None:
    """Auto-pub quotidien sans confirmation : sélectionne, génère, programme."""
    from ai import generate_caption, split_content
    from db import best_posting_hour
    from gallery import carousel_settings, pack_urls, pick_unseen_photos
    from settings import load_yaml_config

    config = load_yaml_config()
    names = config.get("gallery_names") or {}
    display = names.get(galerie) or galerie.replace("-", " ").replace("_", " ").title()
    car_enabled, car_count = carousel_settings(config)

    logger.info("[daily_autopub] start galerie=%s chat=%s", galerie, chat_id)
    try:
        urls = pick_unseen_photos(config["site_url"], galerie, car_count if car_enabled else 1)
    except Exception as e:
        logger.exception("[daily_autopub] pick_unseen_photos failed: %s", e)
        send_message(chat_id, f"⚠️ Auto-pub quotidien *{display}* : erreur de sélection.")
        return

    if not urls:
        send_message(
            chat_id,
            f"⚠️ Auto-pub quotidien *{display}* : galerie épuisée, toutes les photos ont été publiées.",
        )
        return

    cover = urls[0]  # couverture ; la légende couvre TOUTE la sélection
    try:
        full_content = generate_caption(cover, galerie, series_urls=urls)
    except Exception as e:
        logger.exception("[daily_autopub] generate_caption failed: %s", e)
        send_message(chat_id, f"⚠️ Auto-pub quotidien *{display}* : erreur de génération de légende.")
        return

    hour = best_posting_hour(galerie) or best_posting_hour()
    if hour is None:
        now_h = now_local().hour
        hour = next((h for h in DEFAULT_SLOTS if h > now_h), DEFAULT_SLOTS[0])

    target_local = next_slot_for_hour(hour)
    run_at_utc = to_utc(target_local).replace(tzinfo=None)
    schedule_post(chat_id, pack_urls(urls), full_content, run_at_utc)

    day_label = "aujourd'hui" if target_local.date() == now_local().date() else "demain"
    time_label = target_local.strftime("%H:%M")
    format_label = f"🖼️ Carrousel de {len(urls)} photos\n" if len(urls) >= 2 else ""
    send_message(
        chat_id,
        f"🔄 *Auto-pub quotidien* — *{display}*\n"
        f"{format_label}"
        f"📅 Programmé *{day_label} à {time_label}* automatiquement.",
    )
    logger.info("[daily_autopub] scheduled galerie=%s at %s", galerie, run_at_utc)


def _run_daily_autopubs() -> None:
    """Lance une fois par jour (après DAILY_AUTOPUB_TRIGGER_HOUR) les auto-pubs actifs."""
    nowl = now_local()
    if nowl.hour < DAILY_AUTOPUB_TRIGGER_HOUR:
        return
    today_str = nowl.strftime("%Y-%m-%d")

    active = get_active_daily_autopubs()
    for chat_id, galerie in active:
        last_run = get_daily_autopub_last_run(chat_id, galerie)
        if last_run == today_str:
            continue
        set_daily_autopub_last_run(chat_id, galerie, today_str)
        threading.Thread(
            target=_silent_autopub,
            args=(chat_id, galerie),
            daemon=True,
            name=f"daily-autopub-{galerie}-{chat_id}",
        ).start()


def scheduler_loop() -> None:
    while True:
        try:
            _process_due_posts()
            _run_daily_token_check()
            _collect_pending_insights()
            _run_daily_autopubs()
            _run_weekly_report()
            _run_gallery_scan()
            # Sauvegarde DB locale → /data (rate-limitée 5 min, thread isolé,
            # seulement si la DB a changé — voir db.maybe_backup_db)
            from db import maybe_backup_db
            maybe_backup_db()
        except Exception as e:
            logger.exception("Scheduler loop error: %s", e)
        time.sleep(POLL_SECONDS)


def start_scheduler() -> None:
    """Démarre le scheduler en thread démon si on est en process web unique."""
    t = threading.Thread(target=scheduler_loop, daemon=True, name="scheduler")
    t.start()
    logger.info("Scheduler démarré en thread démon.")
