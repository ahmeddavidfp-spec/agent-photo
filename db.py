"""Couche base de données SQLite.

Schema :
- sent_photos(url PK, galerie, date_envoi)
- current_session(chat_id PK, last_url, last_caption)
- scheduled_posts(id PK, chat_id, image_url, caption, run_at, status)
- token_store(key PK, value, updated_at)   ← tokens Meta renouvelés

Tous les accès passent par un context manager `connection()` qui garantit
la fermeture et le commit.
"""
from __future__ import annotations

import csv
import datetime as dt
import logging
import sqlite3
from contextlib import contextmanager
from typing import Iterator, Optional, Tuple

from settings import DB_PATH

logger = logging.getLogger(__name__)


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Création idempotente des tables + index."""
    with connection() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sent_photos ("
            "url TEXT PRIMARY KEY, galerie TEXT, date_envoi TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS current_session ("
            "chat_id INTEGER PRIMARY KEY, last_url TEXT, last_caption TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS scheduled_posts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "chat_id INTEGER, image_url TEXT, caption TEXT, "
            "run_at TEXT, status TEXT DEFAULT 'pending')"
        )
        # Index pour le scheduler (tri rapide des pending par date)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_scheduled_status_run "
            "ON scheduled_posts(status, run_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sent_galerie "
            "ON sent_photos(galerie)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS token_store ("
            "key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)"
        )

        # Migrations douces : ajout de colonnes si manquantes
        cols = {row[1] for row in conn.execute("PRAGMA table_info(sent_photos)")}
        if "galerie" not in cols:
            conn.execute("ALTER TABLE sent_photos ADD COLUMN galerie TEXT")
        if "date_envoi" not in cols:
            conn.execute("ALTER TABLE sent_photos ADD COLUMN date_envoi TEXT")


# --- Sessions utilisateur ---

def get_session(chat_id: int) -> Optional[Tuple[str, str]]:
    with connection() as conn:
        return conn.execute(
            "SELECT last_url, last_caption FROM current_session WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()


def save_session(chat_id: int, url: str, caption: str) -> None:
    with connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO current_session VALUES (?, ?, ?)",
            (chat_id, url, caption),
        )


def clear_session(chat_id: int) -> None:
    with connection() as conn:
        conn.execute("DELETE FROM current_session WHERE chat_id = ?", (chat_id,))


# --- Historique des photos publiées ---

def mark_photo_as_sent(url: str, galerie: str) -> None:
    with connection() as conn:
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT OR IGNORE INTO sent_photos (url, galerie, date_envoi) VALUES (?, ?, ?)",
            (url, galerie, now),
        )


def already_sent_urls() -> set[str]:
    with connection() as conn:
        return {row[0] for row in conn.execute("SELECT url FROM sent_photos")}


def galerie_last_use(galerie: str) -> Optional[dt.datetime]:
    """Retourne la date du dernier envoi pour cette galerie (pour éviter les répétitions)."""
    with connection() as conn:
        row = conn.execute(
            "SELECT date_envoi FROM sent_photos WHERE galerie = ? "
            "ORDER BY date_envoi DESC LIMIT 1",
            (galerie,),
        ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return dt.datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def get_stats() -> str:
    with connection() as conn:
        rows = conn.execute(
            "SELECT galerie, COUNT(*) FROM sent_photos GROUP BY galerie ORDER BY 2 DESC"
        ).fetchall()
    if not rows:
        return "Base vide."
    lines = ["📁 **RESUME :**"]
    for galerie, count in rows:
        name = (galerie or "Inconnue").capitalize()
        lines.append(f"- {name} : {count}")
    return "\n".join(lines)


def export_to_csv(path: str = "/tmp/export.csv") -> str:
    with connection() as conn:
        cur = conn.execute("SELECT url, galerie, date_envoi FROM sent_photos")
        rows = cur.fetchall()
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["url", "galerie", "date"])
        writer.writerows(rows)
    return path


# --- Planificateur ---

def schedule_post(chat_id: int, image_url: str, caption: str, run_at_utc: dt.datetime) -> int:
    with connection() as conn:
        cur = conn.execute(
            "INSERT INTO scheduled_posts (chat_id, image_url, caption, run_at) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, image_url, caption, run_at_utc.strftime("%Y-%m-%d %H:%M:%S")),
        )
        return cur.lastrowid


def due_scheduled_posts(now_utc: dt.datetime) -> list[tuple]:
    """Retourne les posts pending dont run_at est passé."""
    with connection() as conn:
        return conn.execute(
            "SELECT id, chat_id, image_url, caption, run_at "
            "FROM scheduled_posts WHERE status = 'pending' AND run_at <= ? "
            "ORDER BY run_at ASC",
            (now_utc.strftime("%Y-%m-%d %H:%M:%S"),),
        ).fetchall()


def set_scheduled_status(post_id: int, status: str) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE scheduled_posts SET status = ? WHERE id = ?",
            (status, post_id),
        )


# --- Token store (persistance des tokens Meta renouvelés) ---
# Permet au cron de sauvegarder automatiquement les nouveaux tokens
# sans devoir toucher aux variables d'env Render. Au boot, les fonctions
# de meta_api lisent en priorité le DB, puis retombent sur les env vars
# si rien n'est stocké (bootstrap initial).

def get_stored_token(key: str) -> Optional[str]:
    """Retourne le token stocké en DB, ou None si absent."""
    with connection() as conn:
        row = conn.execute(
            "SELECT value FROM token_store WHERE key = ?", (key,)
        ).fetchone()
    return row[0] if row and row[0] else None


def save_token(key: str, value: str) -> None:
    """Sauvegarde un token renouvelé, avec horodatage."""
    with connection() as conn:
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT OR REPLACE INTO token_store (key, value, updated_at) "
            "VALUES (?, ?, ?)",
            (key, value, now),
        )


def token_last_update(key: str) -> Optional[str]:
    """Retourne la date du dernier refresh (string SQL), ou None."""
    with connection() as conn:
        row = conn.execute(
            "SELECT updated_at FROM token_store WHERE key = ?", (key,)
        ).fetchone()
    return row[0] if row and row[0] else None
