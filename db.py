"""Couche base de données SQLite.

Schema :
- sent_photos(url PK, galerie, date_envoi)
- current_session(chat_id PK, last_url, last_caption)
- scheduled_posts(id PK, chat_id, image_url, caption, run_at, status)
- token_store(key PK, value, updated_at)   ← tokens Meta renouvelés
- post_metrics(id PK, platform, media_id, image_url, caption, galerie,
               published_at, metrics_json, collected_at)
               ↑ pour la boucle d'apprentissage (engagement → captions futures)

Tous les accès passent par un context manager `connection()` qui garantit
la fermeture et le commit.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator, List, Optional, Tuple

from settings import DB_PATH

logger = logging.getLogger(__name__)

# Concurrence DB : on s'en remet à SQLite + busy_timeout.
#
# Historique : on avait ajouté un threading.RLock() pour sérialiser les
# accès depuis Python, mais ça créait des deadlocks silencieux (un thread
# qui tenait le lock pendant un appel réseau faisait timeout tous les
# autres). SQLite gère très bien la concurrence avec busy_timeout + un
# seul worker gunicorn : on laisse faire.
#
# Règle d'or appliquée partout : JAMAIS d'appel réseau / d'opération
# lente à l'intérieur d'un `with connection()`. On ouvre, on lit/écrit,
# on ferme — rien d'autre.


def _commit_with_retry(conn: sqlite3.Connection, attempts: int = 6) -> None:
    """Commit résilient à 'database is locked'.

    Le disque /data de Render est lent (type NFS) et sans WAL possible : sous
    contention, un commit peut échouer. On préfère plusieurs tentatives courtes
    avec backoff (on attrape une fenêtre libre) qu'une seule longue attente.
    """
    for i in range(attempts):
        try:
            conn.commit()
            return
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and i < attempts - 1:
                time.sleep(0.25 * (i + 1))  # 0.25,0.5,0.75,1.0,1.25 → ~3.75s cumulés
                continue
            raise


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    # Disque Render lent (type NFS), pas de WAL (-shm/-wal non supportés).
    # busy_timeout court (3s) : on échoue vite puis on réessaie le commit avec
    # backoff (voir _commit_with_retry) → bien plus robuste sous contention.
    # synchronous=NORMAL : commits plus rapides (moins de fsync) ; sûr face à un
    # crash process (recovery par journal), seule une panne d'alim pourrait poser
    # souci — négligeable sur Render et la DB est reconstructible.
    conn = sqlite3.connect(DB_PATH, timeout=3.0)
    try:
        conn.execute("PRAGMA busy_timeout=3000")
        conn.execute("PRAGMA synchronous=NORMAL")
        yield conn
        _commit_with_retry(conn)
    finally:
        conn.close()


def init_db() -> None:
    """Création idempotente des tables + index. Force journal_mode=DELETE."""
    with connection() as conn:
        # One-shot au démarrage : si la DB traîne en WAL depuis une tentative
        # précédente, on la repasse en DELETE. Idempotent si déjà DELETE.
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
        except Exception as e:
            logger.warning("PRAGMA journal_mode=DELETE failed: %s", e)
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
        conn.execute(
            "CREATE TABLE IF NOT EXISTS post_metrics ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "platform TEXT, media_id TEXT, image_url TEXT, "
            "caption TEXT, galerie TEXT, "
            "published_at TEXT, metrics_json TEXT, collected_at TEXT)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_metrics_pending "
            "ON post_metrics(collected_at, published_at)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS daily_autopub ("
            "chat_id INTEGER, galerie TEXT, active INTEGER DEFAULT 1, "
            "last_run_date TEXT, created_at TEXT, "
            "PRIMARY KEY (chat_id, galerie))"
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


def list_scheduled_posts(chat_id: int) -> List[dict]:
    """Retourne les posts pending pour ce chat, triés par date."""
    with connection() as conn:
        rows = conn.execute(
            "SELECT id, image_url, caption, run_at FROM scheduled_posts "
            "WHERE chat_id = ? AND status = 'pending' ORDER BY run_at ASC",
            (chat_id,),
        ).fetchall()
    return [{"id": r[0], "image_url": r[1], "caption": r[2], "run_at": r[3]} for r in rows]


def cancel_scheduled_post(post_id: int, chat_id: int) -> bool:
    """Annule un post programmé. Retourne True si annulé, False si introuvable."""
    with connection() as conn:
        cur = conn.execute(
            "UPDATE scheduled_posts SET status = 'cancelled' "
            "WHERE id = ? AND chat_id = ? AND status = 'pending'",
            (post_id, chat_id),
        )
        return cur.rowcount > 0


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


# --- Metrics / boucle d'apprentissage ---------------------------------------
# On enregistre chaque publication réussie. Un job scheduler récupère
# les insights Meta 24h+ après publish, stocke le JSON dans metrics_json,
# et top_performers() renvoie les meilleures captions historiques (score
# combiné likes+comments+saved) pour nourrir les prompts Claude futurs.

def record_published_post(
    platform: str, media_id: str, image_url: str,
    caption: str, galerie: str,
) -> int:
    """Enregistre une publication réussie. metrics_json reste NULL jusqu'à collecte."""
    with connection() as conn:
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur = conn.execute(
            "INSERT INTO post_metrics "
            "(platform, media_id, image_url, caption, galerie, published_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (platform, media_id, image_url, caption, galerie, now),
        )
        return cur.lastrowid


def posts_pending_metrics(older_than_hours: int = 24) -> List[Tuple[int, str, str]]:
    """Retourne les (id, platform, media_id) à collecter.

    Critères :
    - metrics_json IS NULL (pas encore collecté)
    - published_at < now - older_than_hours (laisser le post mûrir)
    """
    cutoff = (dt.datetime.now() - dt.timedelta(hours=older_than_hours)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with connection() as conn:
        return conn.execute(
            "SELECT id, platform, media_id FROM post_metrics "
            "WHERE metrics_json IS NULL AND published_at <= ? "
            "ORDER BY published_at ASC LIMIT 50",
            (cutoff,),
        ).fetchall()


def save_metrics(post_id: int, metrics: dict) -> None:
    """Stocke les metrics collectées (dict → JSON)."""
    with connection() as conn:
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE post_metrics SET metrics_json = ?, collected_at = ? WHERE id = ?",
            (json.dumps(metrics, ensure_ascii=False), now, post_id),
        )


def _engagement_score(metrics: dict) -> float:
    """Score simple et robuste across IG/TH.

    IG renvoie: like_count, comments_count, saved, reach
    TH renvoie: views, likes, replies, reposts, quotes

    On pondère : save/repost (intention forte) > comment/reply > like.
    On divise par reach/views pour normaliser (un post avec 10 likes sur
    100 vues vaut plus qu'un post avec 10 likes sur 1000).
    """
    m = metrics or {}
    # Numérateur (interactions pondérées)
    likes = m.get("like_count", 0) or m.get("likes", 0) or 0
    comments = m.get("comments_count", 0) or m.get("replies", 0) or 0
    saves = m.get("saved", 0) or m.get("reposts", 0) or m.get("quotes", 0) or 0
    engagement = likes * 1.0 + comments * 3.0 + saves * 5.0
    # Dénominateur (reach/views)
    reach = m.get("reach", 0) or m.get("views", 0) or 0
    if reach <= 0:
        return float(engagement)  # pas de reach → score brut
    return engagement / reach * 100.0  # en %


def top_performers(limit: int = 3) -> List[dict]:
    """Retourne les meilleures captions historiques (score combiné).

    Renvoie une liste de dicts : {caption, galerie, score, platform}.
    """
    with connection() as conn:
        rows = conn.execute(
            "SELECT caption, galerie, platform, metrics_json "
            "FROM post_metrics WHERE metrics_json IS NOT NULL"
        ).fetchall()
    if not rows:
        return []
    scored = []
    for caption, galerie, platform, metrics_json in rows:
        try:
            metrics = json.loads(metrics_json) if metrics_json else {}
        except (json.JSONDecodeError, TypeError):
            metrics = {}
        score = _engagement_score(metrics)
        if score <= 0:
            continue
        scored.append({
            "caption": caption,
            "galerie": galerie,
            "platform": platform,
            "score": score,
        })
    scored.sort(key=lambda d: d["score"], reverse=True)
    return scored[:limit]


def best_posting_hour(galerie: Optional[str] = None) -> Optional[int]:
    """Retourne l'heure locale Europe/Brussels (0-23) avec le meilleur engagement historique.

    Analyse published_at + metrics_json. Retourne None si moins de 5 posts exploitables
    (pas assez de données — l'appelant choisit ses propres créneaux par défaut).

    published_at est stocké via datetime.now() (UTC sur Render) ; on convertit en
    heure locale avant d'agréger.
    """
    from collections import defaultdict
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo  # type: ignore

    TZ_LOCAL = ZoneInfo("Europe/Brussels")
    TZ_UTC = ZoneInfo("UTC")

    with connection() as conn:
        if galerie:
            rows = conn.execute(
                "SELECT published_at, metrics_json FROM post_metrics "
                "WHERE metrics_json IS NOT NULL AND galerie = ?",
                (galerie,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT published_at, metrics_json FROM post_metrics "
                "WHERE metrics_json IS NOT NULL",
            ).fetchall()

    if len(rows) < 5:
        return None

    hour_scores: dict[int, list[float]] = defaultdict(list)
    for published_at, metrics_json in rows:
        try:
            score = _engagement_score(json.loads(metrics_json) if metrics_json else {})
            if score <= 0:
                continue
            dt_utc = dt.datetime.strptime(published_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_UTC)
            hour_scores[dt_utc.astimezone(TZ_LOCAL).hour].append(score)
        except Exception:
            continue

    if not hour_scores:
        return None

    return max(hour_scores, key=lambda h: sum(hour_scores[h]) / len(hour_scores[h]))


# --- Auto-pub quotidien ---

def set_daily_autopub(chat_id: int, galerie: str, active: bool) -> None:
    with connection() as conn:
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if active:
            # Exclusivité : une seule galerie en auto-pub quotidien par chat.
            # Activer une galerie désactive automatiquement toutes les autres,
            # sinon les anciens abonnements (ex. New York) continuent de publier.
            conn.execute(
                "UPDATE daily_autopub SET active = 0 WHERE chat_id = ? AND galerie != ?",
                (chat_id, galerie),
            )
        conn.execute(
            "INSERT INTO daily_autopub (chat_id, galerie, active, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id, galerie) DO UPDATE SET active = excluded.active",
            (chat_id, galerie, int(active), now),
        )


def is_daily_autopub_active(chat_id: int, galerie: str) -> bool:
    with connection() as conn:
        row = conn.execute(
            "SELECT active FROM daily_autopub WHERE chat_id = ? AND galerie = ?",
            (chat_id, galerie),
        ).fetchone()
    return bool(row and row[0])


def get_active_daily_autopubs() -> List[Tuple[int, str]]:
    """Retourne [(chat_id, galerie)] pour tous les abonnements actifs."""
    with connection() as conn:
        return conn.execute(
            "SELECT chat_id, galerie FROM daily_autopub WHERE active = 1"
        ).fetchall()


def get_daily_autopub_last_run(chat_id: int, galerie: str) -> Optional[str]:
    with connection() as conn:
        row = conn.execute(
            "SELECT last_run_date FROM daily_autopub WHERE chat_id = ? AND galerie = ?",
            (chat_id, galerie),
        ).fetchone()
    return row[0] if row else None


def set_daily_autopub_last_run(chat_id: int, galerie: str, date_str: str) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE daily_autopub SET last_run_date = ? WHERE chat_id = ? AND galerie = ?",
            (date_str, chat_id, galerie),
        )
