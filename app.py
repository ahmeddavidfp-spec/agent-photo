import os, requests, yaml, random, sqlite3, time, datetime, csv
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from openai import OpenAI

# =================================================================
# SECTION 1 : CONFIGURATION ET INITIALISATION
# =================================================================
app = Flask(__name__)
DB_PATH = '/data/photos.db' if os.path.exists('/data') else 'photos.db'

def get_db_connection(): 
    """Établit la connexion à la base SQLite locale."""
    return sqlite3.connect(DB_PATH)

def init_db():
    """Initialise et met à jour la structure de la base de données (Migration)."""
    conn = get_db_connection()
    
    # 1. Création initiale des tables si elles n'existent pas du tout
    conn.execute('CREATE TABLE IF NOT EXISTS sent_photos (url TEXT PRIMARY KEY)')
    conn.execute('CREATE TABLE IF NOT EXISTS current_session (chat_id INTEGER PRIMARY KEY, last_url TEXT, last_caption TEXT)')
    
    # 2. Migration : Ajout dynamique des colonnes manquantes
    cursor = conn.execute('PRAGMA table_info(sent_photos)')
    existing_columns = [column[1] for column in cursor.fetchall()]
    
    if 'galerie' not in existing_columns:
        print("Mise à jour DB : Ajout de la colonne 'galerie'")
        conn.execute('ALTER TABLE sent_photos ADD COLUMN galerie TEXT')
        
    if 'date_envoi' not in existing_columns:
        print("Mise à jour DB : Ajout de la colonne 'date_envoi'")
        conn.execute('ALTER TABLE sent_photos ADD COLUMN date_envoi TEXT')
        
    conn.commit()
    conn.close()

def load_config():
    """Charge les paramètres du fichier config.yaml (URLs, noms des galeries)."""
    try:
        with open("config.yaml", "r") as f: return yaml.safe_load(f)
    except: return {"site_url": "https://www.davidahmed.me", "galeries": ["barcelone"]}

init_db()

# =================================================================
# SECTION 2 : MOTEUR D'INTELLIGENCE ARTIFICIELLE (OpenAI)
# =================================================================
def generate_ai_caption(image_url, galerie_nom):
    """Analyse l'image et génère une légende structurée (Hook SEO + Technique)."""
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    config = load_config()
    
    # Préparation du lien simplifié pour l'IA (ex: davidahmed.me/new-york)
    base_url = config.get('site_url', 'davidahmed.me').replace('https://', '').replace('http://', '').rstrip('/')
    display_link = f"{base_url}/{galerie_nom}"
    
    manual_hashtag = config.get('custom_hashtag', '')
    extra_tag = f"+ {manual_hashtag}" if manual_hashtag else ""
    
    instructions = f"""Tu es David Ahmed, photographe d'art. Analyse cette photo de {galerie_nom}.
    
    STRATÉGIE : 1. Crochet (Hook) percutant. 2. Vocabulaire riche. 3. 2-3 phrases claires.
    
    STRUCTURE STRICTE :
    - Ligne 1 : Titre percutant.
    - Ligne 2 : Analyse (2-3 phrases).
    - (Saut de ligne)
    - Ligne : Voir la galerie : {display_link}
    - (Saut de ligne)
    - Hashtags {extra_tag}.
    
    STRICT : 
    - PAS de gras (**), PAS de ###, PAS de majuscules intégrales. 
    - Ne transforme PAS le lien en format Markdown [texte](url). Affiche-le tel quel.
    - Max 480 caractères au total."""
    
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": [{"type": "text", "text": instructions}, {"type": "image_url", "image_url": {"url": image_url, "detail": "high"}}]}],
        max_tokens=500, temperature=0.7
    )
    
    raw = response.choices[0].message.content
    
    # Nettoyage final de sécurité contre le Markdown résiduel
    clean = raw.replace("**", "").replace("__", "").replace("### ", "").replace("## ", "").replace("# ", "")
    
    lines = clean.split('\n')
    if lines:
        lines[0] = lines[0].strip().capitalize() # Formate le Hook
    
    return "\n".join(lines).strip()[:495] # Coupe à 495 pour Threads

# =================================================================
# SECTION 3 : LOGIQUE DES RÉSEAUX SOCIAUX (Instagram & Threads)
# =================================================================
def publish_to_instagram(image_url, caption):
    """Publication via Meta Graph API pour Instagram."""
    token = os.environ.get('IG_ACCESS_TOKEN')
    ig_id = "17841453263147553" 
    try:
        r = requests.post(f"https://graph.facebook.com/v21.0/{ig_id}/media", data={'image_url': image_url, 'caption': caption, 'access_token': token})
        res = r.json()
        c_id = res.get('id')
        if not c_id: return False, res
        time.sleep(10)
        requests.post(f"https://graph.facebook.com/v21.0/{ig_id}/media_publish", data={'creation_id': c_id, 'access_token': token})
        return True, "OK"
    except Exception as e: return False, str(e)


def publish_to_threads(image_url, caption):
    """Publication via Threads API (limite stricte de 500 chars)."""
    token = os.environ.get('THREADS_ACCESS_TOKEN')
    th_id = os.environ.get('THREADS_USER_ID')
    
    # Nettoyage URL pour Threads (évite les rejets sur URLs complexes)
    clean_url = image_url.split('?')[0] 
    short_caption = caption[:495] if len(caption) > 500 else caption
    
    try:
        # Étape 1 : Création du conteneur de média
        url = f"https://graph.threads.net/v1.0/{th_id}/threads"
        r = requests.post(url, data={
            'media_type': 'IMAGE', 
            'image_url': clean_url, 
            'text': short_caption, 
            'access_token': token
        }, timeout=30)
        
        res = r.json()
        
        # Gestion spécifique des erreurs API (Token expiré, permissions, etc.)
        if 'id' not in res:
            error_msg = res.get('error', {}).get('message', 'Erreur inconnue')
            return False, f"Meta Error: {error_msg}"
            
        # Étape 2 : Publication du conteneur
        time.sleep(15) # Attente obligatoire pour l'upload Threads
        pub_url = f"https://graph.threads.net/v1.0/{th_id}/threads_publish"
        r_pub = requests.post(pub_url, data={
            'creation_id': res['id'], 
            'access_token': token
        }, timeout=30)
        
        if r_pub.status_code == 200:
            return True, "OK"
        else:
            return False, f"Publish Error: {r_pub.text}"
            
    except Exception as e: 
        return False, f"System Error: {str(e)}"

# =================================================================
# SECTION 4 : GESTION DE LA BASE DE DONNÉES (DB Admin)
# =================================================================
def mark_photo_as_sent(url, galerie):
    """Enregistre une photo publiée dans l'historique avec date et galerie."""
    # Formatage de la date pour un tri facile dans Sequel Ace
    date_jour = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db_connection()
    # Utilisation du schéma v2 : URL, Galerie, Date
    conn.execute('INSERT OR IGNORE INTO sent_photos (url, galerie, date_envoi) VALUES (?, ?, ?)', 
                 (url, galerie, date_jour))
    conn.commit()
    conn.close()

def get_token_status():
    """Vérifie la validité et l'expiration des Tokens Meta (IG & Threads)."""
    status_msg = "📊 **ÉTAT DES ACCÈS**\n"
    tokens = {"IG/FB": os.environ.get('IG_ACCESS_TOKEN'), "Threads": os.environ.get('THREADS_ACCESS_TOKEN')}
    for name, token in tokens.items():
        if not token: status_msg += f"❌ {name} : Manquant\n"; continue
        try:
            endpoint = "graph.facebook.com" if name == "IG/FB" else "graph.threads.net"
            r = requests.get(f"https://{endpoint}/debug_token", params={"input_token": token, "access_token": token}, timeout=5).json()
            data = r.get('data', {})
            exp = data.get('expires_at')
            # Distinction entre tokens permanents et temporaires
            if not exp or exp == 0: status_msg += f"✅ {name} : Permanent\n"
            else:
                days = (datetime.datetime.fromtimestamp(exp) - datetime.datetime.now()).days
                status_msg += f"⏳ {name} : {days} jours\n"
        except: status_msg += f"⚠️ {name} : Vérif impossible\n"
    return status_msg

def get_db_stats():
    """Génère un résumé rapide du contenu de la base pour Telegram."""
    conn = get_db_connection()
    # Récupère le nombre de photos par galerie pour ton suivi
    stats = conn.execute('SELECT galerie, COUNT(*) FROM sent_photos GROUP BY galerie').fetchall()
    conn.close()
    if not stats: return "La base de données est vide."
    msg = "📁 **RÉSUMÉ DES PUBLICATIONS :**\n"
    for s in stats:
        msg += f"- {s[0].capitalize()} : {s[1]} photos\n"
    return msg

# =================================================================
# SECTION 5 : INTERFACE TELEGRAM (Webhook & Menus)
# =================================================================
@app.route("/telegram-webhook", methods=['POST'])
def telegram_webhook():
    data = request.json
    token = os.environ.get('TELEGRAM_TOKEN')
    if not data: return jsonify({"status": "ok"})
    
    # --- Gestion des Messages Textes ---
    if "message" in data:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"]["text"]
        
        # --- COMMANDES ADMIN ---
        if text == "/debug_db":
            conn = get_db_connection()
            rows = conn.execute('SELECT url FROM sent_photos').fetchall()
            conn.close()
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": f"📋 DB : {len(rows)} photos publiées."})
            return jsonify({"status": "ok"})
            
        if text == "/export_db":
            conn = get_db_connection()
            cursor = conn.execute('SELECT * FROM sent_photos')
            with open('/tmp/export.csv', 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['url', 'galerie', 'date_envoi'])
                writer.writerows(cursor.fetchall())
            conn.close()
            with open('/tmp/export.csv', 'rb') as f:
                requests.post(f"https://api.telegram.org/bot{token}/sendDocument", data={"chat_id": chat_id}, files={"document": f})
            return jsonify({"status": "ok"})

        # NOUVEAU : Commande de renouvellement Threads 
        if text == "/renew_threads":
            client_secret = os.environ.get('THREADS_CLIENT_SECRET')
            current_token = os.environ.get('THREADS_ACCESS_TOKEN')
            
            if not client_secret:
                requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                              json={"chat_id": chat_id, "text": "❌ Erreur : THREADS_CLIENT_SECRET manquant sur Render."})
            else:
                url = "https://graph.threads.net/access_token"
                params = {
                    "grant_type": "th_exchange_token",
                    "client_secret": client_secret,
                    "access_token": current_token
                }
                r = requests.get(url, params=params)
                res = r.json()
                
                if "access_token" in res:
                    new_token = res['access_token']
                    days = res.get('expires_in', 0) // 86400
                    msg = f"✅ Token renouvelé pour {days} jours !\n\n⚠️ IMPORTANT : Copie ce token dans tes variables Render :\n`{new_token}`"
                    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                                  json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"})
                else:
                    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                                  json={"chat_id": chat_id, "text": f"❌ Échec de l'échange : {res}"})
            return jsonify({"status": "ok"})

        # --- LOGIQUE DE SESSION (MANUEL / LIEU) ---
        session = get_session(chat_id)
        if session and session[1] == "WAITING_FOR_MANUAL":
            save_session(chat_id, session[0], text)
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "✅ Légende reçue !", "reply_markup": {"inline_keyboard": [[{"text": "📸 Instagram", "callback_data": "pub_ig"}, {"text": "🧵 Threads", "callback_data": "pub_th"}], [{"text": "📍 Lieu", "callback_data": "add_location"}, {"text": "📅 Heure", "callback_data": "schedule_post"}]]}})
        elif session and session[1] == "WAITING_FOR_LOCATION":
            new_cap = f"📍 {text}\n\n{session[1]}"
            save_session(chat_id, session[0], new_cap)
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "✅ Lieu ajouté.", "reply_markup": {"inline_keyboard": [[{"text": "📸 Instagram", "callback_data": "pub_ig"}, {"text": "🧵 Threads", "callback_data": "pub_th"}]]}})
        else: send_galerie_menu(chat_id)

    # --- Gestion des Clics Boutons (Callbacks) ---
    elif "callback_query" in data:
        chat_id = data["callback_query"]["message"]["chat"]["id"]
        action = data["callback_query"]["data"]
        session = get_session(chat_id)
        
        is_already_sent = False
        if session:
            conn = get_db_connection()
            is_already_sent = conn.execute('SELECT 1 FROM sent_photos WHERE url = ?', (session[0],)).fetchone()
            conn.close()

        if action == "view_stats":
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "Analyse en cours..."})
        elif action.startswith("select_"): 
            send_suggestion(chat_id, action.split("_")[1])
        
        elif action == "pub_ig" and session:
            if is_already_sent:
                requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "⚠️ Déjà enregistré dans la base."})
            else:
                ok, res = publish_to_instagram(session[0], session[1])
                if ok: 
                    gal_name = session[0].split('/')[-2] if '/' in session[0] else "inconnue"
                    mark_photo_as_sent(session[0], gal_name)
                    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": f"📸 Insta : ✅"})
                else:
                    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": f"❌ Erreur IG : {res}"})
        
        elif action == "pub_th" and session:
            if is_already_sent:
                requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "⚠️ Déjà enregistré dans la base."})
            else:
                ok, res = publish_to_threads(session[0], session[1])
                if ok: 
                    gal_name = session[0].split('/')[-2] if '/' in session[0] else "inconnue"
                    mark_photo_as_sent(session[0], gal_name)
                    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": f"🧵 Threads : ✅"})
                else:
                    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": f"❌ Erreur TH : {res}"})
        
        elif action == "manual_edit":
            save_session(chat_id, session[0], "WAITING_FOR_MANUAL")
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "✍️ Envoie ton texte."})
            
    return jsonify({"status": "ok"})
# =================================================================
# SECTION 6 : FONCTIONS AUXILIAIRES (Scraping, Session)
# =================================================================
def send_galerie_menu(chat_id):
    """Génère le menu des galeries avec compteur dynamique x/y."""
    config = load_config()
    token = os.environ.get('TELEGRAM_TOKEN')
    status = get_token_status()
    conn = get_db_connection()
    sent_urls = [row[0] for row in conn.execute('SELECT url FROM sent_photos').fetchall()]
    conn.close()
    keyboard = [[{"text": "📈 Statistiques", "callback_data": "view_stats"}]]
    buttons = []
    for g in config.get('galeries', []):
        url = f"{config.get('site_url')}/{g}"
        try:
            soup = BeautifulSoup(requests.get(url, timeout=10).text, 'html.parser')
            imgs = [img.get('src') for img in soup.find_all('img') if img.get('src')]
            valid = [s if s.startswith('http') else f"{config.get('site_url')}{s}" for s in imgs]
            count = f"{len([u for u in valid if u in sent_urls])}/{len(valid)}"
            buttons.append({"text": f"{g.capitalize()} {count}", "callback_data": f"select_{g}"})
        except: buttons.append({"text": g.capitalize(), "callback_data": f"select_{g}"})
    for i in range(0, len(buttons), 2): keyboard.append(buttons[i:i + 2])
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": f"{status}\n---\nQuelle galerie ?", "reply_markup": {"inline_keyboard": keyboard}})

def send_suggestion(chat_id, galerie_nom):
    """Propose une photo aléatoire non publiée de la galerie choisie."""
    token = os.environ.get('TELEGRAM_TOKEN')
    config = load_config()
    soup = BeautifulSoup(requests.get(f"{config.get('site_url')}/{galerie_nom}").text, 'html.parser')
    valid = [s if s.startswith('http') else f"{config.get('site_url')}{s}" for s in [img.get('src') for img in soup.find_all('img') if img.get('src')]]
    conn = get_db_connection(); sent = [row[0] for row in conn.execute('SELECT url FROM sent_photos').fetchall()]; conn.close()
    avail = [u for u in valid if u not in sent]
    if not avail: requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "Galerie terminée."}); return
    img_url = random.choice(avail); cap = generate_ai_caption(img_url, galerie_nom); save_session(chat_id, img_url, cap)
    requests.post(f"https://api.telegram.org/bot{token}/sendPhoto", json={"chat_id": chat_id, "photo": img_url, "caption": cap, "reply_markup": {"inline_keyboard": [[{"text": "📸 Instagram", "callback_data": "pub_ig"}, {"text": "🧵 Threads", "callback_data": "pub_th"}],[{"text": "📍 Lieu", "callback_data": "add_location"}, {"text": "✍️ Manuel", "callback_data": "manual_edit"}],[{"text": "🔄 Autre", "callback_data": f"select_{galerie_nom}"}, {"text": "⬅️ Menu", "callback_data": "menu"}]]}})

def get_session(chat_id):
    conn = get_db_connection(); res = conn.execute('SELECT last_url, last_caption FROM current_session WHERE chat_id = ?', (chat_id,)).fetchone(); conn.close()
    return res

def save_session(chat_id, url, cap):
    conn = get_db_connection(); conn.execute('INSERT OR REPLACE INTO current_session VALUES (?, ?, ?)', (chat_id, url, cap)); conn.commit(); conn.close()

# =================================================================
# LANCEMENT DU SERVEUR
# =================================================================
if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))