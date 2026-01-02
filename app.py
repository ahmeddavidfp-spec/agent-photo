import os
import requests
import yaml
import random
import sqlite3 # Pour la base de données
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from openai import OpenAI

app = Flask(__name__)

# Initialisation de la base de données
def init_db():
    conn = sqlite3.connect('photos.db')
    c = conn.cursor()
    # Table pour éviter les doublons
    c.execute('''CREATE TABLE IF NOT EXISTS sent_photos (url TEXT PRIMARY KEY)''')
    # NOUVELLE Table pour mémoriser la suggestion actuelle avant publication
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

# --- Gestion de la Mémoire (DB) ---

def is_photo_sent(url):
    conn = sqlite3.connect('photos.db')
    c = conn.cursor()
    c.execute('SELECT 1 FROM sent_photos WHERE url = ?', (url,))
    result = c.fetchone()
    conn.close()
    return result is not None

def mark_photo_as_sent(url):
    conn = sqlite3.connect('photos.db')
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO sent_photos (url) VALUES (?)', (url,))
    conn.commit()
    conn.close()

def save_session(chat_id, url, caption):
    conn = sqlite3.connect('photos.db')
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO current_session (chat_id, last_url, last_caption) 
                 VALUES (?, ?, ?)''', (chat_id, url, caption))
    conn.commit()
    conn.close()

def get_session(chat_id):
    conn = sqlite3.connect('photos.db')
    c = conn.cursor()
    c.execute('SELECT last_url, last_caption FROM current_session WHERE chat_id = ?', (chat_id,))
    res = c.fetchone()
    conn.close()
    return res

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
    except Exception as e:
        print(f"ERREUR Scan: {e}")
        return None

def generate_ai_caption(image_url, galerie_nom):
    api_key = os.environ.get("OPENAI_API_KEY")
    try:
        client = OpenAI(api_key=api_key)
        config = load_config()
        
        instructions = f"""
        Tu es David Ahmed, photographe de rue expert. 
        Ta bio : {config.get('photographer_bio', '')}
        
        MISSION : Analyse cette photo prise à {galerie_nom} pour un post Instagram influent.
        
        CONSIGNES DE STYLE :
        1. Titre sobre (Minuscules avec Majuscule au début). Pas de symboles type ###.
        2. Texte doctrinal : Analyse la composition, l'ombre, le silence. Parle de la condition humaine ou du rythme urbain.
        3. HASHTAGS : Utilise cette base : {config.get('hashtags', '')}. 
           - Ajoute toujours 2 hashtags spécifiques à la ville (ex: #Street{galerie_nom} #Explore{galerie_nom}).
           - Si la photo est très contrastée, ajoute #Chiaroscuro.
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
    except Exception as e:
        return f"L'instant suspendu à {galerie_nom}.\n#streetphotography"

def send_galerie_menu(chat_id):
    config = load_config()
    galeries = config.get('galeries', [])
    token = os.environ.get('TELEGRAM_TOKEN')
    keyboard = []
    for i in range(0, len(galeries), 2):
        row = [{"text": g.capitalize(), "callback_data": f"select_{g}"} for g in galeries[i:i+2]]
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
                      json={"chat_id": chat_id, "text": f"Toutes les photos de {galerie_nom} ont déjà été suggérées !"})
        return

    caption = generate_ai_caption(image_url, galerie_nom)
    
    # ÉTAPE 1 : On enregistre la session (URL + Texte) pour pouvoir publier plus tard
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
    if "message" in data:
        send_galerie_menu(data["message"]["chat"]["id"])
    elif "callback_query" in data:
        chat_id = data["callback_query"]["message"]["chat"]["id"]
        action = data["callback_query"]["data"]
        token = os.environ.get('TELEGRAM_TOKEN')
        requests.post(f"https://api.telegram.org/bot{token}/answerCallbackQuery", 
                      json={"callback_query_id": data["callback_query"]["id"]})

        if action == "menu":
            send_galerie_menu(chat_id)
        elif action.startswith("select_"):
            galerie_nom = action.split("_")[1]
            send_suggestion(chat_id, galerie_nom)
        elif action == "publish":
            # On récupère ce qu'on a mis en mémoire
            session = get_session(chat_id)
            if session:
                url, caption = session
                # Pour l'instant on confirme juste, le code de publication arrive à l'étape suivante
                requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                              json={"chat_id": chat_id, "text": f"Session récupérée. Prêt pour l'étape 2 (Publication réelle).\nPhoto : {url}"})
            
    return jsonify({"status": "ok"})

@app.route("/")
def index():
    return "Agent David Ahmed - Étape 1 Mémoire Active"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)