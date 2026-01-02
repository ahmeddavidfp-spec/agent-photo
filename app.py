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
        return {
            "site_url": "https://www.davidahmed.me",
            "galeries": ["barcelone"],
            "ai_tone": "professionnel, poétique et minimaliste"
        }

# 2. Scanner une galerie
def get_random_photo():
    try:
        config = load_config()
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

# 3. Générer la légende (Définie AVANT d'être appelée)
def generate_ai_caption(image_url, galerie_nom):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return "L'instant suspendu. #streetphotography"

    try:
        client = OpenAI(api_key=api_key)
        config = load_config()
        
        instructions = f"""
        Tu es le photographe David Ahmed. 
        Ta bio : {config.get('photographer_bio', '')}
        Ton style : Noir et blanc, humain en creux, silence urbain, instant suspendu.

        MISSION : Rédige un post Instagram profond en analysant cette photo prise à {galerie_nom}.
        
        STRUCTURE DU POST :
        1. UN TITRE EVOCATEUR (en majuscules).
        2. UN TEXTE DOCTRINAL (2 à 3 paragraphes) : Analyse la lumière, la géométrie ou l'émotion.
        3. UNE REFLEXION sur la ville de {galerie_nom}.
        4. HASHTAGS : {config.get('hashtags', '')}
        """
        
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": instructions},
                        {"type": "image_url", "image_url": {"url": image_url, "detail": "low"}}
                    ],
                }
            ],
            max_tokens=800
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"Erreur OpenAI Vision : {e}")
        return f"SILENCE.\nL'instant suspendu à {galerie_nom}.\n#streetphotography"

# 4. Envoyer la suggestion
def send_suggestion():
    token = os.environ.get('TELEGRAM_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    
    image_url, galerie_nom = get_random_photo()
    
    if not image_url:
        image_url = "https://via.placeholder.com/600x400.png?text=Image+non+trouvee"
        ai_caption = "Désolé David, je n'ai pas trouvé d'image."
    else:
        # Appel de la fonction définie juste au-dessus
        ai_caption = generate_ai_caption(image_url, galerie_nom)

    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    payload = {
        "chat_id": chat_id,
        "photo": image_url,
        "caption": ai_caption,
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "✅ Publier", "callback_data": "publish"},
                {"text": "🔄 Autre", "callback_data": "next"}
            ]]
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
        if action == "next":
            send_suggestion()
        else:
            token = os.environ.get('TELEGRAM_TOKEN')
            chat_id = data["callback_query"]["message"]["chat"]["id"]
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                          json={"chat_id": chat_id, "text": f"Action enregistrée : {action}"})
    return jsonify({"status": "ok"})

@app.route("/")
def index():
    return "Agent David Ahmed opérationnel"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)