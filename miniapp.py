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
import threading
import time
from urllib.parse import parse_qsl

from flask import abort, jsonify, redirect, request

from settings import ALLOWED_CHAT_ID, TELEGRAM_TOKEN, load_yaml_config

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


def _require_user():
    user = _validate_init_data(request.headers.get("X-Telegram-Init-Data", ""))
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
# PAGE HTML DU STUDIO
# =========================================================================

_STUDIO_HTML = r"""<!doctype html>
<html lang="fr"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>Studio</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body { margin: 0; padding: 0 0 66px;
    background: var(--tg-theme-bg-color, #111);
    color: var(--tg-theme-text-color, #eee);
    font: 15px/1.45 -apple-system, system-ui, sans-serif; }
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
</style></head><body>

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
  <div id="stats-state" class="state">Chargement…</div>
  <iframe id="stats-frame" hidden></iframe>
</section>

<section id="v-status" hidden>
  <h1>⚙️ État</h1>
  <div id="status-tokens"></div>
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
  const tg = window.Telegram.WebApp; tg.ready(); tg.expand();
  const Main = tg.MainButton;
  const H = () => ({ "X-Telegram-Init-Data": tg.initData });
  const HJ = () => ({ "Content-Type": "application/json", "X-Telegram-Init-Data": tg.initData });
  const $ = id => document.getElementById(id);
  const haptic = k => tg.HapticFeedback && tg.HapticFeedback.notificationOccurred(k);

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
        row.innerHTML = "<b>" + g.nom + "</b><span class='cnt'>…</span>";
        row.onclick = () => openGallery(g.slug, g.nom);
        list.appendChild(row);
      });
      $("gal-state").hidden = true;
      // Compteurs en arrière-plan (peut prendre 10-30 s à froid)
      api("/api/galleries?counts=1", { headers: H() }).then(dc => {
        dc.galleries.forEach(g => {
          const row = $("gal-" + g.slug);
          if (row) row.querySelector(".cnt").textContent = g.done + "/" + g.total + " publiées";
        });
      }).catch(() => {});
    } catch (e) { $("gal-state").textContent = "Erreur : " + e.message; }
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

  // ---- Reel ----
  function launchReel() {
    Main.showProgress();
    fetch("/api/reel", { method: "POST", headers: HJ(),
      body: JSON.stringify({ galerie: galerie, urls: selected }) })
      .then(r => { if (!r.ok) throw new Error("HTTP " + r.status);
        haptic("success"); tg.close(); })
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
      haptic("success"); tg.close();
    } catch (e) { alert("Échec : " + e.message); $("cap-pub").disabled = false; }
  }
  async function scheduleIt() {
    if (!$("cap-text").value.trim()) { alert("Génère ou écris une légende d'abord."); return; }
    try {
      await api("/api/schedule", { method: "POST", headers: HJ(),
        body: JSON.stringify({ galerie: galerie, urls: selected,
                               visible: $("cap-text").value, alt: alt,
                               when: $("cap-when").value }) });
      haptic("success"); tg.close();
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
        const row = document.createElement("div"); row.className = "row";
        row.innerHTML = "<div style='display:flex;align-items:center;gap:10px'>" +
          "<img src='" + p.thumb + "' style='width:44px;height:44px;border-radius:8px;object-fit:cover'>" +
          "<div><b>" + p.when_local + "</b><br><span class='cnt'>" +
          (p.count > 1 ? "carrousel ×" + p.count : "post simple") + "</span></div></div>";
        const b = document.createElement("button"); b.className = "back"; b.textContent = "Annuler";
        b.onclick = async () => {
          if (!confirm("Annuler ce post ?")) return;
          await api("/api/scheduled/cancel", { method: "POST", headers: HJ(),
            body: JSON.stringify({ id: p.id }) });
          loadSched();
        };
        row.appendChild(b); $("sched-list").appendChild(row);
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

  // ---- État ----
  async function loadStatus() {
    try {
      const d = await api("/api/status", { headers: H() });
      let pinHtml = "";
      if (d.pinterest && d.pinterest.connected) {
        pinHtml = "<div class='row'><b>📌 Pinterest</b><span class='cnt'>✅ connecté</span></div>";
      } else if (d.pinterest && d.pinterest.configured) {
        pinHtml = "<button class='btn' onclick='connectPinterest()'>📌 Connecter Pinterest</button>";
      } else {
        pinHtml = "<div class='row'><b>📌 Pinterest</b><span class='cnt'>app non configurée</span></div>";
      }
      $("status-tokens").innerHTML =
        "<div class='row'><b>📸 Token Instagram</b><span class='cnt'>" +
          (d.ig_days === null ? "?" : d.ig_days + " jours") + "</span></div>" +
        "<div class='row'><b>🧵 Token Threads</b><span class='cnt'>" +
          (d.th_days === null ? "?" : d.th_days + " jours") + "</span></div>" +
        "<div class='row'><b>📘 Page Facebook</b><span class='cnt'>" +
          (d.facebook ? "✅ configurée" : "non configurée") + "</span></div>" +
        pinHtml +
        "<button class='btn sec' onclick=\"renew('IG')\">🔄 Renouveler IG</button>" +
        "<button class='btn sec' onclick=\"renew('TH')\">🔄 Renouveler Threads</button>";
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
  async function connectPinterest() {
    try {
      const d = await api("/api/pinterest/connect-url", { headers: H() });
      tg.openLink(d.url);  // autorisation dans le navigateur, callback → serveur
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

  // ---- Démarrage (deep-link compat : /app?galerie=X&mode=reel) ----
  const qs = new URLSearchParams(location.search);
  loadGalleries();
  if (qs.get("galerie")) openGallery(qs.get("galerie"), qs.get("nom") || qs.get("galerie"),
                                     qs.get("mode") === "reel" ? "reel" : "pub");
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
    def studio():
        return _STUDIO_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}

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
        """Sans param : liste INSTANTANÉE (noms seuls, zéro scraping).
        Avec ?counts=1 : ajoute les compteurs publiées/total (scraping, lent à
        froid) — appelé en arrière-plan par le Studio."""
        _require_user()
        config = load_yaml_config()
        galeries = config.get("galeries") or []
        if request.args.get("counts") != "1":
            return jsonify({"galleries": [
                {"slug": g, "nom": _display_name(g, config)} for g in galeries
            ]})
        from concurrent.futures import ThreadPoolExecutor
        from db import already_sent_urls
        from gallery import counts_for_gallery
        sent = already_sent_urls()
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(
                lambda g: (g, counts_for_gallery(config["site_url"], g, sent)),
                galeries,
            ))
        return jsonify({"galleries": [
            {"slug": g, "nom": _display_name(g, config), "done": d, "total": t}
            for g, (d, t) in results
        ]})

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
            "ig_days": token_days_left("IG"),
            "th_days": token_days_left("TH"),
            "facebook": facebook_page_configured(),
            "pinterest": {"configured": _pin.app_configured(),
                          "connected": _pin.connected()},
            "galleries": [
                {"slug": g, "nom": _display_name(g, config), "daily": g in active}
                for g in (config.get("galeries") or [])
            ],
        })

    @app.route("/api/pinterest/connect-url")
    def api_pinterest_connect_url():
        _require_user()
        import pinterest as _pin
        from settings import APP_BASE_URL
        if not _pin.app_configured():
            abort(400)
        return jsonify({"url": _pin.authorize_url(f"{APP_BASE_URL}/pinterest/callback")})

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
        else:
            abort(400)
        if ok:
            return jsonify({"ok": True, "days": res[1]})
        return jsonify({"ok": False, "error": str(res)[:200]})

    logger.info("Mini App Studio enregistrée (/app + API complète)")
