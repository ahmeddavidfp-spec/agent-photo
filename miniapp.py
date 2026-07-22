"""Telegram Mini App — Module 1 : sélecteur de photos pour composer un Reel.

Servie par le Flask existant (aucune nouvelle infra) :
- GET  /app/picker?galerie=X : la page (grille de photos, plein écran Telegram)
- GET  /api/photos?galerie=X : liste JSON des photos (auth initData requise)
- POST /api/reel            : lance le montage avec les photos CHOISIES

SÉCURITÉ : chaque appel API exige le header X-Telegram-Init-Data, signé par
Telegram (HMAC-SHA256 avec le token du bot). On vérifie la signature, la
fraîcheur (6h max) et que l'utilisateur est bien ALLOWED_CHAT_ID. La page
HTML elle-même est publique (elle ne contient aucune donnée).
"""
import hashlib
import hmac
import json
import logging
import threading
import time
from urllib.parse import parse_qsl

from flask import abort, jsonify, request

from settings import ALLOWED_CHAT_ID, TELEGRAM_TOKEN, load_yaml_config

logger = logging.getLogger(__name__)

_MAX_AGE_S = 6 * 3600  # initData accepté 6 h après ouverture de l'app


def _validate_init_data(init_data: str):
    """Valide la signature Telegram de initData. Retourne le user dict ou None.

    Schéma officiel : secret = HMAC_SHA256(key="WebAppData", msg=bot_token) ;
    hash attendu = HMAC_SHA256(secret, data_check_string) où data_check_string
    = lignes "clé=valeur" triées (sans le champ hash), jointes par \\n.
    """
    if not init_data or not TELEGRAM_TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        their_hash = pairs.pop("hash", "")
        if not their_hash:
            return None
        check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", TELEGRAM_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, their_hash):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > _MAX_AGE_S:
            return None
        user = json.loads(pairs.get("user", "{}"))
        # Seul le chat autorisé peut piloter le bot
        if ALLOWED_CHAT_ID and str(user.get("id", "")) != str(ALLOWED_CHAT_ID):
            logger.warning("MiniApp : user %s refusé (≠ ALLOWED_CHAT_ID)", user.get("id"))
            return None
        return user
    except Exception as e:
        logger.warning("MiniApp : initData invalide (%s)", e)
        return None


def _require_user():
    user = _validate_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    if not user:
        abort(403)
    return user


# =========================================================================
# PAGE HTML (sélecteur) — autonome, thème Telegram automatique
# =========================================================================

_PICKER_HTML = """<!doctype html>
<html lang="fr"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>Composer un Reel</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body {
    margin: 0; padding: 12px 10px 90px;
    background: var(--tg-theme-bg-color, #111);
    color: var(--tg-theme-text-color, #eee);
    font: 15px/1.4 -apple-system, system-ui, sans-serif;
  }
  h1 { font-size: 17px; margin: 2px 4px 2px; }
  .sub { color: var(--tg-theme-hint-color, #999); font-size: 13px; margin: 0 4px 12px; }
  .grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; }
  .ph { position: relative; aspect-ratio: 1; border-radius: 10px; overflow: hidden;
        background: var(--tg-theme-secondary-bg-color, #222); }
  .ph img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .ph .num {
    position: absolute; top: 6px; right: 6px; width: 24px; height: 24px;
    border-radius: 50%; background: var(--tg-theme-button-color, #2ea6ff);
    color: var(--tg-theme-button-text-color, #fff); font-weight: 700; font-size: 13px;
    display: none; align-items: center; justify-content: center;
  }
  .ph.sel { outline: 3px solid var(--tg-theme-button-color, #2ea6ff); outline-offset: -3px; }
  .ph.sel .num { display: flex; }
  .ph .pub {
    position: absolute; left: 6px; bottom: 6px; font-size: 10px; padding: 2px 6px;
    border-radius: 999px; background: rgba(0,0,0,.65); color: #fff;
  }
  .state { text-align: center; color: var(--tg-theme-hint-color, #999); padding: 40px 10px; }
</style></head><body>
<h1 id="title">Composer un Reel</h1>
<p class="sub">Tape 2 à 6 photos — l'ordre de sélection = l'ordre du Reel (la 1ʳᵉ = couverture).</p>
<div id="state" class="state">Chargement des photos…</div>
<div id="grid" class="grid" hidden></div>
<script>
  const tg = window.Telegram.WebApp;
  tg.ready(); tg.expand();
  const qs = new URLSearchParams(location.search);
  const galerie = qs.get("galerie") || "";
  const MAXSEL = 6, MINSEL = 2;
  let selected = [];   // urls dans l'ordre de sélection

  document.getElementById("title").textContent = "Reel — " + (qs.get("nom") || galerie);

  function refreshButton() {
    if (selected.length >= MINSEL) {
      tg.MainButton.setText("Monter le Reel (" + selected.length + " photos)");
      tg.MainButton.show(); tg.MainButton.enable();
    } else {
      tg.MainButton.hide();
    }
  }

  function renderNums() {
    document.querySelectorAll(".ph").forEach(el => {
      const i = selected.indexOf(el.dataset.url);
      el.classList.toggle("sel", i >= 0);
      el.querySelector(".num").textContent = i >= 0 ? (i + 1) : "";
    });
  }

  fetch("/api/photos?galerie=" + encodeURIComponent(galerie), {
    headers: { "X-Telegram-Init-Data": tg.initData }
  }).then(r => {
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  }).then(data => {
    const grid = document.getElementById("grid");
    data.photos.forEach(p => {
      const d = document.createElement("div");
      d.className = "ph"; d.dataset.url = p.url;
      d.innerHTML = '<img loading="lazy" src="' + p.thumb + '">' +
                    '<span class="num"></span>' +
                    (p.sent ? '<span class="pub">✓ publiée</span>' : "");
      d.onclick = () => {
        const i = selected.indexOf(p.url);
        if (i >= 0) selected.splice(i, 1);
        else if (selected.length < MAXSEL) selected.push(p.url);
        else { tg.HapticFeedback && tg.HapticFeedback.notificationOccurred("warning"); return; }
        tg.HapticFeedback && tg.HapticFeedback.selectionChanged();
        renderNums(); refreshButton();
      };
      grid.appendChild(d);
    });
    document.getElementById("state").hidden = true;
    grid.hidden = false;
    if (!data.photos.length)
      document.getElementById("state").textContent = "Aucune photo dans cette galerie.";
  }).catch(e => {
    document.getElementById("state").textContent = "Erreur de chargement (" + e.message + "). Réouvre depuis le bot.";
  });

  tg.MainButton.onClick(() => {
    tg.MainButton.showProgress();
    fetch("/api/reel", {
      method: "POST",
      headers: { "Content-Type": "application/json",
                 "X-Telegram-Init-Data": tg.initData },
      body: JSON.stringify({ galerie: galerie, urls: selected })
    }).then(r => {
      if (!r.ok) throw new Error("HTTP " + r.status);
      tg.HapticFeedback && tg.HapticFeedback.notificationOccurred("success");
      tg.close();  // le MP4 + la description arrivent dans le chat
    }).catch(e => {
      tg.MainButton.hideProgress();
      alert("Échec du lancement : " + e.message);
    });
  });
</script></body></html>"""


# =========================================================================
# ENREGISTREMENT DES ROUTES
# =========================================================================

def register_miniapp(app, launch_reel) -> None:
    """Branche les routes de la Mini App sur le Flask existant.

    `launch_reel(chat_id, galerie, urls)` : callback fourni par app.py qui
    lance le montage en arrière-plan (avec dédup inflight).
    """

    @app.route("/app/picker")
    def miniapp_picker():
        return _PICKER_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}

    @app.route("/api/photos")
    def miniapp_photos():
        _require_user()
        from db import already_sent_urls
        from gallery import _canonical, fetch_gallery_photos
        galerie = request.args.get("galerie", "").strip()
        config = load_yaml_config()
        if galerie not in (config.get("galeries") or []):
            abort(404)
        photos = fetch_gallery_photos(config["site_url"], galerie)
        sent = {_canonical(u) for u in already_sent_urls()}
        return jsonify({"photos": [
            {
                "url": u,
                "thumb": u.split("?")[0] + "?format=300w",
                "sent": _canonical(u) in sent,
            } for u in photos
        ]})

    @app.route("/api/reel", methods=["POST"])
    def miniapp_reel():
        user = _require_user()
        from gallery import fetch_gallery_photos
        data = request.get_json(force=True, silent=True) or {}
        galerie = str(data.get("galerie", "")).strip()
        urls = [u for u in (data.get("urls") or []) if isinstance(u, str)][:6]
        config = load_yaml_config()
        if galerie not in (config.get("galeries") or []):
            abort(404)
        if len(urls) < 2:
            abort(400)
        # Anti-SSRF : chaque URL doit appartenir à la galerie (jamais d'URL arbitraire)
        legit = set(fetch_gallery_photos(config["site_url"], galerie))
        if any(u not in legit for u in urls):
            logger.warning("MiniApp : URL hors galerie refusée (user=%s)", user.get("id"))
            abort(400)
        threading.Thread(
            target=launch_reel, args=(int(user["id"]), galerie, urls),
            daemon=True, name=f"miniapp-reel-{galerie}",
        ).start()
        return jsonify({"ok": True})

    logger.info("Mini App enregistrée (/app/picker, /api/photos, /api/reel)")
