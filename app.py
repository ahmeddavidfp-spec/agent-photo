import os
import sys
import time
import datetime
import random
import sqlite3
import csv
import threading
import re
import logging
import urllib.parse
import yaml
import requests

from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from openai import OpenAI

# =================================================================
# SECTION 1 : CONFIGURATION GLOBALE & LOGS
# =================================================================

# Configuration des logs pour voir ce qui se passe dans la console Render
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s', 
    stream=sys.stdout
)
logger = logging.getLogger()

app = Flask(__name__)

# Chemin de la base de données (Persistant sur Render si /data existe)
DB_PATH = '/data/photos.db' if os.path.exists('/data') else 'photos.db'

# URLs des APIs
TG_API = "https://api.telegram.org/bot"
FB_API = "https://graph.facebook.com/v21.0/"
TH_API = "https://graph.threads.net/v1.0/"


# =================================================================
# SECTION 2 : GESTION DE LA BASE DE DONNEES
# =================================================================

def get_db_connection(): 
    """Crée une connexion avec un timeout de 30s pour éviter 'Database Locked'."""
    return sqlite3.connect(DB_PATH, timeout=30.0)

def init_db():
    """Initialise les tables si elles n'existent pas."""
    conn = get_db_connection()
    
    # Table des photos déjà envoyées
    conn.execute('CREATE TABLE IF NOT EXISTS sent_photos (url TEXT PRIMARY KEY)')
    
    # Table de session (pour se souvenir de la photo en cours de validation)
    conn.execute('CREATE TABLE IF NOT EXISTS current_session (chat_id INTEGER PRIMARY KEY, last_url TEXT, last_caption TEXT)')
    
    # Table du planificateur
    conn.execute('''CREATE TABLE IF NOT EXISTS scheduled_posts 
                    (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                     chat_id INTEGER, 
                     image_url TEXT, 
                     caption TEXT, 
                     run_at TEXT, 
                     status TEXT DEFAULT 'pending')''')
    
    # Mises à jour de structure (Migrations)
    cursor = conn.execute('PRAGMA table_info(sent_photos)')
    cols = [column[1] for column in cursor.fetchall()]
    if 'galerie' not in cols: 
        conn.execute('ALTER TABLE sent_photos ADD COLUMN galerie TEXT')
    if 'date_envoi' not in cols: 
        conn.execute('ALTER TABLE sent_photos ADD COLUMN date_envoi TEXT')
        
    conn.commit()
    conn.close()

def load_config():
    """Charge la configuration YAML ou retourne des valeurs par défaut."""
    try:
        with open("config.yaml", "r") as f: 
            return yaml.safe_load(f)
    except: 
        return {"site_url": "https://www.davidahmed.me", "galeries": ["barcelone"]}

# Initialisation au démarrage
init_db()


# =================================================================
# SECTION 3 : UTILITAIRES & FONCTIONS DB
# =================================================================

def get_session(chat_id):
    conn = get_db_connection()
    res = conn.execute('SELECT last_url, last_caption FROM current_session WHERE chat_id = ?', (chat_id,)).fetchone()
    conn.close()
    return res

def save_session(chat_id, url, cap):
    conn = get_db_connection()
    conn.execute('INSERT OR REPLACE INTO current_session VALUES (?, ?, ?)', (chat_id, url, cap))
    conn.commit()
    conn.close()

def mark_photo_as_sent(url, galerie):
    conn = get_db_connection()
    date_jour = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute('INSERT OR IGNORE INTO sent_photos (url, galerie, date_envoi) VALUES (?, ?, ?)', (url, galerie, date_jour))
    conn.commit()
    conn.close()

def get_db_stats():
    conn = get_db_connection()
    stats = conn.execute('SELECT galerie, COUNT(*) FROM sent_photos GROUP BY galerie').fetchall()
    conn.close()
    if not stats: return "Base vide."
    msg = "📁 **RESUME :**\n"
    for s in stats: 
        name = s[0].capitalize() if s[0] else 'Inconnue'
        msg += f"- {name} : {s[1]}\n"
    return msg

def export_db_to_csv():
    conn = get_db_connection()
    cur = conn.execute('SELECT * FROM sent_photos')
    path = '/tmp/export.csv'
    with open(path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['url', 'galerie', 'date'])
        writer.writerows(cur.fetchall())
    conn.close()
    return path

def get_belgium_offset():
    """Calcul du décalage horaire basique pour la Belgique."""
    month = datetime.datetime.now().month
    return 2 if 4 <= month <= 10 else 1

def split_content(txt):
    """Sépare la légende de la description visuelle (Alt Text)."""
    if "|||" in txt:
        parts = txt.split("|||")
        return parts[0].strip(), parts[1].strip()
    return txt, "Art photography by David Ahmed"

def get_short_url(long_url):
    """
    Raccourcit l'URL via is.gd en utilisant 'params' pour éviter 
    les erreurs 'No connection adapters' dues à l'encodage.
    """
    try:
        # Utilisation de params={} laisse requests gérer l'encodage proprement
        r = requests.get("https://is.gd/create.php", params={"format": "simple", "url": long_url.strip()}, timeout=10)
        
        if r.status_code == 200 and r.text.startswith("http"):
            return r.text.strip()
            
    except Exception as e:
        logger.error(f"Shortener Error: {e}")
        
    # Fallback : on retourne l'URL originale si ça échoue
    return long_url


# =================================================================
# SECTION 4 : GESTION DES TOKENS & SÉCURITÉ
# =================================================================

def renew_threads_token():
    try:
        url = "https://graph.threads.net/access_token"
        params = {
            "grant_type": "th_exchange_token", 
            "client_secret": os.environ.get('THREADS_CLIENT_SECRET'), 
            "access_token": os.environ.get('THREADS_ACCESS_TOKEN')
        }
        r = requests.get(url, params=params)
        res = r.json()
        if "access_token" in res: 
            return True, (res['access_token'], res.get('expires_in', 0) // 86400)
        return False, res
    except Exception as e: return False, str(e)

def get_token_status():
    msg = "📊 **ETAT**\n"
    fb_debug = "https://graph.facebook.com/debug_token"
    th_debug = "https://graph.threads.net/debug_token"
    
    for lbl, env, url in [("IG", "IG_ACCESS_TOKEN", fb_debug), ("TH", "THREADS_ACCESS_TOKEN", th_debug)]:
        tk = os.environ.get(env)
        if tk:
            try:
                r = requests.get(url, params={"input_token": tk, "access_token": tk}, timeout=5).json()
                exp = r.get('data', {}).get('expires_at')
                days = ((datetime.datetime.fromtimestamp(exp) - datetime.datetime.now()).days) if exp else 'OK'
                msg += f"✅ {lbl} : {days}j\n"
            except: 
                msg += f"⚠️ {lbl} : Erreur API\n"
        else: 
            msg += f"❌ {lbl} : Manquant\n"
    return msg


# =================================================================
# SECTION 5 : INTELLIGENCE ARTIFICIELLE (OPENAI)
# =================================================================

# =================================================================
# SECTION 5 : INTELLIGENCE ARTIFICIELLE (OPENAI)
# =================================================================

# --- IA (MODE BILINGUE + SEO 3-TIERS) ---
# =================================================================
# SECTION 5 : INTELLIGENCE ARTIFICIELLE (OPENAI)
# =================================================================

# --- IA (MODE BILINGUE + SEO 3-TIERS + HOOKS ACCROCHEURS) ---
def generate_ai_caption(image_url, galerie_nom):
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    config = load_config()
    
    # Construction du lien propre
    base_url = config.get('site_url', 'davidahmed.me').replace('https://', '').replace('http://', '').rstrip('/')
    display_link = f"{base_url}/{galerie_nom}"
    manual_hashtag = config.get('custom_hashtag', '')
    base_tag = f"#{manual_hashtag}" if manual_hashtag else ""

    SAFE_ACCOUNTS = [
        "archdaily", "architecture_hunter", "buildinglovers", "tv_buildings",
        "streetclassics", "urbanromantix", "raw_urbanshots", "street_avengers",
        "bnw_planet", "bnw_greatshots", "lensculture", "bnw_demand",
        "magnumphotos", "somewheremagazine", "artofvisuals", "beautifuldestinations",
        "natgeotravel", "moodygrams", "streetphotographyinternational"
    ]
    
    # INSTRUCTIONS : ON EXIGE DES TITRES "HOOK" (ACCROCHEURS)
    instructions = f"""You are David Ahmed, fine art photographer. Analyze this photo of {galerie_nom}.
    TASK: Write a viral caption with a **STOP-SCROLLING HOOK** and high-performance SEO.
    
    CRITICAL RULES:
    1. START with English. THEN French.
    2. KEEP IT SHORT. Total output must be under 480 characters.
    3. NO labels like 'Title:', 'Caption:'.
    4. NO Markdown.
    5. SEPARATOR "|||" for Alt Text at the end.
    
    TITLE STRATEGY (The "Hook"):
    - DO NOT use generic descriptions (e.g. "View of a bridge", "Serenity on shore").
    - DO use JOURNALISTIC, EMOTIONAL or INTRIGUING hooks to stop the scroll.
    - Examples: "Why silence matters", "The chaos we ignore", "A moment frozen in time", "New York's hidden side".
    
    HASHTAG STRATEGY (The "3-Tier Method"):
    - Tier 1 (Niche): #bnw_planet, #minimalism...
    - Tier 2 (Location): #BrooklynHeights, #GothicQuarter (Be specific)...
    - Tier 3 (Tech/Vibe): #StreetClassics, #FineArtPhotography...
    *Generate 5 to 7 hashtags.*

    STRUCTURE:
    [STOP-SCROLLING HOOK EN]
    [1 context sentence EN]
    
    [ACCROCHE PERCUTANTE FR]
    [1 phrase de contexte FR]
    
    (cc account1 account2)
    {display_link}
    [The 3-Tier Hashtags]
    |||
    [Visual description for accessibility]"""
    
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": [{"type": "text", "text": instructions}, {"type": "image_url", "image_url": {"url": image_url, "detail": "high"}}]}],
            max_tokens=650, temperature=0.7
        )
        
        raw = response.choices[0].message.content.replace("```markdown", "").replace("```", "").strip()
        
        # Nettoyage des labels IA
        raw = re.sub(r'^(Titre|Title|Caption|English|French|Hook)\s*:\s*', '', raw, flags=re.IGNORECASE)

        if "|||" in raw:
            parts = raw.split("|||")
            caption_part = parts[0].strip()
            alt_part = parts[1].strip()
        else:
            caption_part = raw
            alt_part = f"Fine art photography of {galerie_nom} by David Ahmed."

        # Mentions
        found_accounts = []
        text_for_search = caption_part.lower().replace("_", "").replace(".", "")
        for acc in SAFE_ACCOUNTS:
            if acc in text_for_search: found_accounts.append(f"@{acc}")
        if not found_accounts: found_accounts = ["@lensculture", "@urbanromantix", "@magnumphotos"]
        
        final_mentions_str = f"(cc {' '.join(found_accounts[:2])})" 

        # Nettoyage final
        lines = caption_part.split('\n')
        clean_lines = []
        for line in lines:
            l = line.strip().lower()
            if l.startswith("(") or l.startswith("cc") or l.startswith("@") or "alt text" in l: continue
            if "davidahmed.me" in l: continue
            clean_lines.append(line)
        
        body_text = "\n".join([l for l in clean_lines if not l.startswith("#") and l.strip() != ""]).strip()
        hashtags = "\n".join([l for l in clean_lines if l.startswith("#")]).strip()
        
        if not hashtags: hashtags = f"#StreetPhotography #{galerie_nom} #FineArt {base_tag}"

        final_caption = f"{body_text}\n\n{final_mentions_str}\n{display_link}\n{hashtags}"
        return f"{final_caption}|||{alt_part}"
        
    except Exception as e:
        return f"Photo of {galerie_nom}\nPhoto de {galerie_nom}\n\n{display_link}|||Art photography"


# =================================================================
# SECTION 6 : PUBLICATION RESEAUX SOCIAUX
# =================================================================

def publish_to_instagram(image_url, full_text):
    caption, _ = split_content(full_text)
    token = os.environ.get('IG_ACCESS_TOKEN')
    ig_id = "17841453263147553" 
    
    try:
        # Étape 1 : Upload
        r = requests.post(f"{FB_API}{ig_id}/media", 
                          data={'image_url': image_url, 'caption': caption, 'access_token': token})
        c_id = r.json().get('id')
        if not c_id: return False, r.json()
        
        # Pause pour traitement FB
        time.sleep(10)
        
        # Étape 2 : Publish
        requests.post(f"{FB_API}{ig_id}/media_publish", data={'creation_id': c_id, 'access_token': token})
        return True, "OK"
    except Exception as e: return False, str(e)

# --- FONCTION THREADS (NATIVE IMAGE + TEXTE BILINGUE) ---
def publish_to_threads(image_url, full_text):
    caption, _ = split_content(full_text)
    token = os.environ.get('THREADS_ACCESS_TOKEN')
    th_id = os.environ.get('THREADS_USER_ID')
    
    clean_url = image_url.split('?')[0] + "?format=1000w"
    
    logger.info(f"🧐 DEBUG THREADS | ID: {th_id} | Mode: NATIVE IMAGE + BILINGUE")
    
    if not th_id or not token: return False, "Token manquant"
    
    try:
        # 1. PREPARATION DU TEXTE
        # On garde le lien davidahmed.me cette fois !
        # On enleve juste le "https://" pour faire joli
        clean_caption = caption.replace("https://", "").strip()
        
        # Securite Longueur (Threads = 500 chars max)
        if len(clean_caption) > 495: 
            clean_caption = clean_caption[:490] + "..."

        # 2. CREATION CONTENEUR IMAGE
        url = f"{TH_API}{th_id}/threads"
        payload = {
            'media_type': 'IMAGE',       
            'image_url': clean_url,      
            'text': clean_caption,       
            'access_token': token
        }
        
        r = requests.post(url, json=payload)
        res = r.json()
        
        if 'id' not in res: return False, res 
        container_id = res['id']
        logger.info(f"✅ Conteneur Image cree: {container_id}")
        
        # 3. ATTENTE UPLOAD (25s pour etre sur)
        logger.info("⏳ Attente 25s (Upload Meta)...")
        time.sleep(25) 
        
        # 4. PUBLICATION
        for attempt in range(1, 4):
            r_pub = requests.post(f"{TH_API}{th_id}/threads_publish", 
                                  data={'creation_id': container_id, 'access_token': token})
            if r_pub.status_code == 200: return True, "OK"
            time.sleep(10)
            
        return False, "Timeout"
            
    except Exception as e: return False, str(e)


# =================================================================
# SECTION 7 : TÂCHES DE FOND (BACKGROUND JOBS)
# =================================================================

def background_publish(chat_id, token, mode, image_url, caption):
    logger.info(f"🚀 START JOB | Mode: {mode}")
    try:
        ok_ig = ok_th = False
        res_ig = res_th = "Non demandé"

        if mode in ["both", "ig"]:
            ok_ig, res_ig = publish_to_instagram(image_url, caption)
        
        if mode in ["both", "th"]:
            ok_th, res_th = publish_to_threads(image_url, caption)

        final_msg = ""
        if mode == "both":
            if ok_ig and ok_th:
                final_msg = "🚀 **Succès Total !**\nInsta & Threads : ✅"
                mark_photo_as_sent(image_url, "Auto")
                # Nettoyage session
                try:
                    conn = get_db_connection()
                    conn.execute('DELETE FROM current_session WHERE chat_id = ?', (chat_id,))
                    conn.commit()
                    conn.close()
                except: pass
            else:
                final_msg = f"⚠️ **Résultat Partiel :**\nIG: {'✅' if ok_ig else '❌ ' + str(res_ig)}\nTH: {'✅' if ok_th else '❌ ' + str(res_th)}"
        
        elif mode == "ig":
            if ok_ig:
                final_msg = "📸 **Instagram :** ✅"
                mark_photo_as_sent(image_url, "Auto")
            else: final_msg = f"❌ **Erreur Insta :** {res_ig}"
            
        elif mode == "th":
            if ok_th:
                final_msg = "🧵 **Threads :** ✅"
                mark_photo_as_sent(image_url, "Auto")
            else: final_msg = f"❌ **Erreur Threads :** {res_th}"

        requests.post(f"{TG_API}{token}/sendMessage", json={"chat_id": chat_id, "text": final_msg, "parse_mode": "Markdown"})

    except Exception as e:
        logger.error(f"🔥 CRASH : {str(e)}", exc_info=True)
        requests.post(f"{TG_API}{token}/sendMessage", json={"chat_id": chat_id, "text": f"🔥 Erreur : {str(e)}", "parse_mode": "Markdown"})

def send_galerie_menu(chat_id):
    config = load_config()
    token = os.environ.get('TELEGRAM_TOKEN')
    status = get_token_status()
    
    conn = get_db_connection()
    sent_urls = [row[0] for row in conn.execute('SELECT url FROM sent_photos').fetchall()]
    conn.close()
    
    keyboard = [[{"text": "📈 Stats", "callback_data": "view_stats"}, 
                 {"text": "📥 Export", "callback_data": "export_db_btn"}, 
                 {"text": "🔄 Renew", "callback_data": "renew_threads_btn"}]]
    
    buttons = []
    for g in config.get('galeries', []):
        try:
            # Récupération dynamique des images
            url = f"{config.get('site_url')}/{g}"
            soup = BeautifulSoup(requests.get(url, timeout=10).text, 'html.parser')
            imgs = [img.get('src') for img in soup.find_all('img') if img.get('src')]
            valid = [s if s.startswith('http') else f"{config.get('site_url')}{s}" for s in imgs]
            
            # Calcul du ratio
            count = f"{len([u for u in valid if u in sent_urls])}/{len(valid)}"
            buttons.append({"text": f"{g.capitalize()} {count}", "callback_data": f"select_{g}"})
        except: 
            buttons.append({"text": g.capitalize(), "callback_data": f"select_{g}"})
            
    for i in range(0, len(buttons), 2): 
        keyboard.append(buttons[i:i + 2])
    
    requests.post(f"{TG_API}{token}/sendMessage", json={"chat_id": chat_id, "text": f"{status}\n---\nQuelle galerie ?", "reply_markup": {"inline_keyboard": keyboard}})

def send_suggestion(chat_id, galerie_nom):
    token = os.environ.get('TELEGRAM_TOKEN')
    config = load_config()
    
    # Parsing
    url = f"{config.get('site_url')}/{galerie_nom}"
    soup = BeautifulSoup(requests.get(url).text, 'html.parser')
    valid = [s if s.startswith('http') else f"{config.get('site_url')}{s}" for s in [img.get('src') for img in soup.find_all('img') if img.get('src')]]
    
    # Filtrage DB
    conn = get_db_connection()
    sent = [row[0] for row in conn.execute('SELECT url FROM sent_photos').fetchall()]
    conn.close()
    
    avail = [u for u in valid if u not in sent]
    if not avail:
        requests.post(f"{TG_API}{token}/sendMessage", json={"chat_id": chat_id, "text": "Galerie vide ou toutes les photos déjà envoyées."})
        return
        
    img_url = random.choice(avail)
    
    # Génération IA
    full_content = generate_ai_caption(img_url, galerie_nom)
    save_session(chat_id, img_url, full_content)
    
    visible_caption = full_content.split("|||")[0]
    
    kb = [[{"text": "🚀 Publier sur les deux", "callback_data": "pub_both"}],
          [{"text": "📅 Programmer", "callback_data": "schedule_btn"}],
          [{"text": "📸 Insta", "callback_data": "pub_ig"}, {"text": "🧵 Threads", "callback_data": "pub_th"}],
          [{"text": "🔄 Autre", "callback_data": f"select_{galerie_nom}"}, {"text": "⬅️ Menu", "callback_data": "menu"}]]
    
    requests.post(f"{TG_API}{token}/sendPhoto", json={"chat_id": chat_id, "photo": img_url, "caption": visible_caption, "reply_markup": {"inline_keyboard": kb}})


# =================================================================
# SECTION 8 : ROUTE PRINCIPALE (WEBHOOK TELEGRAM)
# =================================================================

@app.route("/telegram-webhook", methods=['POST'])
def telegram_webhook():
    data = request.json
    token = os.environ.get('TELEGRAM_TOKEN')
    if not data: return jsonify({"status": "ok"})
    
    chat_id = data.get("message", {}).get("chat", {}).get("id") or data.get("callback_query", {}).get("message", {}).get("chat", {}).get("id")
    text = data.get("message", {}).get("text", "").strip()
    action = data.get("callback_query", {}).get("data", "")

    if text: logger.info(f"📩 Reçu texte : {text}")
    if action: logger.info(f"🔘 Reçu action : {action}")

    # Commandes Texte
    if text:
        if text == "/renew_threads":
            success, result = renew_threads_token()
            msg = f"✅ **TOKEN RENOUVELE ({result[1]}j)**\n`{result[0]}`" if success else f"❌ {result}"
            requests.post(f"{TG_API}{token}/sendMessage", json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"})
            return jsonify({"status": "ok"})
        elif text == "/debug_db":
            requests.post(f"{TG_API}{token}/sendMessage", json={"chat_id": chat_id, "text": get_db_stats(), "parse_mode": "Markdown"})
            return jsonify({"status": "ok"})

    # Commandes Boutons
    if action:
        if action == "schedule_btn":
            session = get_session(chat_id)
            if session:
                save_session(chat_id, session[0], f"WAITING_SCHEDULE|{session[1]}")
                requests.post(f"{TG_API}{token}/sendMessage", json={"chat_id": chat_id, "text": "📅 **Heure ?** (HH:MM)", "parse_mode": "Markdown"})
            return jsonify({"status": "ok"})
            
        elif action == "renew_threads_btn":
            success, result = renew_threads_token()
            msg = f"✅ **TOKEN RENOUVELE ({result[1]}j)**\n`{result[0]}`" if success else f"❌ {result}"
            requests.post(f"{TG_API}{token}/sendMessage", json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"})
            return jsonify({"status": "ok"})
            
        elif action == "view_stats":
            requests.post(f"{TG_API}{token}/sendMessage", json={"chat_id": chat_id, "text": get_db_stats(), "parse_mode": "Markdown"})
            
        elif action == "export_db_btn":
            path = export_db_to_csv()
            with open(path, 'rb') as f: requests.post(f"{TG_API}{token}/sendDocument", data={"chat_id": chat_id}, files={"document": f})
            
        elif action == "menu": 
            send_galerie_menu(chat_id)
            
        elif action.startswith("select_"): 
            send_suggestion(chat_id, action.split("_")[1])
            
        else:
            session = get_session(chat_id)
            if session:
                mode = None
                if action == "pub_both": mode = "both"
                elif action == "pub_ig": mode = "ig"
                elif action == "pub_th": mode = "th"
                elif action == "manual_edit":
                    save_session(chat_id, session[0], "WAITING_FOR_MANUAL")
                    requests.post(f"{TG_API}{token}/sendMessage", json={"chat_id": chat_id, "text": "✍️ Envoie ta nouvelle légende."})
                    return jsonify({"status": "ok"})

                if mode:
                    requests.post(f"{TG_API}{token}/sendMessage", json={"chat_id": chat_id, "text": "⏳ **Traitement en cours...**"})
                    threading.Thread(target=background_publish, args=(chat_id, token, mode, session[0], session[1])).start()
            else:
                 requests.post(f"{TG_API}{token}/sendMessage", json={"chat_id": chat_id, "text": "⚠️ **Session expirée.**\nClique sur 'Menu' et génère une nouvelle photo."})
        return jsonify({"status": "ok"})

    # Gestion Texte Libre (Programmation ou Edition Manuelle)
    if text:
        session = get_session(chat_id)
        if session and session[1].startswith("WAITING_SCHEDULE|"):
            try:
                time_str = text.strip().replace('h', ':')
                if ':' not in time_str: time_str += ":00"
                th, tm = map(int, time_str.split(':'))
                
                offset = get_belgium_offset()
                now_be = datetime.datetime.utcnow() + datetime.timedelta(hours=offset)
                
                target = now_be.replace(hour=th, minute=tm, second=0)
                if target <= now_be: target += datetime.timedelta(days=1)
                
                # Conversion en UTC pour stockage
                utc_run = target - datetime.timedelta(hours=offset)
                real_content = session[1].replace("WAITING_SCHEDULE|", "")
                
                conn = get_db_connection()
                conn.execute("INSERT INTO scheduled_posts (chat_id, image_url, caption, run_at) VALUES (?, ?, ?, ?)", 
                             (chat_id, session[0], real_content, utc_run.strftime("%Y-%m-%d %H:%M:%S")))
                conn.commit()
                conn.execute('DELETE FROM current_session WHERE chat_id = ?', (chat_id,))
                conn.commit()
                conn.close()
                requests.post(f"{TG_API}{token}/sendMessage", json={"chat_id": chat_id, "text": f"✅ **Programmé pour {target.strftime('%H:%M')}** (heure belge).", "parse_mode": "Markdown"})
            except:
                requests.post(f"{TG_API}{token}/sendMessage", json={"chat_id": chat_id, "text": "❌ Format invalide (ex: 18:00)."})
            return jsonify({"status": "ok"})
            
        elif session and session[1] == "WAITING_FOR_MANUAL":
            save_session(chat_id, session[0], text)
            requests.post(f"{TG_API}{token}/sendMessage", json={"chat_id": chat_id, "text": "✅ Prêt !", "reply_markup": {"inline_keyboard": [[{"text": "🚀 Les deux", "callback_data": "pub_both"}, {"text": "📅 Programmer", "callback_data": "schedule_btn"}], [{"text": "📸 Insta", "callback_data": "pub_ig"}, {"text": "🧵 Threads", "callback_data": "pub_th"}]]}})
        else:
            send_galerie_menu(chat_id)
            
    return jsonify({"status": "ok"})


# =================================================================
# SECTION 9 : PLANIFICATEUR AUTOMATIQUE (SCHEDULER)
# =================================================================

def scheduler_loop():
    while True:
        try:
            conn = get_db_connection()
            rows = conn.execute("SELECT id, chat_id, image_url, caption, run_at FROM scheduled_posts WHERE status='pending'").fetchall()
            now_utc = datetime.datetime.utcnow()
            
            for row in rows:
                post_id, chat_id, img, full_text, run_at_str = row
                run_at = datetime.datetime.strptime(run_at_str, "%Y-%m-%d %H:%M:%S")
                
                if now_utc >= run_at:
                    token = os.environ.get('TELEGRAM_TOKEN')
                    ok_ig, res_ig = publish_to_instagram(img, full_text)
                    ok_th, res_th = publish_to_threads(img, full_text)
                    
                    status = 'sent' if (ok_ig and ok_th) else 'error'
                    
                    conn.execute("UPDATE scheduled_posts SET status = ? WHERE id = ?", (status, post_id))
                    conn.commit()
                    
                    msg = "⏰ **Post Programmé Exécuté !**\n"
                    msg += f"IG: {'✅' if ok_ig else '❌'} | TH: {'✅' if ok_th else '❌'}"
                    
                    requests.post(f"{TG_API}{token}/sendMessage", json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"})
                    if ok_ig or ok_th: mark_photo_as_sent(img, "Programme")
                    
            conn.close()
        except: pass
        time.sleep(20)

# Démarrage du thread planificateur en mode démon
threading.Thread(target=scheduler_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))