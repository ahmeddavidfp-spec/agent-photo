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

import atexit
import csv
import datetime as dt
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator, List, Optional, Tuple

from settings import BACKUP_DB_PATH, DB_PATH, RESTORE_SOURCES

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


# =========================================================================
# SÉRIALISATION DES ACCÈS DB
# =========================================================================
# L'app tourne en UN SEUL process (gunicorn --workers 1) : tous les accès à
# photos.db viennent d'ici (threads web + scheduler). On sérialise donc TOUT
# accès SQLite derrière un verrou process-wide → une seule connexion à la
# fois → "database is locked" devient impossible par construction. Sur le
# disque Render lent (NFS, pas de WAL), la contention SQLite provoquait des
# embouteillages de 20s+ (un commit poussif garde ses verrous et bloque même
# les lecteurs).
#
# ⚠️ RÈGLE ABSOLUE (leçon du deadlock historique, voir doc §10) : RIEN de
# lent ne doit JAMAIS s'exécuter dans un bloc `with connection()` — pas
# d'appel réseau, pas de sleep, pas d'OpenAI. Ouvrir → lire/écrire → sortir.
# Le verrou n'est tenu que quelques millisecondes (+ fsync du commit).
_DB_LOCK = threading.RLock()
_LOCK_TIMEOUT = 30  # filet de sécurité : échec clair plutôt que blocage infini


_CONN: Optional[sqlite3.Connection] = None


# --- Sauvegarde/restauration vers le disque persistant (/data, NFS fragile) --
# La DB vive est LOCALE (DB_PATH). Tout accès à /data (qui peut GELER au
# niveau OS) est confiné : restauration au boot bornée par un timeout,
# sauvegardes dans un thread jetable. Le bot lui-même ne touche jamais /data.
_BACKUPS_ENABLED = True
_BACKUP_MIN_INTERVAL = 300          # 5 min entre sauvegardes
_commit_count = 0                   # incrémenté à chaque commit (détection "dirty")
_backup_state = {"inflight": False, "last_ts": 0.0, "last_commit": -1}


def _restore_at_boot() -> None:
    """Restaure la DB locale depuis /data (borné : jamais plus de 60s).

    Tout se passe dans un thread jetable (même os.path.exists peut geler sur
    un NFS malade). Si la restauration échoue/gèle : DB vierge + sauvegardes
    DÉSACTIVÉES pour ce boot (on n'écrase jamais un bon backup avec du vide).
    """
    global _BACKUPS_ENABLED
    if os.path.exists(DB_PATH) or not RESTORE_SOURCES:
        return
    result: dict = {}

    def work():
        try:
            for src in RESTORE_SOURCES:
                if os.path.exists(src):
                    shutil.copyfile(src, DB_PATH + ".restoring")
                    os.replace(DB_PATH + ".restoring", DB_PATH)
                    result["src"] = src
                    return
            result["src"] = None  # aucun backup → première installation
        except Exception as e:
            result["err"] = str(e)

    t = threading.Thread(target=work, daemon=True, name="db-restore")
    t.start()
    t.join(timeout=60)
    if t.is_alive() or "err" in result:
        _BACKUPS_ENABLED = False
        logger.error(
            "⚠️ Restore DB depuis /data IMPOSSIBLE (%s) — démarrage sur DB "
            "VIERGE, sauvegardes désactivées ce boot (protection du backup). "
            "Redémarre l'instance quand le disque répond pour récupérer l'historique.",
            result.get("err", "accès /data gelé"),
        )
    elif result.get("src"):
        logger.info("DB restaurée depuis %s → %s", result["src"], DB_PATH)
    else:
        logger.info("Aucun backup sur /data — nouvelle DB locale (première installation)")


def _do_backup() -> None:
    """Snapshot cohérent (local, sous verrou, ms) puis upload vers /data.

    L'upload NFS peut geler : ce thread est jetable, le flag inflight évite
    l'empilement. `os.replace` rend le remplacement du backup atomique."""
    global _backup_state
    try:
        snap = DB_PATH + ".snap"
        with _DB_LOCK:
            dst = sqlite3.connect(snap)
            _get_conn().backup(dst)
            dst.close()
            commits_at_snap = _commit_count
        tmp = BACKUP_DB_PATH + ".tmp"
        shutil.copyfile(snap, tmp)          # ← seul point qui peut geler (NFS)
        os.replace(tmp, BACKUP_DB_PATH)
        os.remove(snap)
        _backup_state["last_ts"] = time.time()
        _backup_state["last_commit"] = commits_at_snap
        logger.info("💾 Backup DB → %s (commit #%d)", BACKUP_DB_PATH, commits_at_snap)
    except Exception as e:
        logger.warning("Backup DB échoué : %s", e)
    finally:
        _backup_state["inflight"] = False


def maybe_backup_db(force: bool = False) -> None:
    """Appelé par le scheduler (toutes les 30s) : sauvegarde si nécessaire.

    Conditions : activé + pas déjà en cours + ≥5 min depuis la dernière +
    au moins un commit depuis. `force=True` ignore l'intervalle (shutdown)."""
    if not (BACKUP_DB_PATH and _BACKUPS_ENABLED) or _backup_state["inflight"]:
        return
    if not force and time.time() - _backup_state["last_ts"] < _BACKUP_MIN_INTERVAL:
        return
    if _commit_count == _backup_state["last_commit"]:
        return  # rien de nouveau à sauver
    _backup_state["inflight"] = True
    threading.Thread(target=_do_backup, daemon=True, name="db-backup").start()


def record_discovered_gallery(slug: str) -> bool:
    """Insère `slug` en 'pending' s'il est inconnu. True si nouvellement vu."""
    with connection() as conn:
        if conn.execute("SELECT 1 FROM discovered_galleries WHERE slug=?", (slug,)).fetchone():
            return False
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("INSERT INTO discovered_galleries (slug, status, seen_at) "
                     "VALUES (?, 'pending', ?)", (slug, now))
        return True


def set_discovered_status(slug: str, status: str) -> None:
    with connection() as conn:
        conn.execute("UPDATE discovered_galleries SET status=? WHERE slug=?", (status, slug))


def known_discovered() -> set:
    """Tous les slugs déjà vus (pending/added/ignored) — à ne pas re-scanner."""
    with connection() as conn:
        return {r[0] for r in conn.execute("SELECT slug FROM discovered_galleries")}


def added_galleries() -> List[str]:
    """Galeries découvertes ET approuvées par l'utilisateur (status='added')."""
    with connection() as conn:
        return [r[0] for r in conn.execute(
            "SELECT slug FROM discovered_galleries WHERE status='added'")]


def pending_galleries() -> List[str]:
    """Galeries détectées en attente de décision (status='pending')."""
    with connection() as conn:
        return [r[0] for r in conn.execute(
            "SELECT slug FROM discovered_galleries WHERE status='pending' "
            "ORDER BY seen_at DESC")]


def _final_backup() -> None:
    """Best effort à l'arrêt propre (deploy/SIGTERM) : sauvegarde synchrone.

    Rend la fenêtre de perte quasi nulle sur les redéploiements. Si /data gèle
    à ce moment, gunicorn tuera le process après son délai de grâce — tant pis,
    la sauvegarde périodique précédente fait foi."""
    if (BACKUP_DB_PATH and _BACKUPS_ENABLED and _CONN is not None
            and _commit_count != _backup_state["last_commit"]):
        _backup_state["inflight"] = True
        _do_backup()


atexit.register(_final_backup)


def _get_conn() -> sqlite3.Connection:
    """Connexion SQLite persistante UNIQUE (protégée par _DB_LOCK).

    Sur le disque /data (NFS) : ouvrir/fermer une connexion par opération =
    négociation de verrous fichier à chaque fois, et chaque commit en mode
    DELETE crée/fsync/supprime un fichier -journal — opérations qui peuvent
    PENDRE quand le NFS est dégradé (c'est ce qui gelait le bot). Remède :
    - une seule connexion, ouverte une fois (check_same_thread=False, on est
      sérialisés par _DB_LOCK, un seul process) ;
    - journal en MÉMOIRE : plus aucun fichier -journal sur le disque.
    Trade-off journal MEMORY : un crash process PILE pendant un commit peut
    corrompre la DB → fenêtre microscopique (transactions minuscules) et DB
    reconstructible (runbook §9 ; tokens re-régénérables via /renew).
    """
    global _CONN
    if _CONN is None:
        _restore_at_boot()
        conn = sqlite3.connect(DB_PATH, timeout=3.0, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=3000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA journal_mode=MEMORY")
        _CONN = conn
    return _CONN


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    global _CONN
    if not _DB_LOCK.acquire(timeout=_LOCK_TIMEOUT):
        raise sqlite3.OperationalError(
            f"DB serialization lock non obtenu en {_LOCK_TIMEOUT}s "
            "(un bloc connection() fait-il quelque chose de lent ?)"
        )
    try:
        conn = _get_conn()
        try:
            yield conn
            conn.commit()
            global _commit_count
            _commit_count += 1  # marque la DB "dirty" pour la sauvegarde
        except Exception:
            # Laisse la connexion persistante propre ; si elle est morte
            # (erreur disque), on la jette — la prochaine opération ré-ouvre.
            try:
                conn.rollback()
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                _CONN = None
            raise
    finally:
        _DB_LOCK.release()


def init_db() -> None:
    """Création idempotente des tables + index.

    NB : le journal_mode est géré par la connexion persistante (_get_conn →
    MEMORY, voir plus haut). Ne PAS forcer DELETE ici — ça écraserait MEMORY.
    """
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
        conn.execute(
            "CREATE TABLE IF NOT EXISTS discovered_galleries ("
            "slug TEXT PRIMARY KEY, status TEXT, seen_at TEXT)"
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
