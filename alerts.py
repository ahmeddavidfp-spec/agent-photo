"""Alertes Telegram : expire-t-on bientôt des tokens Meta ?

Logique : 1×/jour, le scheduler appelle `send_token_alert_if_needed()`.
Si IG ou TH passent sous ALERT_THRESHOLD_DAYS, on pousse un message Telegram
au chat autorisé pour prévenir l'utilisateur.

On n'utilise pas de flag "déjà alerté" : envoyer l'alerte tous les jours pendant
les 10 derniers jours est volontaire (c'est un rappel, pas un one-shot).
"""
import logging
from typing import Optional

from meta_api import token_days_left
from settings import ALLOWED_CHAT_ID
from telegram_bot import send_message

logger = logging.getLogger(__name__)

# Seuil en jours. En dessous, on ping l'utilisateur.
ALERT_THRESHOLD_DAYS = 10


def _build_alert_message() -> Optional[str]:
    """Retourne le message d'alerte si nécessaire, sinon None."""
    alerts = []
    for label in ("IG", "TH", "FB"):
        days = token_days_left(label)
        if days is None or days >= 9999:  # 9999 = token sans expiration
            continue
        if days < ALERT_THRESHOLD_DAYS:
            alerts.append(f"⚠️ **{label}** expire dans **{days}j**")
    if not alerts:
        return None

    body = "\n".join(alerts)
    body += (
        "\n\n**Action** :\n"
        "• **IG** : Graph API Explorer → régénère un long-lived user token, "
        "et colle dans `IG_ACCESS_TOKEN` sur Render.\n"
        "• **TH** : tape `/renew_threads` dans ce chat (auto).\n"
        "• **FB** : Studio → État → Renouveler FB (auto)."
    )
    return body


def send_token_alert_if_needed() -> None:
    """Envoie une alerte Telegram si un token est bientôt expiré."""
    if not ALLOWED_CHAT_ID:
        logger.debug("ALLOWED_CHAT_ID absent — pas d'alerte envoyée.")
        return
    msg = _build_alert_message()
    if not msg:
        return
    try:
        send_message(int(ALLOWED_CHAT_ID), msg)
        logger.info("Alerte d'expiration envoyée sur Telegram.")
    except Exception as e:
        logger.error("Envoi alerte échoué: %s", e)
