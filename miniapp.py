"""Telegram Mini App — « Studio » : toute l'app dans Telegram.

Onglets : Galeries (photos → publier/programmer/Reel) · Programmés · Stats · État.
Servie par le Flask existant, le bot classique reste intact à côté.

Routes :
- GET  /app                : le Studio (page unique à onglets)
- GET  /app/picker         : redirection compat → /app?galerie=X&mode=reel
- GET  /app/stats          : dashboard HTML (auth initData, chargé en iframe)
- API  (toutes auth initData) :
    GET  /api/galleries    : galeries + compteurs publiées/total
    GET  /api/photos       : photos d'une galerie (+ badge publiée)
    POST /api/caption      : génère la légende IA pour une photo (sync)
    POST /api/publish      : publie (IG/TH/les deux, carrousel si plusieurs)
    POST /api/schedule     : programme à une date/heure locale
    GET  /api/scheduled    : posts programmés du user
    POST /api/scheduled/cancel : annule un post programmé
    POST /api/reel         : monte un Reel avec les photos choisies
    GET  /api/status       : jours tokens + auto-pub quotidien
    POST /api/daily        : active/désactive l'auto-pub d'une galerie
    POST /api/renew        : renouvelle le token IG ou TH

SÉCURITÉ : header X-Telegram-Init-Data signé par Telegram (HMAC bot token),
user == ALLOWED_CHAT_ID, fraîcheur 6 h, URLs photos ∈ galerie (anti-SSRF).
"""
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from urllib.parse import parse_qsl

from flask import abort, jsonify, redirect, request

from settings import ALLOWED_CHAT_ID, TELEGRAM_TOKEN, load_yaml_config

# Jeton personnel pour ouvrir le Studio HORS Telegram (PWA écran d'accueil).
# Défini via la variable d'env STUDIO_TOKEN. Vide → accès PWA désactivé
# (seul le Mini App Telegram fonctionne, comportement historique).
STUDIO_TOKEN = os.environ.get("STUDIO_TOKEN", "").strip()

logger = logging.getLogger(__name__)

_MAX_AGE_S = 6 * 3600  # initData accepté 6 h après ouverture de l'app


# =========================================================================
# AUTH
# =========================================================================

def _validate_init_data(init_data: str):
    """Valide la signature Telegram de initData. Retourne le user dict ou None."""
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
        if ALLOWED_CHAT_ID and str(user.get("id", "")) != str(ALLOWED_CHAT_ID):
            logger.warning("MiniApp : user %s refusé (≠ ALLOWED_CHAT_ID)", user.get("id"))
            return None
        return user
    except Exception as e:
        logger.warning("MiniApp : initData invalide (%s)", e)
        return None


def _validate_studio_token(token: str):
    """Auth PWA hors Telegram : jeton personnel == STUDIO_TOKEN → le propriétaire."""
    if STUDIO_TOKEN and token and hmac.compare_digest(token, STUDIO_TOKEN):
        return {"id": ALLOWED_CHAT_ID}
    return None


def _require_user():
    user = _validate_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    if not user:
        user = _validate_studio_token(request.headers.get("X-Studio-Token", ""))
    if not user:
        abort(403)
    return user


def _display_name(galerie: str, config: dict) -> str:
    names = config.get("gallery_names") or {}
    return names.get(galerie) or galerie.replace("-", " ").replace("_", " ").title()


def _check_gallery(galerie: str, config: dict) -> None:
    if galerie not in (config.get("galeries") or []):
        abort(404)


# =========================================================================
# COMPTEURS DE GALERIES (publiées/total) — calculés EN TÂCHE DE FOND
# -------------------------------------------------------------------------
# Le scraping des ~20 galeries ne doit JAMAIS se faire dans une requête (ça
# saturait les threads + le verrou DB → gel de l'app). On le fait dans un
# thread démon single-flight, sans tenir aucun verrou DB pendant le scraping,
# et l'endpoint lit juste le cache.
# =========================================================================
_COUNTS = {"data": {}, "ts": 0.0}
_COUNTS_TTL = 1800.0            # recalcul au plus toutes les 30 min
_counts_lock = threading.Lock()
_counts_running = False
_counts_loaded = False         # a-t-on déjà tenté de charger les compteurs persistés ?


def _load_persisted_counts() -> None:
    """Charge les derniers compteurs SAUVÉS EN BASE → `counts=1` répond tout de
    suite après un redémarrage (fini le 'warming' à vide qui donne l'impression
    que le Studio est bloqué). Un rafraîchissement de fond met ensuite à jour."""
    global _counts_loaded
    if _counts_loaded:
        return
    _counts_loaded = True
    if _COUNTS["data"]:
        return
    try:
        import json as _json
        from db import get_job_last_run
        raw = get_job_last_run("counts_cache")
        if raw:
            d = _json.loads(raw)
            _COUNTS["data"] = {k: tuple(v) for k, v in d.items() if v}
            # ts reste à 0 → un recalcul de fond se déclenchera pour rafraîchir.
            logger.info("Compteurs persistés chargés (%d galeries).", len(_COUNTS["data"]))
    except Exception as e:
        logger.debug("counts_cache load KO : %s", e)

# Un SEUL parsing lourd (BeautifulSoup) à la fois en arrière-plan. Sinon deux
# threads (scan + compteurs) saturent le GIL et affament le thread qui tient le
# verrou DB → attentes de 30 s. Ce verrou sérialise le scraping des tâches de fond.
_HEAVY_LOCK = threading.Lock()


def _refresh_counts() -> None:
    """Recalcule les compteurs (scraping) en arrière-plan. Single-flight.
    AUCUN verrou DB tenu pendant le scraping (sent lu une seule fois avant)."""
    global _counts_running
    with _counts_lock:
        if _counts_running:
            return
        _counts_running = True
    try:
        from db import already_sent_urls
        from gallery import counts_for_gallery
        config = load_yaml_config()
        galeries = config.get("galeries") or []
        sent = already_sent_urls()           # 1 seul accès DB, hors scraping
        data = {}
        with _HEAVY_LOCK:                    # un seul parsing lourd à la fois
            for g in galeries:               # séquentiel = doux pour le GIL
                try:
                    done, total = counts_for_gallery(config["site_url"], g, sent)
                    if total:
                        data[g] = (done, total)
                        # Publie AU FUR ET À MESURE : dès la 1re galerie comptée,
                        # `warming` passe à false et les badges s'affichent
                        # progressivement — au lieu de rester bloqués tant que les
                        # 22 galeries ne sont pas TOUTES scrapées (plusieurs min si
                        # le relais est lent). Le Studio n'apparaît plus figé.
                        _COUNTS["data"] = dict(data)
                        _COUNTS["ts"] = time.time()
                except Exception as e:
                    logger.debug("compteur %s KO : %s", g, e)
        _COUNTS["data"] = data
        _COUNTS["ts"] = time.time()
        # Persiste en base → survit aux redémarrages (chargé instantanément au boot).
        try:
            import json as _json
            from db import set_job_last_run
            set_job_last_run("counts_cache", _json.dumps({k: list(v) for k, v in data.items()}))
        except Exception:
            pass
        logger.info("Compteurs galeries recalculés (%d).", len(data))
    except Exception as e:
        logger.warning("Refresh compteurs KO : %s", e)
    finally:
        _counts_running = False


def _maybe_refresh_counts() -> None:
    """Déclenche un recalcul en tâche de fond si le cache est périmé (non bloquant)."""
    if not _counts_running and (time.time() - _COUNTS["ts"]) > _COUNTS_TTL:
        threading.Thread(target=_refresh_counts, daemon=True,
                         name="counts-refresh").start()


# --- Scan de nouvelles galeries EN TÂCHE DE FOND (jamais dans la requête) -----
_scan_state = {"running": False, "unreachable": False}
_scan_lock = threading.Lock()


def _run_scan() -> None:
    """Scanne le sitemap (scraping) en arrière-plan. Single-flight."""
    with _scan_lock:
        if _scan_state["running"]:
            return
        _scan_state["running"] = True
    try:
        from discover import SiteUnreachable, scan_new_galleries
        # _HEAVY_LOCK évite un scan ET un calcul de compteurs LOURDS en même temps.
        # Mais on ne l'ATTEND PAS indéfiniment : si les compteurs tournent (lents),
        # le scan restait bloqué plusieurs minutes ('scanning' sans fin, bouton en
        # erreur). On tente ~3 s ; sinon on scanne quand même (quelques pages =
        # charge modérée), pour que le scan se termine TOUJOURS.
        got = _HEAVY_LOCK.acquire(timeout=3)
        try:
            scan_new_galleries()          # enregistre les nouvelles (status='pending')
            _scan_state["unreachable"] = False
        except SiteUnreachable:
            _scan_state["unreachable"] = True
        except Exception as e:
            logger.warning("Scan galeries KO : %s", e)
        finally:
            if got:
                _HEAVY_LOCK.release()
    finally:
        _scan_state["running"] = False


def _maybe_scan() -> None:
    if not _scan_state["running"]:
        threading.Thread(target=_run_scan, daemon=True, name="gallery-scan").start()


# =========================================================================
# PAGE HTML DU STUDIO
# =========================================================================

_STUDIO_HTML = r"""<!doctype html>
<html lang="fr"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no,viewport-fit=cover">
<title>Studio</title>
<meta name="theme-color" content="#111111">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Studio">
<link rel="manifest" href="/studio/manifest.webmanifest">
<link rel="apple-touch-icon" href="/static/pwa/icon-180.png">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body { margin: 0; padding: calc(env(safe-area-inset-top) + 46px) 0 66px;
    background: var(--tg-theme-bg-color, #111);
    color: var(--tg-theme-text-color, #eee);
    font: 15px/1.45 -apple-system, system-ui, sans-serif; }
  /* En-tête fixe de l'app (titre de marque), sous l'encoche iPhone. */
  .appbar { position: fixed; top: 0; left: 0; right: 0; z-index: 30;
    padding: calc(env(safe-area-inset-top) + 11px) 14px 11px;
    background: var(--tg-theme-secondary-bg-color, #1c1c1e);
    border-bottom: 1px solid rgba(128,128,128,.25);
    text-align: center; font-size: 16px; font-weight: 700; letter-spacing: .2px;
    color: var(--tg-theme-text-color, #eee); }
  section { padding: 12px 12px 90px; }
  h1 { font-size: 17px; margin: 4px 2px 10px; display: flex; align-items: center; gap: 8px; }
  .hint { color: var(--tg-theme-hint-color, #999); font-size: 13px; margin: 0 2px 12px; }
  .back { border: 0; background: none; color: var(--tg-theme-link-color, #2ea6ff);
          font-size: 15px; padding: 4px 6px 4px 0; }
  .row { display: flex; align-items: center; justify-content: space-between; gap: 10px;
         padding: 13px 14px; border-radius: 12px; margin-bottom: 8px;
         background: var(--tg-theme-secondary-bg-color, #1c1c1e); }
  .row b { font-weight: 600; }
  .row .cnt { color: var(--tg-theme-hint-color, #999); font-size: 13px; white-space: nowrap; }
  .grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; }
  .ph { position: relative; aspect-ratio: 1; border-radius: 10px; overflow: hidden;
        background: var(--tg-theme-secondary-bg-color, #222); }
  .ph img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .ph .num { position: absolute; top: 6px; right: 6px; width: 24px; height: 24px;
    border-radius: 50%; background: var(--tg-theme-button-color, #2ea6ff);
    color: var(--tg-theme-button-text-color, #fff); font-weight: 700; font-size: 13px;
    display: none; align-items: center; justify-content: center; }
  .ph.sel { outline: 3px solid var(--tg-theme-button-color, #2ea6ff); outline-offset: -3px; }
  .ph.sel .num { display: flex; }
  .ph .pub { position: absolute; left: 6px; bottom: 6px; font-size: 10px; padding: 2px 6px;
             border-radius: 999px; background: rgba(0,0,0,.65); color: #fff; }
  .seg { display: flex; background: var(--tg-theme-secondary-bg-color, #1c1c1e);
         border-radius: 10px; padding: 3px; margin: 0 0 12px; }
  .seg button { flex: 1; border: 0; border-radius: 8px; padding: 8px 4px; font-size: 14px;
    background: none; color: var(--tg-theme-hint-color, #999); }
  .seg button.on { background: var(--tg-theme-button-color, #2ea6ff);
                   color: var(--tg-theme-button-text-color, #fff); }
  .btn { display: block; width: 100%; border: 0; border-radius: 12px; padding: 13px;
    font-size: 15px; font-weight: 600; margin: 8px 0;
    background: var(--tg-theme-button-color, #2ea6ff);
    color: var(--tg-theme-button-text-color, #fff); }
  .btn.sec { background: var(--tg-theme-secondary-bg-color, #1c1c1e);
             color: var(--tg-theme-text-color, #eee); }
  .btn:disabled { opacity: .45; }
  textarea { width: 100%; min-height: 170px; border-radius: 12px; padding: 12px;
    border: 1px solid var(--tg-theme-hint-color, #444);
    background: var(--tg-theme-secondary-bg-color, #1c1c1e);
    color: var(--tg-theme-text-color, #eee); font: 14px/1.5 inherit; }
  input[type="datetime-local"] { width: 100%; border-radius: 12px; padding: 11px;
    border: 1px solid var(--tg-theme-hint-color, #444);
    background: var(--tg-theme-secondary-bg-color, #1c1c1e);
    color: var(--tg-theme-text-color, #eee); font-size: 15px; }
  .cover { width: 92px; height: 92px; border-radius: 10px; object-fit: cover; }
  .state { text-align: center; color: var(--tg-theme-hint-color, #999); padding: 36px 10px; }
  iframe { width: 100%; height: calc(100vh - 90px); border: 0; border-radius: 12px; }
  nav { position: fixed; left: 0; right: 0; bottom: 0; display: flex; z-index: 5;
    background: var(--tg-theme-secondary-bg-color, #1c1c1e);
    border-top: 1px solid rgba(128,128,128,.25);
    padding-bottom: env(safe-area-inset-bottom); }
  nav button { flex: 1; border: 0; background: none; padding: 9px 2px 7px;
    color: var(--tg-theme-hint-color, #999); font-size: 11px; }
  nav button.on { color: var(--tg-theme-button-color, #2ea6ff); }
  nav button .ico { display: block; font-size: 20px; margin-bottom: 1px; }
  .pill { font-size: 12px; padding: 3px 9px; border-radius: 999px;
          background: var(--tg-theme-secondary-bg-color, #1c1c1e);
          color: var(--tg-theme-hint-color, #999); }
  /* Bouton principal de remplacement quand on tourne en PWA (hors Telegram),
     posé juste au-dessus de la barre d'onglets. Masqué dans Telegram. */
  .pwa-mainbtn { position: fixed; left: 0; right: 0; z-index: 20; border: 0;
    bottom: calc(56px + env(safe-area-inset-bottom));
    padding: 15px; font-size: 16px; font-weight: 600;
    background: #2ea6ff; color: #fff; }
  .pwa-mainbtn:disabled { opacity: .55; }
  /* Panneau de saisie du jeton (première ouverture PWA). */
  #tokgate { position: fixed; inset: 0; z-index: 100; display: none;
    background: #111; color: #eee; padding: 40px 24px; text-align: center; }
  #tokgate.on { display: block; }
  #tokgate input { width: 100%; max-width: 420px; margin: 16px auto; display: block;
    padding: 13px; border-radius: 10px; border: 1px solid #333; background: #1c1c1e;
    color: #eee; font-size: 15px; }
  #tokgate button { padding: 13px 22px; border: 0; border-radius: 10px;
    background: #2ea6ff; color: #fff; font-size: 15px; font-weight: 600; }
  /* Toast de confirmation (surtout en PWA, où l'app ne se ferme pas). */
  #toast { position: fixed; left: 50%; z-index: 200; max-width: 88vw; text-align: center;
    bottom: calc(74px + env(safe-area-inset-bottom)); transform: translate(-50%, 20px);
    background: #0f1a2e; color: #fff; padding: 12px 18px; border-radius: 12px;
    font-size: 14px; font-weight: 600; box-shadow: 0 10px 34px rgba(0,0,0,.45);
    opacity: 0; pointer-events: none; transition: .28s ease; }
  #toast.show { opacity: 1; transform: translate(-50%, 0); }
</style></head><body>
<header class="appbar">APP Studio Agent</header>
<button id="pwa-mainbtn" class="pwa-mainbtn" hidden></button>
<div id="tokgate">
  <h1 style="justify-content:center">🔒 Studio</h1>
  <p class="hint">Colle ton code d'accès personnel pour ouvrir le Studio.</p>
  <input id="tokgate-input" type="password" autocomplete="off" placeholder="Code d'accès">
  <button onclick="saveToken()">Ouvrir</button>
</div>

<section id="v-gal">
  <h1>🎨 Studio</h1>
  <p class="hint">Choisis une galerie pour publier ou composer un Reel.</p>
  <div id="gal-state" class="state">Chargement des galeries…</div>
  <div id="gal-list"></div>
</section>

<section id="v-photos" hidden>
  <h1><button class="back" onclick="show('v-gal');Main.hide()">‹</button><span id="ph-title"></span></h1>
  <div class="seg" id="mode-seg">
    <button id="seg-pub" class="on" onclick="setMode('pub')">📤 Publier</button>
    <button id="seg-reel" onclick="setMode('reel')">🎬 Reel</button>
  </div>
  <p class="hint" id="ph-hint"></p>
  <div id="ph-state" class="state">Chargement…</div>
  <div id="ph-grid" class="grid" hidden></div>
</section>

<section id="v-caption" hidden>
  <h1><button class="back" onclick="show('v-photos');refreshMain()">‹</button>Publier <span id="cap-count" class="pill"></span></h1>
  <div style="display:flex;gap:10px;margin-bottom:12px;align-items:center">
    <img id="cap-cover" class="cover">
    <div class="hint" style="margin:0">La 1ʳᵉ photo sélectionnée = couverture.<br>Plusieurs photos = carrousel Instagram.</div>
  </div>
  <button class="btn sec" id="cap-gen" onclick="genCaption()">✨ Générer la légende (IA)</button>
  <textarea id="cap-text" placeholder="La légende apparaîtra ici — tu peux l'éditer."></textarea>
  <div class="seg" style="margin-top:10px">
    <button id="m-both" class="on" onclick="setPubMode('both')">Tous</button>
    <button id="m-ig" onclick="setPubMode('ig')">Insta</button>
    <button id="m-th" onclick="setPubMode('th')">Threads</button>
    <button id="m-fb" onclick="setPubMode('fb')">Facebook</button>
  </div>
  <button class="btn" id="cap-pub" onclick="publishNow()">🚀 Publier maintenant</button>
  <input type="datetime-local" id="cap-when">
  <button class="btn sec" onclick="scheduleIt()">📅 Programmer</button>
</section>

<section id="v-sched" hidden>
  <h1>📅 Posts programmés</h1>
  <div id="sched-state" class="state">Chargement…</div>
  <div id="sched-list"></div>
</section>

<section id="v-stats" hidden>
  <h1>📊 Stats</h1>
  <button class="btn sec" onclick="loadReport()">📬 Rapport de la semaine</button>
  <div id="report-box" hidden style="white-space:pre-wrap;background:var(--tg-theme-secondary-bg-color,#1c1c1e);border-radius:12px;padding:14px;margin:8px 0;font-size:14px"></div>
  <button class="btn sec" onclick="loadPlan()">📅 Plan de la semaine</button>
  <div id="plan-box" hidden style="white-space:pre-wrap;background:var(--tg-theme-secondary-bg-color,#1c1c1e);border-radius:12px;padding:14px;margin:8px 0;font-size:14px"></div>
  <button id="plan-go" class="btn" hidden onclick="schedulePlan()">✅ Tout programmer</button>
  <div id="stats-state" class="state">Chargement…</div>
  <iframe id="stats-frame" hidden></iframe>
</section>

<section id="v-status" hidden>
  <h1>⚙️ État</h1>
  <div id="status-tokens"></div>
  <h1 style="margin-top:18px">🩺 Connexion au site</h1>
  <p class="hint">Vérifie si le serveur arrive à joindre ton site (utile en cas de blocage).</p>
  <button class="btn sec" id="diag-btn" onclick="runDiag()">🩺 Tester la connexion</button>
  <div id="diag-results"></div>
  <h1 style="margin-top:18px">🆕 Détection de galeries</h1>
  <p class="hint">Cherche sur ton site les nouvelles galeries à ajouter automatiquement.</p>
  <button class="btn sec" id="scan-btn" onclick="scanGalleries()">🔍 Scanner les galeries</button>
  <div id="scan-results"></div>
  <h1 style="margin-top:18px">🔄 Auto-pub quotidien</h1>
  <p class="hint">Une seule galerie active à la fois (publiée chaque jour à la meilleure heure).</p>
  <div id="status-daily" class="state">Chargement…</div>
</section>

<nav>
  <button id="t-gal" class="on" onclick="tab('v-gal')"><span class="ico">🖼</span>Galeries</button>
  <button id="t-sched" onclick="tab('v-sched');loadSched()"><span class="ico">📅</span>Programmés</button>
  <button id="t-stats" onclick="tab('v-stats');loadStats()"><span class="ico">📊</span>Stats</button>
  <button id="t-status" onclick="tab('v-status');loadStatus()"><span class="ico">⚙️</span>État</button>
</nav>

<script>
  const $ = id => document.getElementById(id);

  // Telegram réel seulement si initData signé présent ; sinon PWA autonome.
  const RealTG = (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initData)
    ? window.Telegram.WebApp : null;
  const STANDALONE = !RealTG;

  // Jeton d'accès (PWA) : ?token=… → localStorage, puis on nettoie l'URL.
  const tokGet = () => { try { return localStorage.getItem("studio_token") || ""; } catch(e){ return ""; } };
  (function(){ try {
    const u = new URL(location.href), t = u.searchParams.get("token");
    if (t) { localStorage.setItem("studio_token", t);
      u.searchParams.delete("token"); history.replaceState(null, "", u.pathname + u.search); }
  } catch(e){} })();

  let tg, Main;
  if (RealTG) {
    tg = RealTG; tg.ready(); tg.expand(); Main = tg.MainButton;
  } else {
    const b = $("pwa-mainbtn");
    Main = {
      setText(t){ b.textContent = t; }, show(){ b.hidden = false; }, hide(){ b.hidden = true; },
      enable(){ b.disabled = false; }, showProgress(){ b.disabled = true; }, hideProgress(){ b.disabled = false; },
      onClick(cb){ b.onclick = () => cb && cb(); },
    };
    const buzz = () => { try { navigator.vibrate && navigator.vibrate(8); } catch(e){} };
    tg = { initData: "", ready(){}, expand(){}, close(){}, MainButton: Main,
      openLink(url){ window.open(url, "_blank"); },
      // Shim complet de l'API haptique Telegram (sinon toggle() plante en PWA).
      HapticFeedback: { notificationOccurred: buzz, selectionChanged: buzz,
                        impactOccurred: buzz } };
  }

  const H = () => STANDALONE ? { "X-Studio-Token": tokGet() }
                             : { "X-Telegram-Init-Data": tg.initData };
  const HJ = () => STANDALONE ? { "Content-Type": "application/json", "X-Studio-Token": tokGet() }
                              : { "Content-Type": "application/json", "X-Telegram-Init-Data": tg.initData };
  const haptic = k => tg.HapticFeedback && tg.HapticFeedback.notificationOccurred(k);

  function saveToken(){
    const v = ($("tokgate-input").value || "").trim();
    if (v) { try { localStorage.setItem("studio_token", v); } catch(e){} location.reload(); }
  }

  let galerie = "", galNom = "", mode = "pub", pubMode = "both";
  let photos = [], selected = [], alt = "";

  function show(id) {
    ["v-gal","v-photos","v-caption","v-sched","v-stats","v-status"].forEach(v => $(v).hidden = v !== id);
    window.scrollTo(0, 0);
  }
  function tab(id) {
    show(id); Main.hide();
    ["t-gal","t-sched","t-stats","t-status"].forEach(t => $(t).classList.remove("on"));
    ({ "v-gal":"t-gal","v-sched":"t-sched","v-stats":"t-stats","v-status":"t-status" })[id]
      && $({ "v-gal":"t-gal","v-sched":"t-sched","v-stats":"t-stats","v-status":"t-status" }[id]).classList.add("on");
  }
  async function api(url, opts) {
    const r = await fetch(url, opts);
    if (r.status === 403 && STANDALONE) {   // jeton absent/incorrect → porte d'accès
      $("tokgate").classList.add("on");
      throw new Error("Code d'accès requis");
    }
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  }

  // ---- Galeries : liste instantanée, compteurs remplis en arrière-plan ----
  async function loadGalleries() {
    try {
      const d = await api("/api/galleries", { headers: H() });   // instantané
      const list = $("gal-list"); list.innerHTML = "";
      d.galleries.forEach(g => {
        const row = document.createElement("div");
        row.className = "row"; row.id = "gal-" + g.slug;
        row.innerHTML = "<b>" + g.nom + "</b><span class='cnt'></span>";
        row.onclick = () => openGallery(g.slug, g.nom);
        list.appendChild(row);
      });
      $("gal-state").hidden = true;
      loadCounts(0);   // compteurs via cache serveur (calcul en tâche de fond)
    } catch (e) { $("gal-state").textContent = "Erreur : " + e.message; }
  }
  // Compteurs « X/Y publiées » : lit le cache serveur, re-sonde jusqu'à prêt.
  async function loadCounts(tries) {
    try {
      const dc = await api("/api/galleries?counts=1", { headers: H() });
      let missing = 0;
      (dc.galleries || []).forEach(g => {
        const row = $("gal-" + g.slug);
        if (!row) return;
        const cnt = row.querySelector(".cnt");
        if (typeof g.done === "number") cnt.textContent = g.done + "/" + g.total + " publiées";
        else missing++;
      });
      if ((dc.warming || missing) && tries < 8) setTimeout(() => loadCounts(tries + 1), 5000);
    } catch (e) {}
  }

  // ---- Photos d'une galerie ----
  async function openGallery(slug, nom, forceMode) {
    galerie = slug; galNom = nom || slug; selected = [];
    if (forceMode) setMode(forceMode, true);
    $("ph-title").textContent = galNom;
    show("v-photos"); refreshHint(); refreshMain();
    $("ph-state").hidden = false; $("ph-grid").hidden = true;
    try {
      const d = await api("/api/photos?galerie=" + encodeURIComponent(slug), { headers: H() });
      photos = d.photos;
      const grid = $("ph-grid"); grid.innerHTML = "";
      photos.forEach(p => {
        const el = document.createElement("div");
        el.className = "ph"; el.dataset.url = p.url;
        el.innerHTML = '<img loading="lazy" src="' + p.thumb + '"><span class="num"></span>' +
                       (p.sent ? '<span class="pub">✓ publiée</span>' : "");
        el.onclick = () => toggle(p.url);
        grid.appendChild(el);
      });
      $("ph-state").hidden = true; grid.hidden = false;
    } catch (e) { $("ph-state").textContent = "Erreur : " + e.message; }
  }
  function maxSel() { return mode === "reel" ? 6 : 10; }
  function minSel() { return mode === "reel" ? 2 : 1; }
  function toggle(url) {
    const i = selected.indexOf(url);
    if (i >= 0) selected.splice(i, 1);
    else if (selected.length < maxSel()) selected.push(url);
    else { haptic("warning"); return; }
    tg.HapticFeedback && tg.HapticFeedback.selectionChanged();
    renderNums(); refreshMain();
  }
  function renderNums() {
    document.querySelectorAll("#ph-grid .ph").forEach(el => {
      const i = selected.indexOf(el.dataset.url);
      el.classList.toggle("sel", i >= 0);
      el.querySelector(".num").textContent = i >= 0 ? (i + 1) : "";
    });
  }
  function setMode(m, silent) {
    mode = m; selected = [];
    $("seg-pub").classList.toggle("on", m === "pub");
    $("seg-reel").classList.toggle("on", m === "reel");
    if (!silent) { renderNums(); refreshMain(); }
    refreshHint();
  }
  function refreshHint() {
    $("ph-hint").textContent = mode === "reel"
      ? "Tape 2 à 6 photos — l'ordre = l'ordre du Reel, la 1ʳᵉ = couverture."
      : "Tape 1 photo (post simple) ou plusieurs (carrousel). La 1ʳᵉ = couverture.";
  }
  function refreshMain() {
    if ($("v-photos").hidden) { Main.hide(); return; }
    if (selected.length >= minSel()) {
      Main.setText(mode === "reel"
        ? "Monter le Reel (" + selected.length + ")"
        : "Continuer (" + selected.length + " photo" + (selected.length > 1 ? "s" : "") + ")");
      Main.show(); Main.enable();
    } else Main.hide();
  }
  Main.onClick(() => {
    if (!$("v-photos").hidden) {
      if (mode === "reel") launchReel();
      else openCaption();
    }
  });

  // ---- Confirmation d'action (Telegram ferme l'app ; la PWA affiche un toast) ----
  function toast(msg){
    let t = $("toast");
    if (!t) { t = document.createElement("div"); t.id = "toast"; document.body.appendChild(t); }
    t.textContent = msg; t.classList.add("show");
    clearTimeout(window.__toastT);
    window.__toastT = setTimeout(function(){ t.classList.remove("show"); }, 3400);
  }
  function finishAction(msg){
    if (RealTG) { tg.close(); return; }            // Telegram : le message arrive dans le chat
    toast(msg || "✅ C'est fait !");               // PWA : confirmation + retour aux galeries
    try { Main.hideProgress(); } catch(e){}
    if ($("cap-pub")) $("cap-pub").disabled = false;
    selected = [];
    tab("v-gal");
  }

  // ---- Reel ----
  function launchReel() {
    Main.showProgress();
    fetch("/api/reel", { method: "POST", headers: HJ(),
      body: JSON.stringify({ galerie: galerie, urls: selected }) })
      .then(r => { if (!r.ok) throw new Error("HTTP " + r.status);
        haptic("success"); finishAction("🎬 Reel en cours de montage — il arrivera dans Telegram."); })
      .catch(e => { Main.hideProgress(); alert("Échec : " + e.message); });
  }

  // ---- Publier / Programmer ----
  function openCaption() {
    Main.hide();
    $("cap-count").textContent = selected.length > 1 ? "carrousel ×" + selected.length : "post simple";
    $("cap-cover").src = selected[0].split("?")[0] + "?format=300w";
    $("cap-text").value = ""; alt = "";
    const d = new Date(Date.now() + 24 * 3600e3); d.setHours(18, 0, 0, 0);
    $("cap-when").value = d.toISOString().slice(0, 16);
    show("v-caption");
  }
  function setPubMode(m) {
    pubMode = m;
    ["m-both","m-ig","m-th","m-fb"].forEach(x => $(x).classList.remove("on"));
    $({ both:"m-both", ig:"m-ig", th:"m-th", fb:"m-fb" }[m]).classList.add("on");
  }
  async function genCaption() {
    $("cap-gen").disabled = true; $("cap-gen").textContent = "✨ Génération… (~15 s)";
    try {
      const d = await api("/api/caption", { method: "POST", headers: HJ(),
        body: JSON.stringify({ galerie: galerie, url: selected[0] }) });
      $("cap-text").value = d.visible; alt = d.alt; haptic("success");
    } catch (e) { alert("Échec génération : " + e.message); }
    $("cap-gen").disabled = false; $("cap-gen").textContent = "✨ Générer la légende (IA)";
  }
  async function publishNow() {
    if (!$("cap-text").value.trim()) { alert("Génère ou écris une légende d'abord."); return; }
    $("cap-pub").disabled = true;
    try {
      await api("/api/publish", { method: "POST", headers: HJ(),
        body: JSON.stringify({ galerie: galerie, urls: selected, mode: pubMode,
                               visible: $("cap-text").value, alt: alt }) });
      haptic("success"); finishAction("🚀 Publication lancée !");
    } catch (e) { alert("Échec : " + e.message); $("cap-pub").disabled = false; }
  }
  async function scheduleIt() {
    if (!$("cap-text").value.trim()) { alert("Génère ou écris une légende d'abord."); return; }
    try {
      await api("/api/schedule", { method: "POST", headers: HJ(),
        body: JSON.stringify({ galerie: galerie, urls: selected,
                               visible: $("cap-text").value, alt: alt,
                               when: $("cap-when").value }) });
      haptic("success"); finishAction("📅 Post programmé !");
    } catch (e) { alert("Échec : " + e.message); }
  }

  // ---- Programmés ----
  async function loadSched() {
    $("sched-state").hidden = false; $("sched-list").innerHTML = "";
    try {
      const d = await api("/api/scheduled", { headers: H() });
      $("sched-state").hidden = true;
      if (!d.posts.length) { $("sched-state").hidden = false;
        $("sched-state").textContent = "Aucun post programmé."; return; }
      d.posts.forEach(p => {
        const card = document.createElement("div"); card.className = "row";
        card.style.flexDirection = "column"; card.style.alignItems = "stretch";

        // En-tête : vignette + date + type + chevron (cliquable pour déplier)
        const head = document.createElement("div");
        head.style.cssText = "display:flex;align-items:center;gap:10px;cursor:pointer";
        head.innerHTML =
          "<img src='" + p.thumb + "' style='width:44px;height:44px;border-radius:8px;object-fit:cover'>" +
          "<div style='flex:1'><b>" + p.when_local + "</b><br><span class='cnt'>" +
          (p.count > 1 ? "carrousel ×" + p.count : "post simple") + "</span></div>" +
          "<span class='chev' style='opacity:.6;font-size:13px'>Voir ▾</span>";

        // Corps : grille des photos + légende + bouton Annuler (replié par défaut)
        const body = document.createElement("div");
        body.hidden = true; body.style.marginTop = "12px";
        const grid = document.createElement("div");
        grid.style.cssText = "display:grid;grid-template-columns:repeat(3,1fr);gap:6px";
        (p.thumbs || []).forEach(u => {
          const im = document.createElement("img"); im.src = u; im.loading = "lazy";
          im.style.cssText = "width:100%;aspect-ratio:1/1;object-fit:cover;border-radius:8px";
          grid.appendChild(im);
        });
        const cap = document.createElement("div");
        cap.style.cssText = "margin-top:12px;white-space:pre-wrap;font-size:13px;line-height:1.45";
        cap.textContent = p.caption || "(pas de légende)";
        const b = document.createElement("button"); b.className = "back"; b.textContent = "Annuler ce post";
        b.style.marginTop = "12px";
        b.onclick = async () => {
          if (!confirm("Annuler ce post ?")) return;
          await api("/api/scheduled/cancel", { method: "POST", headers: HJ(),
            body: JSON.stringify({ id: p.id }) });
          loadSched();
        };
        body.appendChild(grid); body.appendChild(cap); body.appendChild(b);

        head.onclick = () => {
          body.hidden = !body.hidden;
          head.querySelector(".chev").textContent = body.hidden ? "Voir ▾" : "Masquer ▴";
          if (tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
        };

        card.appendChild(head); card.appendChild(body);
        $("sched-list").appendChild(card);
      });
    } catch (e) { $("sched-state").textContent = "Erreur : " + e.message; }
  }

  // ---- Stats ----
  async function loadStats() {
    if (!$("stats-frame").hidden) return;  // déjà chargé
    try {
      const r = await fetch("/app/stats", { headers: H() });
      if (!r.ok) throw new Error("HTTP " + r.status);
      $("stats-frame").srcdoc = await r.text();
      $("stats-state").hidden = true; $("stats-frame").hidden = false;
    } catch (e) { $("stats-state").textContent = "Erreur : " + e.message; }
  }

  async function loadReport() {
    const box = $("report-box"); box.hidden = false; box.textContent = "Génération…";
    try {
      const d = await api("/api/report", { headers: H() });
      // markdown léger → texte lisible
      box.textContent = d.report.replace(/\*/g, "").replace(/_/g, "");
    } catch (e) { box.textContent = "Erreur : " + e.message; }
  }
  async function loadPlan() {
    const box = $("plan-box"); box.hidden = false; box.textContent = "Calcul du plan…";
    $("plan-go").hidden = true;
    try {
      const d = await api("/api/plan", { headers: H() });
      box.textContent = d.plan.replace(/\*/g, "").replace(/_/g, "");
      $("plan-go").hidden = false;
    } catch (e) { box.textContent = "Erreur : " + e.message; }
  }
  async function schedulePlan() {
    const btn = $("plan-go"); btn.disabled = true; const l = btn.textContent; btn.textContent = "⏳ Programmation…";
    try {
      await api("/api/plan/schedule", { method: "POST", headers: HJ(), body: "{}" });
      haptic("success"); toast("📅 Plan en cours de programmation !");
      btn.textContent = "✅ Programmé";
    } catch (e) { alert("Échec : " + e.message); btn.disabled = false; btn.textContent = l; }
  }

  // ---- État ----
  async function loadStatus() {
    try {
      const d = await api("/api/status", { headers: H() });
      let pinHtml = "";
      if (d.pinterest && d.pinterest.connected) {
        pinHtml = "<div class='row'><b>📌 Pinterest</b><span class='cnt'>✅ connecté</span></div>"
          + "<button class='btn sec' onclick='testPinterest(false)'>📌 Tester Pinterest seul (sans IG/FB)</button>"
          + (d.pinterest.sandbox
              ? "<button class='btn sec' onclick='testPinterest(true)'>🎬 Tester en SANDBOX (pour la démo)</button>"
              : "<button class='btn sec' onclick='connectPinterest(true)'>🎬 Connecter le SANDBOX (pour filmer la démo)</button>")
          + "<div id='pin-test-result' class='hint' style='white-space:pre-wrap'></div>";
      } else if (d.pinterest && d.pinterest.configured) {
        pinHtml = "<button class='btn' onclick='connectPinterest()'>📌 Connecter Pinterest</button>";
      } else {
        pinHtml = "<div class='row'><b>📌 Pinterest</b><span class='cnt'>app non configurée</span></div>";
      }
      $("status-tokens").innerHTML =
        "<div class='row'><b>📸 Token Instagram</b><span class='cnt'>" +
          (d.ig_days === null ? "?" : d.ig_days >= 9999 ? "∞ (sans expiration)" : d.ig_days + " jours") + "</span></div>" +
        "<div class='row'><b>🧵 Token Threads</b><span class='cnt'>" +
          (d.th_days === null ? "?" : d.th_days >= 9999 ? "∞ (sans expiration)" : d.th_days + " jours") + "</span></div>" +
        "<div class='row'><b>📘 Token Facebook</b><span class='cnt'>" +
          (!d.facebook ? "non configurée" :
           d.fb_days === null ? "?" :
           d.fb_days >= 9999 ? "∞ (sans expiration)" : d.fb_days + " jours") + "</span></div>" +
        pinHtml +
        "<button class='btn sec' onclick=\"renew('IG')\">🔄 Renouveler IG</button>" +
        "<button class='btn sec' onclick=\"renew('TH')\">🔄 Renouveler Threads</button>" +
        (d.facebook ? "<button class='btn sec' onclick=\"renew('FB')\">🔄 Renouveler FB</button>" : "");
      const dl = $("status-daily"); dl.classList.remove("state"); dl.innerHTML = "";
      d.galleries.forEach(g => {
        const row = document.createElement("div"); row.className = "row";
        row.innerHTML = "<b>" + g.nom + "</b>";
        const b = document.createElement("button"); b.className = "back";
        b.textContent = g.daily ? "✅ Actif — désactiver" : "Activer";
        b.onclick = async () => {
          await api("/api/daily", { method: "POST", headers: HJ(),
            body: JSON.stringify({ galerie: g.slug, active: !g.daily }) });
          loadStatus();
        };
        row.appendChild(b); dl.appendChild(row);
      });
    } catch (e) { $("status-daily").textContent = "Erreur : " + e.message; }
  }
  async function connectPinterest(sandbox) {
    try {
      const d = await api("/api/pinterest/connect-url" + (sandbox ? "?sandbox=1" : ""),
                          { headers: H() });
      tg.openLink(d.url);  // autorisation dans le navigateur, callback → serveur
    } catch (e) { alert("Échec : " + e.message); }
  }
  async function testPinterest(sandbox) {
    const box = $("pin-test-result");
    if (box) box.textContent = sandbox
      ? "⏳ Création d'une épingle dans le SANDBOX (pour ta démo)…"
      : "⏳ Épinglage de test sur Pinterest (sans toucher IG/FB)…";
    try {
      const d = await api("/api/pinterest/test", { method: "POST", headers: HJ(),
        body: JSON.stringify({ sandbox: !!sandbox }) });
      if (box) box.textContent = d.msg || (d.ok ? "OK" : "Échec");
      haptic(d.ok ? "success" : "warning");
    } catch (e) { if (box) box.textContent = "Erreur : " + e.message; }
  }
  async function runDiag() {
    const btn = $("diag-btn"), box = $("diag-results");
    btn.disabled = true; const label = btn.textContent; btn.textContent = "⏳ Test…";
    box.innerHTML = "";
    try {
      const d = await api("/api/diag", { method: "POST", headers: HJ(), body: "{}" });
      let h = d.ok
        ? "<p class='hint'>✅ Site joignable — HTTP " + d.info.status + " en " + d.info.ms + " ms</p>"
        : "<p class='hint'>❌ Site injoignable — " + d.info.error + " (" + d.info.ms + " ms)</p>";
      const open = d.open || {};
      const hosts = Object.keys(open);
      if (hosts.length) {
        hosts.forEach(hn => {
          const m = Math.max(1, Math.floor(open[hn] / 60));
          h += "<p class='hint'>🔌 Pause active sur " + hn + " — reprise dans ~" + m + " min</p>";
        });
      } else {
        h += "<p class='hint'>🔌 Disjoncteur fermé (aucune pause).</p>";
      }
      if (!d.ok) h += "<p class='hint'>Blocage réseau côté hébergeur : il se lève seul. Réessaie dans quelques minutes.</p>";
      box.innerHTML = h;
      haptic(d.ok ? "success" : "warning");
    } catch (e) { box.innerHTML = "<p class='hint'>Erreur : " + e.message + "</p>"; }
    finally { btn.disabled = false; btn.textContent = label; }
  }
  async function scanGalleries(poll) {
    const btn = $("scan-btn"), box = $("scan-results");
    if (!poll) {
      btn.disabled = true; btn.dataset.label = btn.dataset.label || btn.textContent;
      btn.textContent = "⏳ Scan en cours…";
      box.innerHTML = "<p class='hint'>⏳ Analyse du site en tâche de fond…</p>";
    }
    try {
      const d = await api("/api/scan", { method: "POST", headers: HJ(), body: "{}" });
      if (d.unreachable) {
        box.innerHTML = "<p class='hint'>🌐 Site temporairement injoignable. Réessaie dans quelques minutes.</p>";
      } else if (d.scanning) {
        setTimeout(function(){ scanGalleries(true); }, 4000);   // encore en cours → re-sonde
        return;
      } else {
        renderScan(d.pending || []);
      }
    } catch (e) { box.innerHTML = "<p class='hint'>Erreur : " + e.message + "</p>"; }
    btn.disabled = false; btn.textContent = btn.dataset.label || "🔍 Scanner les galeries";
  }
  function renderScan(pending) {
    const box = $("scan-results"); box.innerHTML = "";
    if (!pending.length) {
      box.innerHTML = "<p class='hint'>✅ Aucune nouvelle galerie à ajouter.</p>";
      return;
    }
    pending.forEach(g => {
      const row = document.createElement("div"); row.className = "row";
      row.innerHTML = "<b>" + g.name + "</b><span class='cnt'>" + g.count + " photos</span>";
      const add = document.createElement("button"); add.className = "back"; add.textContent = "➕ Ajouter";
      const ign = document.createElement("button"); ign.className = "back"; ign.textContent = "🚫 Ignorer";
      add.onclick = () => decideGallery(g.slug, "add", g.name);
      ign.onclick = () => decideGallery(g.slug, "ignore", g.name);
      row.appendChild(add); row.appendChild(ign); box.appendChild(row);
    });
  }
  async function decideGallery(slug, action, name) {
    try {
      await api("/api/gallery/decide", { method: "POST", headers: HJ(),
        body: JSON.stringify({ slug: slug, action: action }) });
      haptic("success");
      alert(action === "add" ? "✅ « " + name + " » ajoutée !" : "🚫 « " + name + " » ignorée.");
      loadStatus();       // rafraîchit la liste auto-pub (nouvelle galerie dispo)
      scanGalleries();    // rafraîchit la liste des détections
    } catch (e) { alert("Échec : " + e.message); }
  }
  async function renew(platform) {
    try {
      const d = await api("/api/renew", { method: "POST", headers: HJ(),
        body: JSON.stringify({ platform: platform }) });
      alert(d.ok ? "✅ Token " + platform + " renouvelé (" + d.days + " j)" : "❌ " + d.error);
      loadStatus();
    } catch (e) { alert("Échec : " + e.message); }
  }

  // ---- Service worker (PWA installable, coquille en cache) ----
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(function(){});
    // Nouvelle version déployée → le nouveau SW prend le contrôle → on recharge
    // UNE fois pour afficher tout de suite la version fraîche (fini le vieux cache).
    var _swReloaded = false;
    navigator.serviceWorker.addEventListener("controllerchange", function () {
      if (_swReloaded) return; _swReloaded = true; location.reload();
    });
  }

  // ---- Démarrage (deep-link compat : /app?galerie=X&mode=reel) ----
  const qs = new URLSearchParams(location.search);
  if (STANDALONE && !tokGet()) {
    $("tokgate").classList.add("on");     // 1re ouverture PWA : demander le code
  } else {
    loadGalleries();
    if (qs.get("galerie")) openGallery(qs.get("galerie"), qs.get("nom") || qs.get("galerie"),
                                       qs.get("mode") === "reel" ? "reel" : "pub");
  }
</script></body></html>"""


# =========================================================================
# ROUTES
# =========================================================================

def register_miniapp(app, hooks) -> None:
    """Branche le Studio sur le Flask existant.

    `hooks` (fournis par app.py, avec dédup inflight) :
      - reel(chat_id, galerie, urls)
      - publish(chat_id, mode, packed_urls, full_caption)
    """

    @app.route("/app")
    @app.route("/studio")           # entrée PWA (écran d'accueil, hors Telegram)
    def studio():
        return _STUDIO_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}

    @app.route("/studio/manifest.webmanifest")
    def studio_manifest():
        manifest = {
            "name": "Agent Photo Studio",
            "short_name": "Studio",
            "start_url": "/studio",
            "scope": "/",
            "display": "standalone",
            "background_color": "#111111",
            "theme_color": "#111111",
            "orientation": "portrait",
            "icons": [
                {"src": "/static/pwa/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/static/pwa/icon-512.png", "sizes": "512x512", "type": "image/png"},
                {"src": "/static/pwa/icon-maskable-512.png", "sizes": "512x512",
                 "type": "image/png", "purpose": "maskable"},
            ],
        }
        return jsonify(manifest), 200, {"Content-Type": "application/manifest+json"}

    @app.route("/sw.js")            # servi à la racine → portée "/" (couvre /studio + /api)
    def studio_sw():
        sw = (
            "const C='studio-v3';\n"
            "self.addEventListener('install',e=>{self.skipWaiting();});\n"
            "self.addEventListener('activate',e=>{e.waitUntil((async()=>{\n"
            "  const ks=await caches.keys();\n"
            "  await Promise.all(ks.filter(k=>k!==C).map(k=>caches.delete(k)));\n"
            "  await self.clients.claim();\n"
            "})());});\n"
            "self.addEventListener('fetch',e=>{\n"
            "  const u=new URL(e.request.url);\n"
            "  if(e.request.method!=='GET') return;\n"
            "  if(u.pathname.startsWith('/api/')) return;         // API : toujours réseau\n"
            "  if(u.pathname==='/studio'||u.pathname==='/app'){    // coquille : réseau puis cache\n"
            "    e.respondWith(fetch(e.request).then(r=>{const c=r.clone();\n"
            "      caches.open(C).then(x=>x.put(e.request,c));return r;})\n"
            "      .catch(()=>caches.match(e.request)));return;}\n"
            "  if(u.pathname.startsWith('/static/')){              // assets : cache puis réseau\n"
            "    e.respondWith(caches.match(e.request).then(m=>m||fetch(e.request).then(r=>{\n"
            "      const c=r.clone();caches.open(C).then(x=>x.put(e.request,c));return r;})));}\n"
            "});\n"
        )
        return sw, 200, {"Content-Type": "application/javascript",
                         "Service-Worker-Allowed": "/"}

    @app.route("/app/picker")
    def miniapp_picker():  # compat avec les anciens boutons
        g = request.args.get("galerie", "")
        nom = request.args.get("nom", "")
        return redirect(f"/app?galerie={g}&nom={nom}&mode=reel")

    @app.route("/app/stats")
    def studio_stats():
        _require_user()
        from dashboard import render_stats_html
        return render_stats_html(), 200, {"Content-Type": "text/html; charset=utf-8"}

    @app.route("/api/galleries")
    def api_galleries():
        """Sans param : liste instantanée. Avec ?counts=1 : ajoute les
        compteurs publiées/total depuis le CACHE (calculé en tâche de fond ;
        JAMAIS de scraping dans la requête). `warming: true` tant que le cache
        n'est pas prêt → le Studio re-sonde."""
        _require_user()
        config = load_yaml_config()
        galeries = config.get("galeries") or []
        base = [{"slug": g, "nom": _display_name(g, config)} for g in galeries]
        if request.args.get("counts") != "1":
            return jsonify({"galleries": base})
        _load_persisted_counts()             # affiche les dernières valeurs connues au boot
        _maybe_refresh_counts()              # non bloquant (rafraîchit en fond)
        data = _COUNTS["data"]
        for item in base:
            c = data.get(item["slug"])
            if c:
                item["done"], item["total"] = c[0], c[1]
        return jsonify({"galleries": base, "warming": not data})

    @app.route("/api/photos")
    def api_photos():
        _require_user()
        from db import already_sent_urls
        from gallery import _canonical, fetch_gallery_photos
        galerie = request.args.get("galerie", "").strip()
        config = load_yaml_config()
        _check_gallery(galerie, config)
        photos = fetch_gallery_photos(config["site_url"], galerie)
        sent = {_canonical(u) for u in already_sent_urls()}
        return jsonify({"photos": [
            {"url": u, "thumb": u.split("?")[0] + "?format=300w",
             "sent": _canonical(u) in sent} for u in photos
        ]})

    def _legit_urls(galerie: str, urls, config, nmax: int):
        """Valide que les URLs appartiennent à la galerie (anti-SSRF)."""
        from gallery import fetch_gallery_photos
        urls = [u for u in (urls or []) if isinstance(u, str)][:nmax]
        if not urls:
            abort(400)
        legit = set(fetch_gallery_photos(config["site_url"], galerie))
        if any(u not in legit for u in urls):
            abort(400)
        return urls

    @app.route("/api/caption", methods=["POST"])
    def api_caption():
        _require_user()
        from ai import generate_caption, split_content
        data = request.get_json(force=True, silent=True) or {}
        galerie = str(data.get("galerie", "")).strip()
        config = load_yaml_config()
        _check_gallery(galerie, config)
        url = _legit_urls(galerie, [data.get("url")], config, 1)[0]
        full = generate_caption(url, galerie)
        visible, alt = split_content(full)
        return jsonify({"visible": visible, "alt": alt})

    @app.route("/api/publish", methods=["POST"])
    def api_publish():
        user = _require_user()
        from ai import SEPARATOR
        from gallery import pack_urls
        data = request.get_json(force=True, silent=True) or {}
        galerie = str(data.get("galerie", "")).strip()
        config = load_yaml_config()
        _check_gallery(galerie, config)
        urls = _legit_urls(galerie, data.get("urls"), config, 10)
        mode = data.get("mode") if data.get("mode") in ("both", "ig", "th", "fb") else "both"
        visible = str(data.get("visible", "")).strip()
        if not visible:
            abort(400)
        full = visible + SEPARATOR + (str(data.get("alt", "")).strip()
                                      or "Fine art photography by David Mertens.")
        hooks["publish"](int(user["id"]), mode, pack_urls(urls), full)
        return jsonify({"ok": True})

    @app.route("/api/schedule", methods=["POST"])
    def api_schedule():
        user = _require_user()
        from ai import SEPARATOR
        from db import schedule_post
        from gallery import pack_urls
        from timezones import TZ, to_utc
        data = request.get_json(force=True, silent=True) or {}
        galerie = str(data.get("galerie", "")).strip()
        config = load_yaml_config()
        _check_gallery(galerie, config)
        urls = _legit_urls(galerie, data.get("urls"), config, 10)
        visible = str(data.get("visible", "")).strip()
        if not visible:
            abort(400)
        try:
            local = dt.datetime.strptime(str(data.get("when", "")), "%Y-%m-%dT%H:%M")
        except ValueError:
            abort(400)
        run_at_utc = to_utc(local.replace(tzinfo=TZ)).replace(tzinfo=None)
        full = visible + SEPARATOR + (str(data.get("alt", "")).strip()
                                      or "Fine art photography by David Mertens.")
        schedule_post(int(user["id"]), pack_urls(urls), full, run_at_utc)
        return jsonify({"ok": True})

    @app.route("/api/scheduled")
    def api_scheduled():
        user = _require_user()
        from db import list_scheduled_posts
        from gallery import unpack_urls
        from timezones import TZ, from_utc_str
        posts = []
        for p in list_scheduled_posts(int(user["id"])):
            urls = unpack_urls(p["image_url"])
            try:
                # run_at est stocké en UTC → conversion heure locale pour l'affichage
                when = from_utc_str(p["run_at"]).astimezone(TZ).strftime("%d/%m à %H:%M")
            except Exception:
                when = p["run_at"]
            posts.append({
                "id": p["id"], "when_local": when, "count": len(urls),
                "thumb": (urls[0].split("?")[0] + "?format=300w") if urls else "",
                "thumbs": [u.split("?")[0] + "?format=400w" for u in urls],
                "caption": p["caption"] or "",
            })
        return jsonify({"posts": posts})

    @app.route("/api/scheduled/cancel", methods=["POST"])
    def api_scheduled_cancel():
        user = _require_user()
        from db import cancel_scheduled_post
        data = request.get_json(force=True, silent=True) or {}
        ok = cancel_scheduled_post(int(data.get("id", 0)), int(user["id"]))
        return jsonify({"ok": bool(ok)})

    @app.route("/api/reel", methods=["POST"])
    def api_reel():
        user = _require_user()
        data = request.get_json(force=True, silent=True) or {}
        galerie = str(data.get("galerie", "")).strip()
        config = load_yaml_config()
        _check_gallery(galerie, config)
        urls = _legit_urls(galerie, data.get("urls"), config, 6)
        if len(urls) < 2:
            abort(400)
        threading.Thread(
            target=hooks["reel"], args=(int(user["id"]), galerie, urls),
            daemon=True, name=f"miniapp-reel-{galerie}",
        ).start()
        return jsonify({"ok": True})

    @app.route("/api/status")
    def api_status():
        user = _require_user()
        from db import get_active_daily_autopubs
        from meta_api import facebook_page_configured, token_days_left
        import pinterest as _pin
        config = load_yaml_config()
        active = {g for cid, g in get_active_daily_autopubs()
                  if str(cid) == str(user["id"])}
        return jsonify({
            # cached_only=True : JAMAIS d'appel réseau Meta dans la requête → l'onglet
            # État (et le bouton Connecter Pinterest) s'affiche instantanément.
            "ig_days": token_days_left("IG", cached_only=True),
            "th_days": token_days_left("TH", cached_only=True),
            "facebook": facebook_page_configured(),
            "fb_days": token_days_left("FB", cached_only=True) if facebook_page_configured() else None,
            "pinterest": {"configured": _pin.app_configured(),
                          "connected": _pin.connected(),
                          "sandbox": _pin.sandbox_connected()},
            "galleries": [
                {"slug": g, "nom": _display_name(g, config), "daily": g in active}
                for g in (config.get("galeries") or [])
            ],
        })

    @app.route("/api/report")
    def api_report():
        _require_user()
        from analyzer import build_report
        return jsonify({"report": build_report()})

    @app.route("/api/plan")
    def api_plan():
        _require_user()
        from planner import build_weekly_plan
        plan = build_weekly_plan()
        return jsonify({"plan": plan["text"] if plan else "Aucune galerie configurée."})

    @app.route("/api/plan/schedule", methods=["POST"])
    def api_plan_schedule():
        user = _require_user()
        hooks["plan"](int(user["id"]))   # programme en tâche de fond
        return jsonify({"ok": True})

    @app.route("/api/pinterest/connect-url")
    def api_pinterest_connect_url():
        _require_user()
        import pinterest as _pin
        from settings import APP_BASE_URL
        if not _pin.app_configured():
            abort(400)
        sandbox = request.args.get("sandbox") == "1"
        return jsonify({"url": _pin.authorize_url(f"{APP_BASE_URL}/pinterest/callback",
                                                  sandbox=sandbox)})

    @app.route("/api/pinterest/test", methods=["POST"])
    def api_pinterest_test():
        """Test ISOLÉ de pins:write : épingle 2 photos d'une galerie sur Pinterest
        SANS publier sur IG/Threads/FB. Vérifie que la connexion écrit vraiment."""
        _require_user()
        import pinterest as _pin
        from gallery import fetch_gallery_photos
        if not _pin.connected():
            return jsonify({"ok": False, "error": "Pinterest non connecté"}), 400
        config = load_yaml_config()
        gals = config.get("galeries") or []
        data = request.get_json(force=True, silent=True) or {}
        galerie = str(data.get("galerie", "") or "").strip()
        if galerie not in gals:
            galerie = "rotterdam" if "rotterdam" in gals else (gals[0] if gals else "")
        try:
            photos = fetch_gallery_photos(config["site_url"], galerie)[:2]
        except Exception as e:
            return jsonify({"ok": False, "error": f"scrape KO : {e}"}), 500
        if not photos:
            return jsonify({"ok": False, "msg": "Aucune photo récupérée pour cette galerie."})
        # Mode SANDBOX (pour filmer la démo d'accès Standard) : crée une vraie
        # épingle dans le sandbox où le Trial autorise l'écriture.
        if data.get("sandbox"):
            ok_sb, msg_sb = _pin.test_sandbox(photos[0])
            return jsonify({"ok": ok_sb, "msg": msg_sb, "galerie": galerie})
        # Légende avec le lien → pin_photos déduit le tableau (ville) automatiquement.
        caption = f"Street photography — https://davidmertens.com/{galerie}"
        ok, total = _pin.pin_photos(photos, caption)
        if ok > 0:
            return jsonify({"ok": True,
                            "msg": f"✅ {ok}/{total} épingle(s) créée(s) sur Pinterest "
                                   f"(galerie {galerie}). Va voir ton tableau « Street {galerie.title()} »."})
        # Échec : diagnostic exact (tableau + épingle avec une vraie image).
        return jsonify({"ok": False,
                        "msg": f"❌ 0/{total} épingle créée. Diagnostic — {_pin.diagnose(photos[0])}"})

    @app.route("/api/daily", methods=["POST"])
    def api_daily():
        user = _require_user()
        from db import set_daily_autopub
        data = request.get_json(force=True, silent=True) or {}
        galerie = str(data.get("galerie", "")).strip()
        config = load_yaml_config()
        _check_gallery(galerie, config)
        set_daily_autopub(int(user["id"]), galerie, bool(data.get("active")))
        return jsonify({"ok": True})

    @app.route("/api/renew", methods=["POST"])
    def api_renew():
        _require_user()
        from meta_api import renew_instagram_token, renew_threads_token
        data = request.get_json(force=True, silent=True) or {}
        platform = data.get("platform")
        if platform == "IG":
            ok, res = renew_instagram_token()
        elif platform == "TH":
            ok, res = renew_threads_token()
        elif platform == "FB":
            from meta_api import renew_facebook_token
            ok, res = renew_facebook_token()
        else:
            abort(400)
        if ok:
            return jsonify({"ok": True, "days": res[1]})
        return jsonify({"ok": False, "error": str(res)[:200]})

    @app.route("/api/scan", methods=["POST"])
    def api_scan():
        """Lance le scan EN TÂCHE DE FOND (jamais de scraping dans la requête) et
        renvoie les galeries en attente + l'état. Le Studio re-sonde tant que
        `scanning` est vrai."""
        _require_user()
        from db import pending_galleries
        from settings import auto_gallery_assets
        from gallery import fetch_gallery_photos
        config = load_yaml_config()
        _maybe_scan()   # non bloquant
        out = []
        for slug in pending_galleries():
            name, _ = auto_gallery_assets(slug)
            try:
                count = len(fetch_gallery_photos(config["site_url"], slug))  # caché
            except Exception:
                count = 0
            out.append({"slug": slug, "name": name, "count": count})
        return jsonify({"pending": out, "scanning": _scan_state["running"],
                        "unreachable": _scan_state["unreachable"]})

    @app.route("/api/diag", methods=["POST"])
    def api_diag():
        """Teste en direct la connexion serveur → site + état du disjoncteur."""
        _require_user()
        from http_client import circuit_state, probe
        config = load_yaml_config()
        site = config["site_url"].rstrip("/")
        ok, info = probe(site + "/sitemap.xml")
        st = circuit_state()
        return jsonify({"ok": ok, "info": info, "open": st.get("open") or {}})

    @app.route("/api/gallery/decide", methods=["POST"])
    def api_gallery_decide():
        """Approuve ('add') ou rejette ('ignore') une galerie détectée."""
        _require_user()
        from db import set_discovered_status
        data = request.get_json(force=True, silent=True) or {}
        slug = str(data.get("slug", "")).strip()
        action = data.get("action")
        if not slug or action not in ("add", "ignore"):
            abort(400)
        set_discovered_status(slug, "added" if action == "add" else "ignored")
        if action == "add":
            from settings import invalidate_config_cache
            invalidate_config_cache()  # la galerie doit apparaître tout de suite
        return jsonify({"ok": True})

    logger.info("Mini App Studio enregistrée (/app + API complète)")
