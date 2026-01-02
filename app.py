import os
import requests
import yaml
import random
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from openai import OpenAI

app = Flask(__name__)

def load_config():
    try:
        with open("config.yaml", "r") as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"ERREUR Config: {e}")
        return {"site_url": "https://www.davidahmed.me", "galeries": ["barcelone"]}

def get_photo_from_galerie(galerie_nom):
    try:
        config = load_config()
        url = f"{config.get('site_url')}/{galerie_nom}"
        print(f"DEBUG: Scan de la galerie {url}")
        
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        images = soup.find_all('img')
        image_urls = []
        for img in images:
            src = img.get('src')
            if src:
                if src.startswith('http'):
                    image_urls.append(src)
                elif src.startswith('/'):
                    image_urls.append(f"{config.get('site_url')}{src}")
        
        print(f"DEBUG: {len(image_urls)} images trouvées.")
        return random.choice(image_urls) if image_urls else None
    except Exception as e:
        print(f"ERREUR Scan: {e}")
        return None

def generate_ai_caption(image_url, galerie_nom):
    print(f"DEBUG: Lancement analyse OpenAI pour {image_url}")
    api_key = os.environ.get("OPENAI_API_KEY")
    try:
        client = OpenAI(api_key=api_key)
        config = load_config()
        
        instructions = f"""
        Tu es David Ahmed, photographe de rue. 
        Bio : {config.get('photographer_bio', '')}
        Style : Noir et blanc, instant suspendu.
        Rédige un post Instagram profond (Titre MAJUSCULES, 2-3 paragraphes, hashtags).
        Ville : {galerie_nom}
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
            max_tokens=800,
            timeout=30 # On laisse du temps à l'IA
        )
        print("DEBUG: Analyse OpenAI réussie.")
        return response.choices[0].message.content
    except Exception as e:
        print(f"ERREUR OpenAI: {e}")
        return f"SILENCE.\nL'instant suspendu à {galerie_nom}.\n#streetphotography"

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
    print(f"DEBUG: Préparation de la suggestion pour {galerie_nom}")
    token = os.environ.get('TELEGRAM_TOKEN')
    
    image_url = get_photo_from_galerie(galerie_nom)
    if not image_url:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                      json={"chat_id": chat_id, "text": "Aucune photo trouvée."})
        return

    caption = generate_ai_caption(image_url, galerie_nom)
    
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
    r = requests.post(f"https://api.telegram.org/bot{token}/sendPhoto", json=payload)
    print(f"DEBUG: Envoi Telegram status: {r.status_code}")

@app.route("/telegram-webhook", methods=['POST'])
def telegram_webhook():
    data = request.json
    if "message" in data:
        send_galerie_menu(data["message"]["chat"]["id"])
    elif "callback_query" in data:
        chat_id = data["callback_query"]["message"]["chat"]["id"]
        action = data["callback_query"]["data"]
        
        # On répond à Telegram pour enlever le petit sablier sur le bouton
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
    return "Agent prêt."

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)