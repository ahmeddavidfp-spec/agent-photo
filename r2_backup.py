"""Sauvegarde / restauration de la base SQLite sur Cloudflare R2 (API S3).

Utilisé quand l'app tourne sur Cloudflare Containers : le disque du conteneur est
EPHEMERE, donc la base vive est locale et une COPIE part vers R2 (restaurée au
boot). Même logique que la sauvegarde /data sur Render, mais vers un objet R2.

Inerte si les variables R2_* ne sont pas définies (ex. sur Render, qui utilise
/data) : enabled() renvoie False et db.py garde son chemin fichier habituel.
boto3 n'est importé QUE si R2 est activé (aucun coût sur Render)."""
import logging
import os
import threading

logger = logging.getLogger(__name__)

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "").strip()
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
R2_BUCKET = os.environ.get("R2_BUCKET", "").strip()
R2_KEY = os.environ.get("R2_KEY", "photos_backup.db").strip()

_client = None
_client_lock = threading.Lock()


def enabled() -> bool:
    """R2 configuré ? (les 4 variables essentielles présentes)."""
    return bool(R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_BUCKET)


def _get_client():
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            import boto3
            from botocore.config import Config
            _client = boto3.client(
                "s3",
                endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
                aws_access_key_id=R2_ACCESS_KEY_ID,
                aws_secret_access_key=R2_SECRET_ACCESS_KEY,
                region_name="auto",
                config=Config(
                    retries={"max_attempts": 3, "mode": "standard"},
                    connect_timeout=10, read_timeout=40,
                ),
            )
    return _client


def download(dest_path: str) -> bool:
    """Télécharge la sauvegarde R2 vers dest_path. True si un objet a été récupéré,
    False si absent (première installation) ou en cas d'échec."""
    if not enabled():
        return False
    tmp = dest_path + ".r2dl"
    try:
        _get_client().download_file(R2_BUCKET, R2_KEY, tmp)
        os.replace(tmp, dest_path)
        logger.info("R2 : DB restaurée depuis r2://%s/%s", R2_BUCKET, R2_KEY)
        return True
    except Exception as e:
        s = str(e)
        if "NoSuchKey" in type(e).__name__ or "404" in s or "Not Found" in s:
            logger.info("R2 : aucun backup existant (première installation)")
        else:
            logger.warning("R2 : téléchargement KO : %s", e)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def upload(src_path: str) -> bool:
    """Envoie src_path vers la sauvegarde R2. True si réussi."""
    if not enabled():
        return False
    try:
        _get_client().upload_file(src_path, R2_BUCKET, R2_KEY)
        logger.info("R2 : backup DB → r2://%s/%s", R2_BUCKET, R2_KEY)
        return True
    except Exception as e:
        logger.warning("R2 : upload KO : %s", e)
        return False
