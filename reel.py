"""Génération d'un Reel Instagram (diaporama 9:16) à partir de photos de galerie.

Chemin B (hybride) : le bot MONTE la vidéo et l'envoie sur Telegram ; l'humain
la poste dans l'app IG en ajoutant un son tendance (la meilleure portée). La
vidéo produite est donc **muette** (le son est ajouté côté app).

Aucune dépendance système : le binaire ffmpeg vient de `imageio-ffmpeg` (pip).
Esthétique : chaque photo est centrée (ajustée sans crop) sur un fond flou
d'elle-même — le rendu "pro" standard des Reels photo.
"""
import logging
import os
import shutil
import subprocess
import tempfile
from typing import List, Optional

from PIL import Image, ImageEnhance, ImageFilter

from http_client import safe_get

logger = logging.getLogger(__name__)

# 720x1280 (et non 1080x1920) : Instagram l'accepte et ça divise ~par 2 la
# mémoire d'encodage — critique sur une instance Render 512 Mo.
W, H = 720, 1280           # 9:16 vertical
FPS = 30
SEC_PER_PHOTO = 2.5        # durée d'affichage par photo
FADE = 0.4                 # fondu entrée/sortie (s)
_MARGIN = 40               # marge autour de la photo (px) sur le fond flou


def _download(url: str, dest: str) -> bool:
    try:
        # Squarespace : 1000w suffit pour un rendu 720p et allège la mémoire
        clean = url.split("?")[0] + "?format=1000w"
        r = safe_get(clean, timeout=20)
        r.raise_for_status()
        with open(dest, "wb") as f:
            f.write(r.content)
        return True
    except Exception as e:
        logger.warning("Reel : download KO (%s) : %s", url[:60], e)
        return False


def _normalize(src_path: str, dst_path: str) -> None:
    """Ajuste une photo en WxH sur un fond flou d'elle-même (faible mémoire).

    Optimisations mémoire (instance Render 512 Mo) :
    - `draft` : décodage JPEG à résolution réduite (libjpeg), bien moins de RAM.
    - fond flou calculé en basse résolution (1/4) puis agrandi : le flou masque
      l'upscale, et l'opération de flou coûte ~1/16 de la mémoire.
    """
    img = Image.open(src_path)
    img.draft("RGB", (W, H * 2))   # décodage réduit AVANT chargement des pixels
    img = img.convert("RGB")

    # Fond flou : calculé en petit (W/4 x H/4) puis agrandi
    sw, sh = W // 4, H // 4
    bg = img.copy()
    scale = max(sw / bg.width, sh / bg.height)
    bg = bg.resize((max(1, round(bg.width * scale)), max(1, round(bg.height * scale))),
                   Image.BILINEAR)
    left = (bg.width - sw) // 2
    top = (bg.height - sh) // 2
    bg = bg.crop((left, top, left + sw, top + sh))
    bg = bg.filter(ImageFilter.GaussianBlur(10))
    bg = ImageEnhance.Brightness(bg).enhance(0.5)
    bg = bg.resize((W, H), Image.BILINEAR)  # agrandi (le flou masque l'upscale)

    # Premier plan : ajuster DANS le cadre (sans crop), marge autour
    img.thumbnail((W - 2 * _MARGIN, H - 2 * _MARGIN), Image.LANCZOS)
    bg.paste(img, ((W - img.width) // 2, (H - img.height) // 2))
    bg.save(dst_path, "JPEG", quality=88)
    img.close()
    bg.close()


def _ffmpeg_exe() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _assemble(frame_paths: list[str], out_path: str, sec: float = SEC_PER_PHOTO) -> None:
    """Assemble des frames normalisées (toutes 1080x1920) en un MP4 muet."""
    workdir = os.path.dirname(frame_paths[0])
    # Renomme en séquence pour le demuxer image2 (frame_%04d.jpg)
    for i, p in enumerate(frame_paths):
        seq = os.path.join(workdir, f"seq_{i:04d}.jpg")
        if p != seq:
            os.replace(p, seq)
    total = len(frame_paths) * sec
    vf = (
        f"fps={FPS},format=yuv420p,"
        f"fade=t=in:st=0:d={FADE},fade=t=out:st={total - FADE:.2f}:d={FADE}"
    )
    cmd = [
        _ffmpeg_exe(), "-y",
        "-framerate", f"1/{sec}",
        "-i", os.path.join(workdir, "seq_%04d.jpg"),
        "-vf", vf,
        # ultrafast + threads 1 : plafonne la mémoire de l'encodeur (Render 512 Mo)
        "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        out_path,
    ]
    logger.info("Reel : ffmpeg assemble %d frames → %s", len(frame_paths), out_path)
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {res.stderr[-400:]}")


def build_reel_from_paths(image_paths: list[str], out_path: str,
                          sec: float = SEC_PER_PHOTO) -> str:
    """Monte un Reel à partir de fichiers image locaux. Retourne out_path."""
    if len(image_paths) < 2:
        raise ValueError("Au moins 2 photos requises pour un Reel")
    workdir = tempfile.mkdtemp(prefix="reelwork_")
    try:
        frames = []
        for i, src in enumerate(image_paths):
            dst = os.path.join(workdir, f"frame_{i:04d}.jpg")
            _normalize(src, dst)
            frames.append(dst)
        _assemble(frames, out_path, sec)
        return out_path
    finally:
        shutil.rmtree(workdir, ignore_errors=True)  # frames temporaires


def build_reel(image_urls: List[str], out_path: Optional[str] = None,
               sec: float = SEC_PER_PHOTO) -> Optional[str]:
    """Télécharge les photos et monte un Reel muet 9:16. Retourne le chemin MP4
    ou None si échec (téléchargements insuffisants / erreur ffmpeg)."""
    urls = [u for u in (image_urls or []) if u]
    if len(urls) < 2:
        logger.warning("Reel : moins de 2 URLs")
        return None
    dldir = tempfile.mkdtemp(prefix="reeldl_")
    try:
        local = []
        for i, u in enumerate(urls):
            dst = os.path.join(dldir, f"src_{i:04d}.jpg")
            if _download(u, dst):
                local.append(dst)
        if len(local) < 2:
            logger.warning("Reel : téléchargements insuffisants (%d)", len(local))
            return None
        out_path = out_path or tempfile.mktemp(prefix="reel_", suffix=".mp4")
        return build_reel_from_paths(local, out_path, sec)
    except Exception as e:
        logger.exception("Reel : build échoué : %s", e)
        return None
    finally:
        shutil.rmtree(dldir, ignore_errors=True)  # photos sources téléchargées
