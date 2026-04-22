"""Agent Photo — webhook Flask + dispatch Telegram."""
import logging
import sys
import threading
import time

from flask import Flask, abort, jsonify, request

from db import (
    already_sent_urls, clear_session, export_to_csv, get_session,
    get_stats, init_db, mark_photo_as_sent, record_published_post,
    save_session, schedule_post,
)
from ai import SEPARATOR, generate_caption, split_content
from gallery import (
    counts_for_gallery, fetch_gallery_photos, invalidate_cache, pick_unseen_photo,
)
from meta_api import (
    publish_to_instagram, publish_to_threads,
    renew_instagram_token, renew_threads_token, token_status,
)
from scheduler import start_scheduler
from settings import (
    ALLOWED_CHAT_ID, CRON_SECRET, TELEGRAM_TOKEN, TELEGRAM_WEBHOOK_SECRET,
    load_yaml_config,
)
from telegram_bot import send_document, send_message, send_photo
from timezones import now_local, to_utc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

init_db()
start_scheduler()


# =============================================================================
# ROUTES DE SERVICE
# =============================================================================

@app.route("/")
def home():
    return "Agent Photo is Alive", 200


@app.route("/health")
def health():
    """Health-check détaillé pour UptimeRobot."""
    checks = {}
    try:
        _ = already_sent_urls()
        checks["db"] = "ok"
    except Exception as e:
        checks["db"] = f"error: {e}"
    checks["tokens"] = token_status().replace("\n", " | ")
    status_code = 200 if checks["db"] == "ok" else 503
    return jsonify(checks), status_code


@app.route("/cron/refresh-tokens", methods=["POST", "GET"])
def cron_refresh_tokens():
    """À appeler tous les 45 jours via un Cron Job Render.

    Sécurité : header X-Cron-Secret doit matcher CRON_SECRET (si défini).
    """
    if CRON_SECRET and request.headers.get("X-Cron-Secret") != CRON_SECRET:
        abort(403)

    ig_ok, ig_res = renew_instagram_token()
    th_ok, th_res = renew_threads_token()
    payload = {
        "instagram": {"ok": ig_ok, "result": str(ig_res)[:200]},
        "threads": {"ok": th_ok, "result": str(th_res)[:200]},
    }
    logger.info("Token refresh: %s", payload)
    return jsonify(payload)


# =============================================================================
# WEBHOOK TELEGRAM
# =============================================================================

PROCESSED_CACHE: dict[int, float] = {}
_CACHE_LOCK = threading.Lock()


def _is_duplicate(update_id: int) -> bool:
    now = time.time()
    with _CACHE_LOCK:
        expired = [uid for uid, ts in PROCESSED_CACHE.items() if now - ts > 60]
        for uid in expired:
            del PROCESSED_CACHE[uid]
        if update_id in PROCESSED_CACHE:
            return True
        PROCESSED_CACHE[update_id] = now
    return False


def _authorized_chat(chat_id: int) -> bool:
    if not ALLOWED_CHAT_ID:
        return True
    try:
        return str(chat_id) == str(ALLOWED_CHAT_ID)
    except Exception:
        return False


@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    # 1. Vérification du secret Telegram
    if TELEGRAM_WEBHOOK_SECRET:
        header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if header != TELEGRAM_WEBHOOK_SECRET:
            logger.warning("Webhook Telegram : secret invalide")
            abort(403)

    data = request.get_json(silent=True) or {}
    update_id = data.get("update_id")
    if update_id is None:
        return jsonify({"status": "ok"})

    if _is_duplicate(update_id):
        logger.info("🚫 Doublon bloqué (update_id=%s)", update_id)
        return jsonify({"status": "ok"})

    chat_id = (
        data.get("message", {}).get("chat", {}).get("id")
        or data.get("callback_query", {}).get("message", {}).get("chat", {}).get("id")
    )
    if chat_id is None:
        return jsonify({"status": "ok"})

    if not _authorized_chat(chat_id):
        logger.warning("Chat non autorisé : %s", chat_id)
        return jsonify({"status": "ok"})

    text = (data.get("message", {}).get("text") or "").strip()
    action = data.get("callback_query", {}).get("data", "")

    # IMPORTANT : on ack Telegram en <1ms et on traite en background.
    # Render kill le worker à 60s ; _send_menu peut scraper 18 galeries +
    # appeler Meta (retries http_client → worst case ~67s).
    if text:
        logger.info("📩 texte: %s", text)
        threading.Thread(
            target=_safe_handle, args=(_handle_text, chat_id, text),
            daemon=True, name=f"tg-text-{update_id}",
        ).start()
    elif action:
        logger.info("🔘 action: %s", action)
        threading.Thread(
            target=_safe_handle, args=(_handle_action, chat_id, action),
            daemon=True, name=f"tg-action-{update_id}",
        ).start()

    return jsonify({"status": "ok"})


def _safe_handle(fn, chat_id: int, payload: str) -> None:
    """Exécute handler en thread ; log timing + erreurs sans crasher."""
    t0 = time.time()
    try:
        fn(chat_id, payload)
    except Exception as e:
        logger.exception("Handler %s failed: %s", fn.__name__, e)
        try:
            send_message(chat_id, f"❌ Erreur interne : {str(e)[:200]}")
        except Exception:
            pass
    finally:
        dt_ms = int((time.time() - t0) * 1000)
        logger.info("✅ %s done in %dms (payload=%r)", fn.__name__, dt_ms, payload[:40])


# =============================================================================
# DISPATCH TEXTE
# =============================================================================

def _handle_text(chat_id: int, text: str) -> None:
    if text == "/renew_threads":
        ok, res = renew_threads_token()
        msg = f"✅ **TOKEN TH renouvelé ({res[1]}j)**\n`{res[0]}`" if ok else f"❌ {res}"
        send_message(chat_id, msg)
        return

    if text == "/renew_instagram":
        ok, res = renew_instagram_token()
        msg = f"✅ **TOKEN IG renouvelé ({res[1]}j)**\n`{res[0]}`" if ok else f"❌ {res}"
        send_message(chat_id, msg)
        return

    if text == "/debug_db":
        send_message(chat_id, get_stats())
        return

    if text == "/health":
        send_message(chat_id, token_status())
        return

    session = get_session(chat_id)
    if session and session[1].startswith("WAITING_SCHEDULE|"):
        _handle_schedule_input(chat_id, session, text)
        return
    if session and session[1] == "WAITING_FOR_MANUAL":
        save_session(chat_id, session[0], text)
        kb = [
            [{"text": "🚀 Les deux", "callback_data": "pub_both"},
             {"text": "📅 Programmer", "callback_data": "schedule_btn"}],
            [{"text": "📸 Insta", "callback_data": "pub_ig"},
             {"text": "🧵 Threads", "callback_data": "pub_th"}],
        ]
        send_message(chat_id, "✅ Prêt !", reply_markup={"inline_keyboard": kb})
        return

    _send_menu(chat_id)


def _handle_schedule_input(chat_id: int, session: tuple, text: str) -> None:
    import datetime as dt
    try:
        clean = text.strip().replace("h", ":")
        if ":" not in clean:
            clean += ":00"
        hh, mm = map(int, clean.split(":"))
        target_local = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target_local <= now_local():
            target_local += dt.timedelta(days=1)
        run_at_utc = to_utc(target_local).replace(tzinfo=None)

        real_caption = session[1].replace("WAITING_SCHEDULE|", "")
        schedule_post(chat_id, session[0], real_caption, run_at_utc)
        clear_session(chat_id)
        send_message(
            chat_id,
            f"✅ **Programmé pour {target_local.strftime('%H:%M')}** (Europe/Brussels).",
        )
    except Exception as e:
        logger.warning("Bad schedule input '%s' : %s", text, e)
        send_message(chat_id, "❌ Format invalide (ex: 18:00).")


# =============================================================================
# DISPATCH ACTIONS (inline buttons)
# =============================================================================

def _handle_action(chat_id: int, action: str) -> None:
    if action == "menu":
        _send_menu(chat_id)
        return
    if action == "view_stats":
        send_message(chat_id, get_stats())
        return
    if action == "export_db_btn":
        send_document(chat_id, export_to_csv())
        return
    if action == "renew_threads_btn":
        ok, res = renew_threads_token()
        msg = f"✅ **TOKEN TH renouvelé ({res[1]}j)**\n`{res[0]}`" if ok else f"❌ {res}"
        send_message(chat_id, msg)
        return

    if action == "schedule_btn":
        session = get_session(chat_id)
        if session:
            save_session(chat_id, session[0], f"WAITING_SCHEDULE|{session[1]}")
            send_message(chat_id, "📅 **Heure ?** (HH:MM)")
        return

    if action.startswith("select_"):
        galerie = action.split("_", 1)[1]
        threading.Thread(
            target=_send_suggestion, args=(chat_id, galerie), daemon=True
        ).start()
        return

    if action == "manual_edit":
        session = get_session(chat_id)
        if session:
            save_session(chat_id, session[0], "WAITING_FOR_MANUAL")
            send_message(chat_id, "✍️ Envoie ta nouvelle légende.")
        return

    mode_map = {"pub_both": "both", "pub_ig": "ig", "pub_th": "th"}
    if action in mode_map:
        session = get_session(chat_id)
        if not session:
            send_message(chat_id, "⚠️ **Session expirée.**\nClique 'Menu' pour recommencer.")
            return
        send_message(chat_id, "⏳ **Traitement en cours...**")
        threading.Thread(
            target=_background_publish,
            args=(chat_id, mode_map[action], session[0], session[1]),
            daemon=True,
        ).start()


# =============================================================================
# RENDU DU MENU + SUGGESTION
# =============================================================================

def _progress_dot(done: int, total: int) -> str:
    """Icône colorée par taux de complétion : ⚪ (0) 🔵 (en cours) ✅ (fini)."""
    if total <= 0:
        return "⚪"
    if done >= total:
        return "✅"
    if done == 0:
        return "⚪"
    return "🔵"


def _display_name_for(slug: str, config: dict) -> str:
    """Joli nom pour affichage (lecture de gallery_names, fallback slug titlé)."""
    names = (config.get("gallery_names") or {})
    if slug in names and names[slug]:
        return str(names[slug])
    return slug.replace("-", " ").replace("_", " ").title()


def _compact_token_line() -> str:
    """Header 1-ligne : '📸 Agent Photo · IG 35j · TH 59j'."""
    from meta_api import token_days_left
    ig = token_days_left("IG")
    th = token_days_left("TH")
    ig_s = f"IG {ig}j" if ig is not None else "IG ?"
    th_s = f"TH {th}j" if th is not None else "TH ?"
    return f"📸 *Agent Photo* · {ig_s} · {th_s}"


def _send_menu(chat_id: int) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from gallery import suggest_next_gallery

    config = load_yaml_config()
    header = _compact_token_line()
    sent = already_sent_urls()

    keyboard = [[
        {"text": "📈 Stats", "callback_data": "view_stats"},
        {"text": "📥 Export", "callback_data": "export_db_btn"},
        {"text": "🔄 Renew", "callback_data": "renew_threads_btn"},
    ]]

    galeries = config.get("galeries", [])
    # Parallélisation du scraping (cache hit = instantané, miss = ~1s pour 18 galeries)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda g: (g, counts_for_gallery(config["site_url"], g, sent)),
            galeries,
        ))

    # Suggestion anti-répétition (galerie utilisée il y a le plus longtemps)
    suggested = suggest_next_gallery(galeries)

    # Bouton ⭐ en haut pour la galerie suggérée
    suggestion_line = ""
    if suggested:
        suggested_display = _display_name_for(suggested, config)
        done_s, total_s = dict((g, (d, t)) for g, (d, t) in results).get(
            suggested, (0, 0)
        )
        keyboard.append([{
            "text": f"⭐ {suggested_display}  ({done_s}/{total_s})",
            "callback_data": f"select_{suggested}",
        }])
        suggestion_line = f"\n⭐ Suggestion : *{suggested_display}*"

    # Les autres galeries, 2 par ligne, avec dot coloré + display name propre
    buttons = []
    for g, (done, total) in results:
        if g == suggested:
            continue  # déjà au-dessus
        name = _display_name_for(g, config)
        dot = _progress_dot(done, total)
        label = f"{dot} {name} {done}/{total}" if total else f"{dot} {name}"
        buttons.append({"text": label, "callback_data": f"select_{g}"})
    for i in range(0, len(buttons), 2):
        keyboard.append(buttons[i:i + 2])

    send_message(
        chat_id,
        f"{header}{suggestion_line}\n\nChoisis une galerie :",
        reply_markup={"inline_keyboard": keyboard},
    )


def _send_suggestion(chat_id: int, galerie: str) -> None:
    config = load_yaml_config()
    img_url = pick_unseen_photo(config["site_url"], galerie)
    if not img_url:
        send_message(chat_id, "Galerie vide ou toutes les photos déjà envoyées.")
        return

    full_content = generate_caption(img_url, galerie)
    save_session(chat_id, img_url, full_content)
    visible_caption, _alt = split_content(full_content)

    kb = [
        [{"text": "🚀 Publier sur les deux", "callback_data": "pub_both"}],
        [{"text": "📅 Programmer", "callback_data": "schedule_btn"}],
        [{"text": "📸 Insta", "callback_data": "pub_ig"},
         {"text": "🧵 Threads", "callback_data": "pub_th"}],
        [{"text": "✍️ Éditer légende", "callback_data": "manual_edit"}],
        [{"text": "🔄 Autre", "callback_data": f"select_{galerie}"},
         {"text": "⬅️ Menu", "callback_data": "menu"}],
    ]
    send_photo(chat_id, img_url, visible_caption, reply_markup={"inline_keyboard": kb})


# =============================================================================
# PUBLICATION EN ARRIÈRE-PLAN
# =============================================================================

def _gallery_from_url(image_url: str) -> str:
    """Déduit le slug de galerie depuis l'URL Squarespace.

    Ex. https://.../new-york/photo.jpg → 'new-york'. Fallback : 'inconnue'.
    """
    try:
        from urllib.parse import urlparse
        parts = [p for p in urlparse(image_url).path.split("/") if p]
        # Squarespace colle les slugs au début du path, on prend le 1er non-vide
        return parts[0] if parts else "inconnue"
    except Exception:
        return "inconnue"


def _background_publish(chat_id: int, mode: str, image_url: str, caption: str) -> None:
    logger.info("🚀 START JOB | Mode=%s", mode)
    ok_ig = ok_th = False
    res_ig = res_th = "Non demandé"

    try:
        if mode in ("both", "ig"):
            ok_ig, res_ig = publish_to_instagram(image_url, caption)
        if mode in ("both", "th"):
            ok_th, res_th = publish_to_threads(image_url, caption)
    except Exception as e:
        logger.exception("Erreur background_publish")
        send_message(chat_id, f"🔥 Erreur : {e}")
        return

    # Enregistre chaque publish réussi pour la boucle d'apprentissage
    # (caption sans alt → c'est ce que l'audience voit)
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

    if mode == "both":
        if ok_ig and ok_th:
            final = "🚀 **Succès Total !**\nInsta & Threads : ✅"
            mark_photo_as_sent(image_url, galerie)
            clear_session(chat_id)
        else:
            final = (
                f"⚠️ **Résultat Partiel :**\n"
                f"IG: {'✅' if ok_ig else '❌ ' + str(res_ig)[:80]}\n"
                f"TH: {'✅' if ok_th else '❌ ' + str(res_th)[:80]}"
            )
    elif mode == "ig":
        if ok_ig:
            final = "📸 **Instagram :** ✅"
            mark_photo_as_sent(image_url, galerie)
        else:
            final = f"❌ **Erreur Insta :** {res_ig}"
    else:  # th
        if ok_th:
            final = "🧵 **Threads :** ✅"
            mark_photo_as_sent(image_url, galerie)
        else:
            final = f"❌ **Erreur Threads :** {res_th}"

    send_message(chat_id, final)


if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
