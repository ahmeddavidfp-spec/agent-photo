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
    c.execute('''CREATE TABLE IF NOT EXISTS sent_photos (url TEXT PRIMARY KEY)''')
    conn.commit()
    conn.close()

init_db()

def load_config():
    try:
        with open("config.yaml", "r") as f:
            return yaml.safe_load(f)
    except Exception as e:
        return {"site_url": "https://www.davidahmed.me", "galeries": ["barcelone"]}

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
                # On ne garde que les photos pas encore envoyées
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
        
        # Instructions mises à jour : Pas de MAJUSCULES intégrales, pas de ###
        instructions = f"""
        Tu es David Ahmed, photographe de rue. 
        Bio : {config.get('photographer_bio', '')}
        Style : Noir et blanc, instant suspendu.
        
        MISSION : Analyse cette photo prise à {galerie_nom} pour Instagram.
        
        CONSIGNES DE FORMATAGE :
        1. Le titre doit être en Minuscules avec une Majuscule au début (ex: Le silence des rues).
        2. NE JAMAIS utiliser de symboles Markdown comme ### ou ** pour le titre.
        3. Rédige 2-3 paragraphes doctrinaux et profonds.
        4. Termine par les hashtags : {config.get('hashtags', '')}
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
    
    # On enregistre l'URL dans la DB pour ne plus la proposer
    mark_photo_as_sent(image_url)
    
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
            
    return jsonify({"status": "ok"})

@app.route("/")
def index():
    return "Agent David Ahmed - DB active"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)