import os
import requests
import yaml
import random
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from openai import OpenAI

app = Flask(__name__)

# 1. Charger la configuration
def load_config():
    try:
        with open("config.yaml", "r") as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"Erreur lecture config: {e}")
        return {"site_url": "https://www.davidahmed.me", "galeries": ["barcelone"]}

# 2. Scanner une galerie spécifique
def get_photo_from_galerie(galerie_nom):
    try:
        config = load_config()
        url = f"{config.get('site_url')}/{galerie_nom}"
        
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers)
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
        
        return random.choice(image_urls) if image_urls else None
    except Exception as e:
        print(f"Erreur scan {galerie_nom}: {e}")
        return None

# 3. Générer la légende avec analyse visuelle
def generate_ai_caption(image_url, galerie_nom):
    api_key = os.environ.get("OPENAI_API_KEY")
    try:
        client = OpenAI(api_key=api_key)
        config = load_config()
        
        instructions = f"""
        Tu es David Ahmed, photographe de rue. 
        Bio : {config.get('photographer_bio', '')}
        Style : Noir et blanc, instant suspendu, silence urbain.
        
        MISSION : Analyse cette photo prise à {galerie_nom} pour Instagram.
        STRUCTURE : Titre en MAJUSCULES, texte doctrinal profond (2-3 paragraphes), hashtags.
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
        return f"SILENCE.\nL'instant suspendu à {galerie_nom}.\n#streetphotography"

# 4. Envoyer le menu des galeries
def send_galerie_menu(chat_id):
    config = load_config()
    galeries = config.get('galeries', [])
    token = os.environ.get('TELEGRAM_TOKEN')
    
    # Création des boutons (2 par ligne)
    keyboard = []
    for i in range(0, len(galeries), 2):
        row = [{"text": g.capitalize(), "callback_data": f"select_{g}"} for g in galeries[i:i+2]]
        keyboard.append(row)
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": "Quelle galerie souhaites-tu explorer, David ?",
        "reply_markup": {"inline_keyboard": keyboard}
    }
    requests.post(url, json=payload)

# 5. Envoyer la suggestion après choix
def send_suggestion(chat_id, galerie_nom):
    token = os.environ.get('TELEGRAM_TOKEN')
    image_url = get_photo_from_galerie(galerie_nom)
    
    if not image_url:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                      json={"chat_id": chat_id, "text": f"Aucune photo trouvée dans {galerie_nom}."})
        return

    ai_caption = generate_ai_caption(image_url, galerie_nom)
    
    payload = {
        "chat_id": chat_id,
        "photo": image_url,
        "caption": ai_caption,
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [
                [{"text": "✅ Publier", "callback_data": "publish"},
                 {"text": "🔄 Autre (Même ville)", "callback_data": f"select_{galerie_nom}"}],
                [{"text": "⬅️ Changer de ville", "callback_data": "menu"}]
            ]
        }
    }
    requests.post(f"https://api.telegram.org/bot{token}/sendPhoto", json=payload)

@app.route("/telegram-webhook", methods=['POST'])
def telegram_webhook():
    data = request.json
    token = os.environ.get('TELEGRAM_TOKEN')
    
    if "message" in data:
        send_galerie_menu(data["message"]["chat"]["id"])
        
    elif "callback_query" in data:
        chat_id = data["callback_query"]["message"]["chat"]["id"]
        action = data["callback_query"]["data"]
        
        if action == "menu":
            send_galerie_menu(chat_id)
        elif action.startswith("select_"):
            galerie_nom = action.replace("select_", "")
            send_suggestion(chat_id, galerie_nom)
        else:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                          json={"chat_id": chat_id, "text": f"Action enregistrée : {action}"})
        
    return jsonify({"status": "ok"})

@app.route("/")
def index():
    return "Agent Photo David avec Menu actif"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)