import os
import requests
import yaml
import random
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from openai import OpenAI

app = Flask(__name__)

# Initialisation du client OpenAI
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# 1. Charger la config pour le ton de l'IA
def load_config():
    with open("config.yaml", "r") as f:
        return yaml.safe_load(f)

# 2. Fonction pour scanner le site davidahmed.me
def get_random_photo():
    try:
        url = "https://www.davidahmed.me/barcelone"
        # Ajout d'un User-Agent pour éviter d'être bloqué par le site
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # On cherche toutes les images
        images = soup.find_all('img')
        # On filtre les URLs valides (statiques ou HTTP)
        image_urls = []
        for img in images:
            src = img.get('src')
            if src:
                if src.startswith('http'):
                    image_urls.append(src)
                elif src.startswith('/'):
                    image_urls.append(f"https://www.davidahmed.me{src}")
        
        if image_urls:
            return random.choice(image_urls)
        return None
    except Exception as e:
        print(f"Erreur scan : {e}")
        return None

# 3. Générer une légende avec OpenAI
def generate_ai_caption():
    config = load_config()
    tone = config.get('ai_tone', 'professionnel, poétique et minimaliste')
    
    try:
        response = client.chat.completions.create(
            model="gpt-4o", # Modèle performant pour l'analyse et le texte
            messages=[
                {"role": "system", "content": f"Tu es un agent expert en photographie. Ton ton est {tone}."},
                {"role": "user", "content": "Rédige une courte légende Instagram captivante pour une photo de ma galerie à Barcelone."}
            ]
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"Erreur OpenAI : {e}")
        return "Une superbe capture de Barcelone."

# 4. Envoyer la suggestion avec boutons
def send_suggestion():
    token = os.environ.get('TELEGRAM_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    
    image_url = get_random_photo()
    if not image_url:
        image_url = "https://via.placeholder.com/600x400.png?text=Pas+de+photo+trouvee"

    ai_caption = generate_ai_caption()
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Publier", "callback_data": "publish"},
                {"text": "🔄 Autre", "callback_data": "next"}
            ]
        ]
    }
    
    payload = {
        "chat_id": chat_id,
        "photo": image_url,
        "caption": f"✨ **Suggestion de l'IA** :\n\n{ai_caption}\n\nOn publie ?",
        "parse_mode": "Markdown",
        "reply_markup": keyboard
    }
    requests.post(url, json=payload)

@app.route("/telegram-webhook", methods=['POST'])
def telegram_webhook():
    data = request.json
    
    # Si on reçoit un message texte
    if "message" in data:
        send_suggestion()
        
    # Si on clique sur un bouton
    if "callback_query" in data:
        action = data["callback_query"]["data"]
        token = os.environ.get('TELEGRAM_TOKEN')
        chat_id = data["callback_query"]["message"]["chat"]["id"]
        
        if action == "next":
            send_suggestion()
        else:
            res_url = f"https://api.telegram.org/bot{token}/sendMessage"
            requests.post(res_url, json={"chat_id": chat_id, "text": f"Action enregistrée : {action}"})
        
    return jsonify({"status": "ok"})

@app.route("/")
def index():
    return "Agent Photo avec IA en ligne !"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)