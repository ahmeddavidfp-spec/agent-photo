import os
import requests
import yaml
import random
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from openai import OpenAI

app = Flask(__name__)

# 1. Charger la configuration (Bio, Galeries, Ton)
def load_config():
    try:
        with open("config.yaml", "r") as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"Erreur lecture config: {e}")
        return {
            "site_url": "https://www.davidahmed.me",
            "galeries": ["barcelone"],
            "ai_tone": "professionnel, poétique et minimaliste"
        }

# 2. Scanner une galerie aléatoire parmi ta liste
def get_random_photo():
    try:
        config = load_config()
        # Choix aléatoire d'une galerie (Barcelone ou Tokyo)
        galerie = random.choice(config.get('galeries', ['barcelone']))
        url = f"{config.get('site_url')}/{galerie}"
        
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
        
        if image_urls:
            return random.choice(image_urls), galerie
        return None, None
    except Exception as e:
        print(f"Erreur scan : {e}")
        return None, None

# 3. Générer une légende basée sur ta Bio et ton style
def generate_ai_caption(galerie_nom):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return "L'instant suspendu. #streetphotography"

    try:
        client = OpenAI(api_key=api_key)
        config = load_config()
        
        # On injecte ta bio et tes hashtags dans le prompt
        instructions = f"""
        Tu es l'assistant du photographe David Ahmed.
        Bio du photographe : {config.get('photographer_bio', '')}
        Ton recherché : {config.get('ai_tone', '')}
        
        Mission : Rédige une légende Instagram pour une photo prise à {galerie_nom}.
        Structure impérative :
        1. Un TITRE court en MAJUSCULES.
        2. Une phrase poétique sur le silence, l'humanité ou l'ombre.
        3. Les hashtags : {config.get('hashtags', '')}
        """
        
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": instructions},
                {"role": "user", "content": f"Génère la légende pour une photo de {galerie_nom}."}
            ],
            timeout=15
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"Erreur OpenAI : {e}")
        return f"SILENCE.\nL'instant suspendu à {galerie_nom}.\n#streetphotography"

# 4. Envoyer la suggestion à Telegram
def send_suggestion():
    token = os.environ.get('TELEGRAM_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    
    image_url, galerie_nom = get_random_photo()
    
    if not image_url:
        image_url = "https://via.placeholder.com/600x400.png?text=Image+non+trouvee"
        ai_caption = "Désolé David, je n'ai pas trouvé d'image dans tes galeries."
    else:
        ai_caption = generate_ai_caption(galerie_nom)

    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    
    payload = {
        "chat_id": chat_id,
        "photo": image_url,
        "caption": ai_caption,
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [
                [
                    {"text": "✅ Publier", "callback_data": "publish"},
                    {"text": "🔄 Autre", "callback_data": "next"}
                ]
            ]
        }
    }
    requests.post(url, json=payload)

@app.route("/telegram-webhook", methods=['POST'])
def telegram_webhook():
    data = request.json
    if "message" in data:
        send_suggestion()
    elif "callback_query" in data:
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
    return "Agent David Ahmed IA - Opérationnel"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)