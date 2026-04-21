"""Boucle planificateur : scanne scheduled_posts et publie quand l'heure est passée."""
import logging
import threading
import time

from db import due_scheduled_posts, mark_photo_as_sent, set_scheduled_status
from meta_api import publish_to_instagram, publish_to_threads
from telegram_bot import send_message
from timezones import now_utc

logger = logging.getLogger(__name__)

POLL_SECONDS = 30


def _process_due_posts() -> None:
    rows = due_scheduled_posts(now_utc().replace(tzinfo=None))
    for post_id, chat_id, image_url, caption, run_at in rows:
        logger.info("⏰ Scheduled post %s due (run_at=%s)", post_id, run_at)
        ok_ig, res_ig = publish_to_instagram(image_url, caption)
        ok_th, res_th = publish_to_threads(image_url, caption)

        if ok_ig or ok_th:
            set_scheduled_status(post_id, "sent")
            mark_photo_as_sent(image_url, "Programme")
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
        except Exception as e:
            logger.exception("Scheduler loop error: %s", e)
        time.sleep(POLL_SECONDS)


def start_scheduler() -> None:
    """Démarre le scheduler en thread démon si on est en process web unique."""
    t = threading.Thread(target=scheduler_loop, daemon=True, name="scheduler")
    t.start()
    logger.info("Scheduler démarré en thread démon.")
