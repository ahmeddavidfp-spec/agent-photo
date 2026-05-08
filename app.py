"""Agent Photo — webhook Flask + dispatch Telegram."""
import logging
import sys
import threading
import time

from flask import Flask, abort, jsonify, request

from db import (
    already_sent_urls, best_posting_hour, cancel_scheduled_post, clear_session,
    export_to_csv, get_active_daily_autopubs, get_session, get_stats, init_db,
    is_daily_autopub_active, list_scheduled_posts,
    mark_photo_as_sent, record_published_post, save_session, schedule_post,
    set_daily_autopub,
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
from telegram_bot import answer_callback_query, send_document, send_message, send_photo, send_typing_action
from timezones import DEFAULT_SLOTS, from_utc_str, next_slot_for_hour, now_local, now_utc, to_utc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    stream=sys.stdout,
)
# Force stdout unbuffered pour que les logs des threads apparaissent
# immédiatement dans les logs Render (évite les pertes quand un thread
# crashe avant le flush naturel).
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Banner de boot pour identifier à 100% quelle version tourne en prod.
# Render injecte RENDER_GIT_COMMIT dans l'environnement de chaque deploy.
import os as _os
_git_commit = _os.environ.get("RENDER_GIT_COMMIT", "unknown")[:12]
logger.info("=" * 60)
logger.info("🚀 AGENT PHOTO BOOT | commit=%s", _git_commit)
logger.info("=" * 60)

try:
    init_db()
    logger.info("✅ init_db() OK")
except Exception as e:
    logger.exception("❌ init_db() failed: %s", e)

try:
    start_scheduler()
    logger.info("✅ start_scheduler() OK")
except Exception as e:
    logger.exception("❌ start_scheduler() failed: %s", e)


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


@app.route("/cron/reset-ig-token", methods=["POST", "GET"])
def cron_reset_ig_token():
    """Supprime le token IG stocké en DB pour forcer le fallback sur l'env var."""
    if CRON_SECRET and request.headers.get("X-Cron-Secret") != CRON_SECRET:
        abort(403)
    from db import connection
    with connection() as conn:
        conn.execute("DELETE FROM token_store WHERE key = 'ig_access_token'")
    logger.info("IG token DB supprimé — fallback sur env var IG_ACCESS_TOKEN")
    return jsonify({"ok": True, "message": "Token IG réinitialisé depuis env var"})


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

# Anti-doublon pour actions coûteuses (ex: select_X qui lance OpenAI + Claude).
# Clé = (chat_id, action_key). Si une action pour le même user est déjà en
# cours, on ignore les clics suivants pendant INFLIGHT_TTL secondes.
_INFLIGHT: dict = {}
_INFLIGHT_LOCK = threading.Lock()
INFLIGHT_TTL = 120  # 2 min — le temps max d'un cycle describe+caption


def _acquire_inflight(key: str) -> bool:
    """Retourne True si on peut démarrer, False si déjà en cours."""
    now = time.time()
    with _INFLIGHT_LOCK:
        # Cleanup des entrées périmées
        expired = [k for k, ts in _INFLIGHT.items() if now - ts > INFLIGHT_TTL]
        for k in expired:
            del _INFLIGHT[k]
        if key in _INFLIGHT:
            return False
        _INFLIGHT[key] = now
        return True


def _release_inflight(key: str) -> None:
    with _INFLIGHT_LOCK:
        _INFLIGHT.pop(key, None)


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
    callback_query = data.get("callback_query", {}) or {}
    action = callback_query.get("data", "")
    callback_query_id = callback_query.get("id", "")

    # IMPORTANT : on ack Telegram en <1ms et on traite en background.
    # Render kill le worker à 60s ; _send_menu peut scraper 18 galeries +
    # appeler Meta (retries http_client → worst case ~67s).
    if text:
        logger.info("📩 texte: %s", text)
        print(f"[boot={_git_commit}] spawning text thread for {text!r}", flush=True)
        threading.Thread(
            target=_safe_handle, args=(_handle_text, chat_id, text),
            daemon=True, name=f"tg-text-{update_id}",
        ).start()
    elif action:
        logger.info("🔘 action: %s", action)
        print(f"[boot={_git_commit}] spawning action thread for {action!r}", flush=True)
        # On ack le callback_query IMMÉDIATEMENT dans un mini thread pour
        # stopper le spinner Telegram. Sinon Telegram retry le webhook ~12s
        # plus tard (cause du "2 photos" vu précédemment).
        if callback_query_id:
            threading.Thread(
                target=answer_callback_query, args=(callback_query_id,),
                daemon=True, name=f"tg-ack-{update_id}",
            ).start()
        threading.Thread(
            target=_safe_handle, args=(_handle_action, chat_id, action),
            daemon=True, name=f"tg-action-{update_id}",
        ).start()

    return jsonify({"status": "ok"})


def _safe_handle(fn, chat_id: int, payload: str) -> None:
    """Exécute handler en thread ; log timing + erreurs sans crasher."""
    print(f"[thread] _safe_handle START fn={fn.__name__} payload={payload!r}", flush=True)
    t0 = time.time()
    try:
        fn(chat_id, payload)
    except Exception as e:
        print(f"[thread] _safe_handle EXCEPTION fn={fn.__name__}: {e!r}", flush=True)
        logger.exception("Handler %s failed: %s", fn.__name__, e)
        try:
            send_message(chat_id, f"❌ Erreur interne : {str(e)[:200]}")
        except Exception:
            pass
    finally:
        dt_ms = int((time.time() - t0) * 1000)
        print(f"[thread] _safe_handle END fn={fn.__name__} in {dt_ms}ms", flush=True)
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
        # Purge les tokens DB invalides pour forcer le fallback sur les env vars
        from db import connection as _db_conn
        with _db_conn() as _c:
            _c.execute("DELETE FROM token_store WHERE key IN ('ig_access_token', 'th_access_token')")
        ig_ok, ig_res = renew_instagram_token()
        th_ok, th_res = renew_threads_token()
        ig_msg = f"IG ✅ {ig_res[1]}j" if ig_ok else f"IG ❌ {str(ig_res)[:80]}"
        th_msg = f"TH ✅ {th_res[1]}j" if th_ok else f"TH ❌ {str(th_res)[:80]}"
        send_message(chat_id, f"🔄 *Tokens renouvelés*\n{ig_msg}\n{th_msg}")
        return

    if action == "schedule_btn":
        session = get_session(chat_id)
        if session:
            save_session(chat_id, session[0], f"WAITING_SCHEDULE|{session[1]}")
            send_message(chat_id, "📅 **Heure ?** (HH:MM)")
        return

    if action == "scheduled_list":
        _send_scheduled_list(chat_id)
        return

    if action.startswith("cancel_sched_"):
        try:
            post_id = int(action.split("_")[-1])
        except ValueError:
            return
        ok = cancel_scheduled_post(post_id, chat_id)
        send_message(chat_id, "✅ Post annulé." if ok else "⚠️ Post introuvable ou déjà publié.")
        return

    if action == "daily_menu":
        _send_daily_menu(chat_id)
        return

    if action.startswith("daily_on_"):
        galerie = action[len("daily_on_"):]
        set_daily_autopub(chat_id, galerie, True)
        config = load_yaml_config()
        display = _display_name_for(galerie, config)
        send_message(
            chat_id,
            f"✅ Auto-pub quotidien *{display}* activé.\n"
            "_Je publierai automatiquement une photo chaque jour à partir de 8h._",
        )
        return

    if action.startswith("daily_off_"):
        galerie = action[len("daily_off_"):]
        set_daily_autopub(chat_id, galerie, False)
        config = load_yaml_config()
        display = _display_name_for(galerie, config)
        send_message(chat_id, f"⏸ Auto-pub quotidien *{display}* désactivé.")
        return

    if action == "autopub_menu":
        _send_autopub_menu(chat_id)
        return

    if action == "autopub_cancel":
        clear_session(chat_id)
        send_message(chat_id, "❌ Auto-pub annulé.")
        return

    if action.startswith("gallery_done_"):
        g = action[len("gallery_done_"):]
        name = g.replace("_", " ").capitalize()
        send_message(chat_id, f"✅ Galerie *{name}* terminée — toutes les photos ont été publiées.")
        return

    if action.startswith("autopub_ok_"):
        try:
            hour = int(action.split("_")[-1])
        except ValueError:
            return
        session = get_session(chat_id)
        if not session:
            send_message(chat_id, "⚠️ Session expirée. Recommence depuis le menu.")
            return
        target_local = _next_slot_for_hour(hour)
        run_at_utc = to_utc(target_local).replace(tzinfo=None)
        schedule_post(chat_id, session[0], session[1], run_at_utc)
        clear_session(chat_id)
        day_label = "aujourd'hui" if target_local.date() == now_local().date() else "demain"
        send_message(chat_id, f"✅ *Programmé {day_label} à {target_local.strftime('%H:%M')}* !")
        return

    if action.startswith("autopub_"):
        galerie = action[len("autopub_"):]
        key = f"autopub:{chat_id}:{galerie}"
        if not _acquire_inflight(key):
            logger.info("🚫 Dédupe autopub: %s déjà en cours pour chat=%s", galerie, chat_id)
            return

        def _run_autopub():
            try:
                _auto_publish_flow(chat_id, galerie)
            except Exception as e:
                logger.exception("[autopub] crashed galerie=%s: %s", galerie, e)
                try:
                    send_message(chat_id, f"❌ Erreur auto-pub : {str(e)[:180]}")
                except Exception:
                    pass
            finally:
                _release_inflight(key)

        threading.Thread(target=_run_autopub, daemon=True, name=f"autopub-{galerie}").start()
        return

    if action.startswith("select_"):
        galerie = action.split("_", 1)[1]
        key = f"select:{chat_id}:{galerie}"
        if not _acquire_inflight(key):
            logger.info("🚫 Dédupe: %s déjà en cours pour chat=%s", galerie, chat_id)
            return

        def _run_suggestion():
            logger.info("[select] thread start galerie=%s chat=%s", galerie, chat_id)
            try:
                _send_suggestion(chat_id, galerie)
                logger.info("[select] ✅ finished galerie=%s", galerie)
            except Exception as e:
                logger.exception("[select] ❌ crashed galerie=%s: %s", galerie, e)
                try:
                    send_message(chat_id, f"❌ Erreur : {str(e)[:180]}")
                except Exception:
                    pass
            finally:
                _release_inflight(key)

        logger.info("[select] spawning thread for galerie=%s", galerie)
        threading.Thread(target=_run_suggestion, daemon=True).start()
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

import contextlib

# Alias local (exporté depuis timezones)
_DEFAULT_SLOTS = DEFAULT_SLOTS


@contextlib.contextmanager
def _typing_while(chat_id: int):
    """Envoie sendChatAction(typing) toutes les 4s pendant le bloc `with`."""
    stop = threading.Event()

    def _loop():
        send_typing_action(chat_id)
        while not stop.wait(4):
            send_typing_action(chat_id)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()


_next_slot_for_hour = next_slot_for_hour


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
    """Rend le menu Telegram. Très verbose en logs pour diagnostiquer les hangs."""
    from concurrent.futures import ThreadPoolExecutor
    from gallery import suggest_next_gallery

    t0 = time.time()
    logger.info("[menu] start chat=%s", chat_id)

    try:
        config = load_yaml_config()
        logger.info("[menu] config loaded in %dms", int((time.time() - t0) * 1000))

        t1 = time.time()
        header = _compact_token_line()  # cached 10 min, max 3s par appel non-caché
        logger.info("[menu] header (%s) built in %dms", header, int((time.time() - t1) * 1000))

        t1 = time.time()
        sent = already_sent_urls()
        logger.info("[menu] already_sent_urls: %d rows in %dms", len(sent), int((time.time() - t1) * 1000))

        keyboard = [
            [
                {"text": "📈 Stats", "callback_data": "view_stats"},
                {"text": "📅 Programmés", "callback_data": "scheduled_list"},
                {"text": "🔄 Renew", "callback_data": "renew_threads_btn"},
            ],
            [
                {"text": "🤖 Auto-pub", "callback_data": "autopub_menu"},
                {"text": "🔄 Quotidien", "callback_data": "daily_menu"},
                {"text": "📥 Export", "callback_data": "export_db_btn"},
            ],
        ]

        galeries = config.get("galeries", [])
        # Scraping parallèle ; counts_for_gallery a son propre cache 5min.
        t1 = time.time()
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(
                lambda g: (g, counts_for_gallery(config["site_url"], g, sent)),
                galeries,
            ))
        logger.info("[menu] scraped %d galeries in %dms", len(galeries), int((time.time() - t1) * 1000))

        t1 = time.time()
        suggested = suggest_next_gallery(galeries)
        logger.info("[menu] suggestion=%s in %dms", suggested, int((time.time() - t1) * 1000))

        # Bouton ⭐ en haut pour la galerie suggérée
        suggestion_line = ""
        if suggested:
            suggested_display = _display_name_for(suggested, config)
            done_s, total_s = dict((g, (d, t)) for g, (d, t) in results).get(
                suggested, (0, 0)
            )
            if total_s > 0 and done_s >= total_s:
                keyboard.append([{
                    "text": f"⭐ {suggested_display} ✅ terminée",
                    "callback_data": f"gallery_done_{suggested}",
                }])
            else:
                keyboard.append([{
                    "text": f"⭐ {suggested_display}  ({done_s}/{total_s})",
                    "callback_data": f"select_{suggested}",
                }])
            suggestion_line = f"\n⭐ Suggestion : *{suggested_display}*"

        # Les autres galeries, 2 par ligne, avec dot coloré + display name propre
        buttons = []
        for g, (done, total) in results:
            if g == suggested:
                continue
            name = _display_name_for(g, config)
            if total > 0 and done >= total:
                buttons.append({"text": f"✅ {name}", "callback_data": f"gallery_done_{g}"})
            else:
                dot = _progress_dot(done, total)
                label = f"{dot} {name} {done}/{total}" if total else f"{dot} {name}"
                buttons.append({"text": label, "callback_data": f"select_{g}"})
        for i in range(0, len(buttons), 2):
            keyboard.append(buttons[i:i + 2])

        t1 = time.time()
        send_message(
            chat_id,
            f"{header}{suggestion_line}\n\nChoisis une galerie :",
            reply_markup={"inline_keyboard": keyboard},
        )
        logger.info("[menu] send_message in %dms", int((time.time() - t1) * 1000))
        logger.info("[menu] ✅ TOTAL %dms", int((time.time() - t0) * 1000))
    except Exception as e:
        logger.exception("[menu] crashed after %dms: %s", int((time.time() - t0) * 1000), e)
        # Fallback minimal : au moins l'utilisateur a un menu cliquable
        try:
            send_message(
                chat_id,
                "⚠️ Menu dégradé (erreur interne).\nTape /start pour réessayer.",
            )
        except Exception:
            pass


def _send_suggestion(chat_id: int, galerie: str) -> None:
    t0 = time.time()
    logger.info("[suggest] start galerie=%s", galerie)

    config = load_yaml_config()
    logger.info("[suggest] config loaded (+%dms)", int((time.time() - t0) * 1000))

    with _typing_while(chat_id):
        img_url = pick_unseen_photo(config["site_url"], galerie)
        logger.info("[suggest] pick_unseen_photo → %s (+%dms)",
                    img_url[:60] if img_url else "None", int((time.time() - t0) * 1000))
        if not img_url:
            send_message(chat_id, "Galerie vide ou toutes les photos déjà envoyées.")
            return

        full_content = generate_caption(img_url, galerie)
    logger.info("[suggest] caption generated %d chars (+%dms)",
                len(full_content or ""), int((time.time() - t0) * 1000))

    save_session(chat_id, img_url, full_content)
    logger.info("[suggest] session saved (+%dms)", int((time.time() - t0) * 1000))

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
    logger.info("[suggest] ✅ send_photo done TOTAL %dms", int((time.time() - t0) * 1000))


# =============================================================================
# AUTO-PUB : sélection galerie + schedule automatique à l'heure optimale
# =============================================================================

def _send_scheduled_list(chat_id: int) -> None:
    """Affiche les posts programmés en attente avec bouton d'annulation."""
    import datetime as dt
    posts = list_scheduled_posts(chat_id)
    if not posts:
        send_message(chat_id, "📅 Aucun post programmé en attente.")
        return

    tz_local = now_local().tzinfo
    lines = [f"📅 *{len(posts)} post(s) en attente :*\n"]
    kb = []
    for p in posts:
        galerie = _gallery_from_url(p["image_url"])
        config = load_yaml_config()
        name = _display_name_for(galerie, config)
        first_line = (p["caption"] or "").split("\n")[0][:50]
        try:
            dt_local = from_utc_str(p["run_at"]).astimezone(tz_local)
            run_str = dt_local.strftime("%d/%m à %H:%M")
        except Exception:
            run_str = p["run_at"]
        lines.append(f"• *{name}* — {run_str}\n  _{first_line}…_")
        kb.append([{"text": f"❌ Annuler {name} ({run_str})",
                    "callback_data": f"cancel_sched_{p['id']}"}])

    kb.append([{"text": "⬅️ Menu", "callback_data": "menu"}])
    send_message(chat_id, "\n".join(lines), reply_markup={"inline_keyboard": kb})


def _send_daily_menu(chat_id: int) -> None:
    """Sous-menu : active/désactive l'auto-pub quotidien par galerie."""
    config = load_yaml_config()
    galeries = config.get("galeries", [])
    keyboard = []
    for g in galeries:
        display = _display_name_for(g, config)
        if is_daily_autopub_active(chat_id, g):
            keyboard.append([{"text": f"✅ {display} — désactiver", "callback_data": f"daily_off_{g}"}])
        else:
            keyboard.append([{"text": f"▶️ {display}", "callback_data": f"daily_on_{g}"}])
    keyboard.append([{"text": "⬅️ Menu", "callback_data": "menu"}])
    send_message(
        chat_id,
        "🔄 *Auto-pub quotidien*\n"
        "_Active une galerie : je publierai automatiquement une photo chaque jour sans que tu aies à intervenir._\n\n"
        "✅ = actif (cliquer pour désactiver) · ▶️ = inactif",
        reply_markup={"inline_keyboard": keyboard},
    )


def _send_autopub_menu(chat_id: int) -> None:
    """Sous-menu : liste des galeries pour l'auto-pub."""
    config = load_yaml_config()
    galeries = config.get("galeries", [])
    buttons = [
        {"text": f"🤖 {_display_name_for(g, config)}", "callback_data": f"autopub_{g}"}
        for g in galeries
    ]
    keyboard = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    keyboard.append([{"text": "⬅️ Menu", "callback_data": "menu"}])
    send_message(
        chat_id,
        "🤖 *Auto-pub* — Choisis une galerie :\n"
        "_Je sélectionne une photo, génère la légende et programme à l'heure optimale._",
        reply_markup={"inline_keyboard": keyboard},
    )


def _auto_publish_flow(chat_id: int, galerie: str) -> None:
    """Sélectionne une photo, génère la caption et propose un créneau à confirmer."""
    t0 = time.time()
    config = load_yaml_config()
    display = _display_name_for(galerie, config)
    logger.info("[autopub] start galerie=%s", galerie)

    send_message(chat_id, f"🤖 *Auto-pub {display}* — sélection en cours…")

    with _typing_while(chat_id):
        img_url = pick_unseen_photo(config["site_url"], galerie)
        if not img_url:
            send_message(chat_id, f"⚠️ *{display}* est épuisée — toutes les photos ont déjà été envoyées.")
            return
        full_content = generate_caption(img_url, galerie)

    visible_caption, _alt = split_content(full_content)
    logger.info("[autopub] caption %d chars (+%dms)", len(full_content), int((time.time()-t0)*1000))

    # Meilleure heure : galerie d'abord, puis toutes galeries, puis créneaux par défaut
    hour = best_posting_hour(galerie) or best_posting_hour()
    if hour is None:
        now_h = now_local().hour
        hour = next((h for h in _DEFAULT_SLOTS if h > now_h), _DEFAULT_SLOTS[0])
        source = "créneau par défaut"
    else:
        source = "heure optimale"
    logger.info("[autopub] heure proposée=%dh (%s)", hour, source)

    target_local = _next_slot_for_hour(hour)
    day_label = "aujourd'hui" if target_local.date() == now_local().date() else "demain"
    time_label = target_local.strftime("%H:%M")

    # Sauvegarde session (même mécanique que _send_suggestion)
    save_session(chat_id, img_url, full_content)

    # Preview + boutons de confirmation
    kb = [
        [{"text": f"✅ Programmer {day_label} à {time_label}", "callback_data": f"autopub_ok_{hour}"}],
        [{"text": "📅 Autre heure", "callback_data": "schedule_btn"},
         {"text": "🚀 Publier maintenant", "callback_data": "pub_both"}],
        [{"text": "✍️ Éditer légende", "callback_data": "manual_edit"}],
        [{"text": "❌ Annuler", "callback_data": "autopub_cancel"}],
    ]
    header = f"🤖 *{display}* — {source}\n📅 Proposé : *{day_label} à {time_label}*\n\n"
    preview = visible_caption[:450] + ("…" if len(visible_caption) > 450 else "")
    send_photo(chat_id, img_url, header + preview, reply_markup={"inline_keyboard": kb})


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
