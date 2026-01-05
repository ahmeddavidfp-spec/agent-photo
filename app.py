import os
import requests
import yaml
import random
import sqlite3
import time  
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from openai import OpenAI

app = Flask(__name__)

# --- Configuration du Chemin de la Base de Données (Persistent Disk) ---
DB_PATH = '/data/photos.db' if os.path.exists('/data') else 'photos.db'

def get_db_connection():
    return sqlite3.connect(DB_PATH)

# Initialisation de la base de données
def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS sent_photos (url TEXT PRIMARY KEY)''')
    c.execute('''CREATE TABLE IF NOT EXISTS current_session (
                    chat_id INTEGER PRIMARY KEY, 
                    last_url TEXT, 
                    last_caption TEXT)''')
    conn.commit()
    conn.close()

init_db()

def load_config():
    try:
        with open("config.yaml", "r") as f:
            return yaml.safe_load(f)
    except Exception as e:
        return {"site_url": "https://www.davidahmed.me", "galeries": ["barcelone"]}

# --- Logique de calcul des statistiques (X/Y) ---

def get_galerie_stats(galerie_nom):
    try:
        config = load_config()
        url = f"{config.get('site_url')}/{galerie_nom}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        images = soup.find_all('img')
        all_urls = []
        for img in images:
            src = img.get('src')
            if src:
                full_url = src if src.startswith('http') else f"{config.get('site_url')}{src}"
                all_urls.append(full_url)
        
        total_site = len(all_urls)
        if total_site == 0:
            return 0, 0
            
        conn = get_db_connection()
        c = conn.cursor()
        placeholders = ','.join(['?'] * total_site)
        c.execute(f'SELECT COUNT(*) FROM sent_photos WHERE url IN ({placeholders})', all_urls)
        already_sent = c.fetchone()[0]
        conn.close()
        
        return already_sent, total_site
    except:
        return 0, 0

# --- Gestion de la Mémoire (DB) ---

def is_photo_sent(url):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT 1 FROM sent_photos WHERE url = ?', (url,))
    result = c.fetchone()
    conn.close()
    return result is not None

def mark_photo_as_sent(url):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO sent_photos (url) VALUES (?)', (url,))
    conn.commit()
    conn.close()

def save_session(chat_id, url, caption):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO current_session (chat_id, last_url, last_caption) 
                 VALUES (?, ?, ?)''', (chat_id, url, caption))
    conn.commit()
    conn.close()

def get_session(chat_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT last_url, last_caption FROM current_session WHERE chat_id = ?', (chat_id,))
    res = c.fetchone()
    conn.close()
    return res

# --- Logique Instagram Business API ---

def publish_to_instagram(image_url, caption):
    token = os.environ.get('IG_ACCESS_TOKEN')
    ig_id = os.environ.get('INSTAGRAM_BUSINESS_ID')
    
    if not token or not ig_id:
        return False, "Variables d'environnement manquantes."

    try:
        post_url = f"https://graph.facebook.com/v19.0/{ig_id}/media"
        payload = {
            'image_url': image_url,
            'caption': caption,
            'access_token': token
        }
        r_container = requests.post(post_url, data=payload)
        container_data = r_container.json()
        container_id = container_data.get('id')
        
        if not container_id:
            error_msg = container_data.get('error', {}).get('message', 'Erreur inconnue')
            return False, f"Erreur Meta (Container) : {error_msg}"

        time.sleep(10) 

        publish_url = f"https://graph.facebook.com/v19.0/{ig_id}/media_publish"
        r_publish = requests.post(publish_url, data={
            'creation_id': container_id,
            'access_token': token
        })
        
        if r_publish.status_code == 200:
            return True, "Succès"
        else:
            return False, f"Erreur Meta (Publish) : {r_publish.json()}"
            
    except Exception as e:
        return False, str(e)

# --- Logique de l'Agent ---

def get_photo_from_galerie(galerie_nom):
    try:
        config = load_config()
        url = f"{config.get('site_url')}/{galerie_nom}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        images = soup.find_all('img')
        image_urls = []
        for img in images:
            src = img.get('src')
            if src:
                full_url = src if src.startswith('http') else f"{config.get('site_url')}{src}"
                if not is_photo_sent(full_url):
                    image_urls.append(full_url)
        
        return random.choice(image_urls) if image_urls else None
    except:
        return None

def generate_ai_caption(image_url, galerie_nom):
    api_key = os.environ.get("OPENAI_API_KEY")
    try:
        client = OpenAI(api_key=api_key)
        config = load_config()
        instructions = f"""
        Tu es David Ahmed, photographe de rue expert. Bio: {config.get('photographer_bio', '')}
        MISSION : Analyse cette photo prise à {galerie_nom}.
        FORMAT : Titre direct sans '**', saute deux lignes, analyse doctrinale, hashtags: {config.get('hashtags', '')}
        """
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": instructions},
                    {"type": "image_url", "image_url": {"url": image_url, "detail": "low"}}
                ],
            }],
            max_tokens=800
        )
        return response.choices[0].message.content
    except:
        return f"L'instant suspendu à {galerie_nom}.\n#streetphotography"

def send_galerie_menu(chat_id):
    config = load_config()
    galeries = config.get('galeries', [])
    token = os.environ.get('TELEGRAM_TOKEN')
    keyboard = []
    
    for i in range(0, len(galeries), 2):
        row = []
        for g in galeries[i:i+2]:
            sent, total = get_galerie_stats(g)
            label = f"{g.capitalize()} ({sent}/{total})"
            row.append({"text": label, "callback_data": f"select_{g}"})
        keyboard.append(row)
    
    payload = {
        "chat_id": chat_id,
        "text": "Quelle galerie souhaites-tu explorer, David ?",
        "reply_markup": {"inline_keyboard": keyboard}
    }
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)

def send_suggestion(chat_id, galerie_nom):
    token = os.environ.get('TELEGRAM_TOKEN')
    image_url = get_photo_from_galerie(galerie_nom)
    
    if not image_url:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                      json={"chat_id": chat_id, "text": "Plus de photos disponibles."})
        return

    caption = generate_ai_caption(image_url, galerie_nom)
    save_session(chat_id, image_url, caption)
    
    payload = {
        "chat_id": chat_id,
        "photo": image_url,
        "caption": caption,
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [
                [{"text": "✅ Publier", "callback_data": "publish"},
                 {"text": "🔄 Autre", "callback_data": f"select_{galerie_nom}"}],
                [{"text": "⬅️ Menu", "callback_data": "menu"}]
            ]
        }
    }
    requests.post(f"https://api.telegram.org/bot{token}/sendPhoto", json=payload)

@app.route("/telegram-webhook", methods=['POST'])
def telegram_webhook():
    data = request.json
    token = os.environ.get('TELEGRAM_TOKEN')
    
    if "message" in data:
        msg_obj = data["message"]
        chat_id = msg_obj["chat"]["id"]
        
        # --- LOGIQUE DE MODIFICATION DU TEXTE ---
        # Si David répond (Reply) à un message de photo avec du texte
        if "reply_to_message" in msg_obj and "text" in msg_obj:
            new_text = msg_obj["text"]
            session = get_session(chat_id)
            if session:
                last_url, _ = session
                save_session(chat_id, last_url, new_text) # On met à jour le texte en base
                requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                              json={"chat_id": chat_id, "text": "✅ **Texte mis à jour !**\nClique sur Publier pour valider.", "parse_mode": "Markdown"})
                return jsonify({"status": "ok"})

        # Sinon, on affiche le menu classique
        send_galerie_menu(chat_id)

    elif "callback_query" in data:
        chat_id = data["callback_query"]["message"]["chat"]["id"]
        action = data["callback_query"]["data"]
        
        requests.post(f"https://api.telegram.org/bot{token}/answerCallbackQuery", 
                      json={"callback_query_id": data["callback_query"]["id"]})

        if action == "menu":
            send_galerie_menu(chat_id)
        elif action.startswith("select_"):
            send_suggestion(chat_id, action.split("_")[1])
        elif action == "publish":
            session = get_session(chat_id)
            if session:
                url, caption = session
                requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                              json={"chat_id": chat_id, "text": "⏳ Publication en cours..."})
                success, msg = publish_to_instagram(url, caption)
                text = "🚀 **Publié !**" if success else f"❌ **Échec**\n{msg}"
                if success: mark_photo_as_sent(url)
                requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                              json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})
            
    return jsonify({"status": "ok"})

@app.route("/")
def index(): return "Agent David Ahmed - Actif"

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))