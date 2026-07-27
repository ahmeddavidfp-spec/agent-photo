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
import random
import shutil
import subprocess
import tempfile
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from http_client import safe_get

logger = logging.getLogger(__name__)

# Baseline 720x1280 : sert de RÉFÉRENCE pour l'échelle de la signature/marges.
# La résolution de SORTIE réelle est passée par `width` (720 léger, 1080 HD) —
# tout le rendu se met à l'échelle via s = w/720.
W, H = 720, 1280           # baseline 9:16 (pour scaling — pas la sortie forcée)
FPS = 30
SEC_PER_PHOTO = 2.5        # durée d'affichage par photo
FADE = 0.4                 # fondu entrée/sortie (s)
XFADE = 0.5                # durée du fondu enchaîné entre photos (transitions)
_MARGIN = 40               # marge (baseline 720) autour de la photo
ZOOM = 0.08                # 8 % de zoom "Ken Burns" par photo
_MOTION_SRC = 1.35         # frames sources = 1.35× la sortie (marge de zoom)

# Police embarquée (Archivo, SIL OFL) pour le bandeau "STREET <VILLE>".
_FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "Archivo.ttf")


def _label_font(size: int, weight: str = "SemiBold") -> "ImageFont.FreeTypeFont":
    f = ImageFont.truetype(_FONT_PATH, size)
    try:
        f.set_variation_by_name(weight)  # poids stable (Archivo est variable)
    except Exception:
        pass  # défaut = SemiBold de toute façon
    return f


def _tracked(d: "ImageDraw.ImageDraw", w: int, cy: float, text: str,
             font: "ImageFont.FreeTypeFont", fill: tuple, track: float) -> float:
    """Dessine `text` centré sur une largeur `w` à `cy`. Retourne la largeur."""
    widths = [d.textlength(c, font=font) for c in text]
    total = sum(widths) + track * (len(text) - 1)
    asc, desc = font.getmetrics()
    x, y = w / 2 - total / 2, cy - (asc + desc) / 2
    for c, cw in zip(text, widths):
        d.text((x, y), c, font=font, fill=fill)
        x += cw + track
    return total


def _signature_overlay(label: str, tagline: Optional[str], w: int, h: int) -> Image.Image:
    """Calque signature RGBA w×h (accroche + bandeau ville + filets), mis à
    l'échelle depuis la baseline 720. Incrusté FIXE (ne zoome jamais)."""
    s = w / 720.0
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    band = round(290 * s)
    for i in range(band):
        d.line([(0, h - band + i), (w, h - band + i)], fill=(0, 0, 0, int(150 * i / band)))

    if tagline:
        tag = tagline.upper()
        size, track = round(21 * s), 4 * s
        while size > round(14 * s):
            tfont = _label_font(size, "Regular")
            tw = sum(d.textlength(c, font=tfont) for c in tag) + track * (len(tag) - 1)
            if tw <= w - round(100 * s):
                break
            size -= max(1, round(s))
        _tracked(d, w, h - round(162 * s), tag, tfont, (255, 255, 255, 225), track)

    size, track = round(46 * s), 5 * s
    while size > round(26 * s):
        font = _label_font(size)
        total = sum(d.textlength(c, font=font) for c in label) + track * (len(label) - 1)
        if total <= w - round(80 * s):
            break
        size -= max(1, round(2 * s))
    cy = h - round(92 * s)
    total = _tracked(d, w, cy, label, font, (255, 255, 255, 255), track)
    half = total / 2
    lw = max(2, round(2 * s))
    for dy in (-round(38 * s), round(38 * s)):
        d.line([(w / 2 - half, cy + dy), (w / 2 + half, cy + dy)], fill=(255, 255, 255, 235), width=lw)
    return layer


def _download(url: str, dest: str) -> bool:
    try:
        # Squarespace : 2000w → assez pour le 1080p HD (+ marge de zoom motion)
        clean = url.split("?")[0] + "?format=2000w"
        r = safe_get(clean, timeout=25)
        r.raise_for_status()
        with open(dest, "wb") as f:
            f.write(r.content)
        return True
    except Exception as e:
        logger.warning("Reel : download KO (%s) : %s", url[:60], e)
        return False


def _normalize(src_path: str, dst_path: str,
               overlay: Optional[Image.Image] = None,
               w: int = W, h: int = H) -> None:
    """Ajuste une photo en w×h sur un fond flou d'elle-même (faible mémoire).

    `overlay` : calque signature RGBA (720x1280) composité par-dessus — utilisé
    par le mode statique. Le mode motion rend les frames en ZWxZH SANS overlay
    (la signature est incrustée fixe par ffmpeg après le zoom).

    Optimisations mémoire (instance Render 512 Mo) :
    - `draft` : décodage JPEG à résolution réduite (libjpeg), bien moins de RAM.
    - fond flou calculé en basse résolution (1/4) puis agrandi : le flou masque
      l'upscale, et l'opération de flou coûte ~1/16 de la mémoire.
    """
    img = Image.open(src_path)
    img.draft("RGB", (w, h * 2))   # décodage réduit AVANT chargement des pixels
    img = img.convert("RGB")

    # Fond flou : calculé en petit (w/4 x h/4) puis agrandi
    sw, sh = w // 4, h // 4
    bg = img.copy()
    scale = max(sw / bg.width, sh / bg.height)
    bg = bg.resize((max(1, round(bg.width * scale)), max(1, round(bg.height * scale))),
                   Image.BILINEAR)
    left = (bg.width - sw) // 2
    top = (bg.height - sh) // 2
    bg = bg.crop((left, top, left + sw, top + sh))
    bg = bg.filter(ImageFilter.GaussianBlur(10))
    bg = ImageEnhance.Brightness(bg).enhance(0.5)
    bg = bg.resize((w, h), Image.BILINEAR)  # agrandi (le flou masque l'upscale)

    # Premier plan : ajuster DANS le cadre (sans crop), marge proportionnelle
    m = round(_MARGIN * w / W)
    img.thumbnail((w - 2 * m, h - 2 * m), Image.LANCZOS)
    bg.paste(img, ((w - img.width) // 2, (h - img.height) // 2))
    if overlay is not None:
        base = Image.alpha_composite(bg.convert("RGBA"), overlay)
        out = base.convert("RGB")
        out.save(dst_path, "JPEG", quality=88)
        out.close()
        base.close()
    else:
        bg.save(dst_path, "JPEG", quality=88)
    img.close()
    bg.close()


_FFMPEG_PATH: Optional[str] = None
_XFADE_OK: Optional[bool] = None


def _ffmpeg_exe() -> str:
    """Chemin ffmpeg. Préfère un build COMPLET (static-ffmpeg, a le filtre
    xfade pour les transitions) ; repli sur imageio-ffmpeg (embarqué, fiable,
    mais minimal → pas de xfade). Résultat mis en cache."""
    global _FFMPEG_PATH
    if _FFMPEG_PATH:
        return _FFMPEG_PATH
    try:
        from static_ffmpeg import run as _sf
        _FFMPEG_PATH = _sf.get_or_fetch_platform_executables_else_raise()[0]
        logger.info("ffmpeg : build complet (static-ffmpeg)")
        return _FFMPEG_PATH
    except Exception as e:
        logger.warning("static-ffmpeg indisponible (%s) → imageio-ffmpeg", e)
    import imageio_ffmpeg
    _FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
    return _FFMPEG_PATH


def _supports_xfade() -> bool:
    """Le ffmpeg courant a-t-il le filtre xfade ? (détecté une fois, caché)."""
    global _XFADE_OK
    if _XFADE_OK is None:
        try:
            out = subprocess.run([_ffmpeg_exe(), "-hide_banner", "-filters"],
                                 capture_output=True, text=True, timeout=20).stdout
            _XFADE_OK = " xfade " in out
        except Exception as e:
            logger.warning("Détection xfade KO : %s", e)
            _XFADE_OK = False
        logger.info("ffmpeg : xfade %s", "disponible ✅" if _XFADE_OK else "absent (coupures)")
    return _XFADE_OK


# =========================================================================
# SÉLECTION HOMOGÈNE : 4 photos COULEUR ou 5 photos NOIR & BLANC
# =========================================================================
# Ne jamais mélanger couleur et N&B dans un même Reel (unité visuelle).
# Classification par saturation moyenne sur une miniature (300w → ~96px),
# coût mémoire/réseau négligeable.

_BW_SAT_THRESHOLD = 0.09   # saturation moyenne < 9% → photo considérée N&B


def _mean_saturation(path: str) -> float:
    """Saturation moyenne (0..1) d'une image, calculée sur une miniature."""
    img = Image.open(path)
    img.draft("RGB", (240, 240))
    img = img.convert("RGB")
    img.thumbnail((96, 96))
    hsv = img.convert("HSV")
    hist = hsv.histogram()[256:512]  # canal S
    total = sum(hist) or 1
    mean = sum(i * c for i, c in enumerate(hist)) / total / 255.0
    img.close()
    hsv.close()
    return mean


def pick_reel_photos(site_url: str, galerie: str, n_color: int = 4,
                     n_bw: int = 5, sample: int = 12) -> Tuple[List[str], str]:
    """Choisit des photos HOMOGÈNES pour un Reel : n_bw N&B ou n_color couleur.

    Échantillonne `sample` photos de la galerie, les classe par saturation,
    et prend le groupe majoritaire (égalité → N&B, l'ADN du compte).
    Retourne (urls, "bw"|"color"|"mixed"|"none").
    """
    from gallery import fetch_gallery_photos  # import local (pas de cycle)
    allp = fetch_gallery_photos(site_url, galerie)
    if len(allp) < 2:
        return [], "none"
    cands = allp[:]
    random.shuffle(cands)
    cands = cands[:sample]

    tmpd = tempfile.mkdtemp(prefix="reelclass_")
    bw: List[str] = []
    color: List[str] = []
    try:
        for i, u in enumerate(cands):
            p = os.path.join(tmpd, f"c{i}.jpg")
            try:
                r = safe_get(u.split("?")[0] + "?format=300w", timeout=15)
                r.raise_for_status()
                with open(p, "wb") as f:
                    f.write(r.content)
                (bw if _mean_saturation(p) < _BW_SAT_THRESHOLD else color).append(u)
            except Exception as e:
                logger.warning("Classification photo KO (%s) : %s", u[:60], e)
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)

    logger.info("Reel %s : %d N&B / %d couleur sur %d échantillonnées",
                galerie, len(bw), len(color), len(cands))
    if len(bw) >= 2 and (len(bw) >= len(color) or len(color) < 2):
        return bw[:n_bw], "bw"
    if len(color) >= 2:
        return color[:n_color], "color"
    mixed = (bw + color)[:n_color]
    return mixed, ("mixed" if len(mixed) >= 2 else "none")


def _assemble(frame_paths: list[str], out_path: str, sec: float = SEC_PER_PHOTO) -> None:
    """Assemble des frames normalisées (toutes 1080x1920) en un MP4 muet."""
    workdir = os.path.dirname(frame_paths[0])
    # Renomme en séquence pour le demuxer image2 (frame_%04d.jpg)
    for i, p in enumerate(frame_paths):
        seq = os.path.join(workdir, f"seq_{i:04d}.jpg")
        if p != seq:
            os.replace(p, seq)
    total = len(frame_paths) * sec
    # Pas de fondu d'entrée (la 1re frame = miniature/couverture propre)
    vf = (
        f"fps={FPS},format=yuv420p,"
        f"fade=t=out:st={total - FADE:.2f}:d={FADE}"
    )
    cmd = [
        _ffmpeg_exe(), "-y",
        "-framerate", f"1/{sec}",
        "-i", os.path.join(workdir, "seq_%04d.jpg"),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-threads", "1",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        out_path,
    ]
    logger.info("Reel : ffmpeg assemble %d frames → %s", len(frame_paths), out_path)
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {res.stderr[-400:]}")


def _assemble_motion(frame_paths: list[str], overlay_png: str, out_path: str,
                     sec: float, w: int, h: int, transitions: bool = False) -> None:
    """Zoom lent "Ken Burns" (alterné) + signature FIXE, sortie w×h.

    `transitions` : fondu enchaîné (xfade) entre les photos plutôt que coupures.
    Frames sources plus grandes que w×h (marge de zoom) ; zoompan sort en w×h."""
    d = max(2, round(sec * FPS))
    n = len(frame_paths)
    cmd = [_ffmpeg_exe(), "-y"]
    for f in frame_paths:
        cmd += ["-i", f]
    cmd += ["-i", overlay_png]
    parts = []
    for k in range(n):
        z = f"1+{ZOOM}*on/{d - 1}" if k % 2 == 0 else f"{1 + ZOOM}-{ZOOM}*on/{d - 1}"
        parts.append(
            f"[{k}:v]zoompan=z='{z}':d={d}"
            f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":s={w}x{h}:fps={FPS},format=yuv420p[v{k}]"
        )

    if transitions and n >= 2 and _supports_xfade():
        # Chaîne de xfade : offset_i = i*(sec - XFADE)
        prev = "[v0]"
        for i in range(1, n):
            tag = "[cat]" if i == n - 1 else f"[x{i}]"
            off = i * (sec - XFADE)
            parts.append(f"{prev}[v{i}]xfade=transition=fade:duration={XFADE}"
                         f":offset={off:.3f}{tag}")
            prev = tag
        total = n * sec - (n - 1) * XFADE
    else:
        if transitions and n >= 2:
            logger.warning("Transitions demandées mais xfade indisponible → coupures franches")
        concat_in = "".join(f"[v{k}]" for k in range(n))
        parts.append(f"{concat_in}concat=n={n}:v=1:a=0[cat]")
        total = n * sec

    # Signature fixe par-dessus + fondu de sortie (pas de fondu d'entrée).
    parts.append(
        f"[cat][{n}:v]overlay=0:0,"
        f"fade=t=out:st={total - FADE:.2f}:d={FADE},format=yuv420p[vout]"
    )
    cmd += ["-filter_complex", ";".join(parts), "-map", "[vout]",
            "-c:v", "libx264", "-preset", "veryfast", "-threads", "1",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path]
    logger.info("Reel : ffmpeg motion %d segments (%dx%d, transitions=%s) → %s",
                n, w, h, transitions, out_path)
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg motion failed: {res.stderr[-400:]}")


def build_reel_from_paths(image_paths: list[str], out_path: str,
                          sec: float = SEC_PER_PHOTO,
                          label: Optional[str] = None,
                          tagline: Optional[str] = None,
                          motion: bool = False, transitions: bool = False,
                          width: int = 720) -> str:
    """Monte un Reel à partir de fichiers image locaux. Retourne out_path."""
    if len(image_paths) < 2:
        raise ValueError("Au moins 2 photos requises pour un Reel")
    w = max(360, int(width)) & ~1          # pair (libx264)
    h = (round(w * 16 / 9)) & ~1
    workdir = tempfile.mkdtemp(prefix="reelwork_")
    try:
        overlay = _signature_overlay(label, tagline, w, h) if label else None
        frames = []
        if motion:
            # Frames sources plus grandes (marge de zoom), SANS signature
            sw = (round(w * _MOTION_SRC)) & ~1
            sh = (round(h * _MOTION_SRC)) & ~1
            for i, src in enumerate(image_paths):
                dst = os.path.join(workdir, f"frame_{i:04d}.jpg")
                _normalize(src, dst, overlay=None, w=sw, h=sh)
                frames.append(dst)
            ov_png = os.path.join(workdir, "signature.png")
            (overlay or Image.new("RGBA", (w, h), (0, 0, 0, 0))).save(ov_png)
            _assemble_motion(frames, ov_png, out_path, sec, w, h, transitions)
        else:
            for i, src in enumerate(image_paths):
                dst = os.path.join(workdir, f"frame_{i:04d}.jpg")
                _normalize(src, dst, overlay=overlay, w=w, h=h)
                frames.append(dst)
            _assemble(frames, out_path, sec)
        if overlay is not None:
            overlay.close()
        return out_path
    finally:
        shutil.rmtree(workdir, ignore_errors=True)  # frames temporaires


def build_reel(image_urls: List[str], out_path: Optional[str] = None,
               sec: float = SEC_PER_PHOTO, label: Optional[str] = None,
               tagline: Optional[str] = None, motion: bool = False,
               transitions: bool = False, width: int = 720) -> Optional[str]:
    """Télécharge les photos et monte un Reel muet 9:16. Retourne le chemin MP4
    ou None si échec.

    `label`/`tagline` : signature incrustée. `motion` : zoom Ken Burns.
    `transitions` : fondu enchaîné entre photos. `width` : 720 ou 1080.
    """
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
        return build_reel_from_paths(local, out_path, sec, label, tagline,
                                     motion, transitions, width)
    except Exception as e:
        logger.exception("Reel : build échoué : %s", e)
        return None
    finally:
        shutil.rmtree(dldir, ignore_errors=True)  # photos sources téléchargées
