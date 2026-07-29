"""Wrappers fins autour de l'API Bot Telegram."""
import logging
from typing import Optional

from http_client import safe_post
from settings import TELEGRAM_TOKEN, TG_API

logger = logging.getLogger(__name__)


def _url(path: str) -> str:
    return f"{TG_API}{TELEGRAM_TOKEN}/{path}"


def send_message(chat_id: int, text: str, reply_markup: Optional[dict] = None,
                 parse_mode: str = "Markdown") -> None:
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN manquant")
        return

    def _post(pm):
        payload = {"chat_id": chat_id, "text": text}
        if pm:
            payload["parse_mode"] = pm
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return safe_post(_url("sendMessage"), json=payload)

    try:
        r = _post(parse_mode)
        if r.status_code == 200:
            return
        body = r.text[:250]
        # Un 400 avec parse_mode = Markdown mal formé (souvent un _ * [ ` dans un
        # message d'erreur ou un slug). On NE veut PAS perdre le message → on le
        # renvoie en texte brut. Sinon le filet de sécurité "❌ Erreur interne"
        # lui-même pouvait disparaître silencieusement.
        if r.status_code == 400 and parse_mode:
            logger.warning("send_message 400 (%s) → retry en texte brut", body)
            r2 = _post(None)
            if r2.status_code != 200:
                logger.error("send_message échec définitif HTTP %s : %s",
                             r2.status_code, r2.text[:200])
            return
        logger.error("send_message HTTP %s : %s", r.status_code, body)
    except Exception as e:
        logger.error("send_message failed: %s", e)


def send_photo(chat_id: int, photo_url: str, caption: str,
               reply_markup: Optional[dict] = None) -> None:
    if not TELEGRAM_TOKEN:
        return
    payload = {"chat_id": chat_id, "photo": photo_url, "caption": caption}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = safe_post(_url("sendPhoto"), json=payload)
        if r.status_code != 200:   # ex. légende > 1024 car. → ne pas rester muet
            logger.error("send_photo HTTP %s : %s", r.status_code, r.text[:250])
    except Exception as e:
        logger.error("send_photo failed: %s", e)


def answer_callback_query(callback_query_id: str, text: str = "") -> None:
    """Ack un callback Telegram (inline button) pour arrêter le spinner.

    Sans cet appel, Telegram pense que le bot n'a pas reçu le clic et
    retransmet la même callback après ~10-15s → double exécution.
    """
    if not TELEGRAM_TOKEN or not callback_query_id:
        return
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        safe_post(_url("answerCallbackQuery"), json=payload, timeout=5)
    except Exception as e:
        logger.warning("answer_callback_query failed: %s", e)


def send_typing_action(chat_id: int) -> None:
    """Affiche l'indicateur 'en train d'écrire' dans Telegram (dure ~5s)."""
    if not TELEGRAM_TOKEN:
        return
    try:
        safe_post(_url("sendChatAction"),
                  json={"chat_id": chat_id, "action": "typing"}, timeout=5)
    except Exception:
        pass


def send_document(chat_id: int, path: str) -> None:
    if not TELEGRAM_TOKEN:
        return
    try:
        with open(path, "rb") as f:
            safe_post(
                _url("sendDocument"),
                data={"chat_id": chat_id},
                files={"document": f},
                timeout=30,
            )
    except Exception as e:
        logger.error("send_document failed: %s", e)


def send_video(chat_id: int, path: str, caption: str = "") -> None:
    """Envoie une vidéo (MP4) jouable dans Telegram (ex. un Reel généré).

    On lit le fichier EN MÉMOIRE (bytes) et non comme handle ouvert : sinon les
    retries urllib3 (connexion Telegram capricieuse depuis Render) ne peuvent pas
    renvoyer un fichier déjà consommé. Timeout large car l'upload est plus lourd.
    """
    if not TELEGRAM_TOKEN:
        return
    try:
        with open(path, "rb") as f:
            video_bytes = f.read()
        r = safe_post(
            _url("sendVideo"),
            data={"chat_id": chat_id, "caption": caption, "parse_mode": "Markdown"},
            files={"video": ("reel.mp4", video_bytes, "video/mp4")},
            timeout=180,
        )
        if r.status_code != 200:
            logger.warning("send_video HTTP %s : %s", r.status_code, r.text[:200])
    except Exception as e:
        logger.error("send_video failed: %s", e)
