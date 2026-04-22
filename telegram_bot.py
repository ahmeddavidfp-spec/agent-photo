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
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        safe_post(_url("sendMessage"), json=payload)
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
        safe_post(_url("sendPhoto"), json=payload)
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
