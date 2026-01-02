import os
import requests
import yaml
import random
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from openai import OpenAI

app = Flask(__name__)

# Initialisation OpenAI
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

def load_config():
    with open("config.yaml", "r") as f:
        return yaml.safe_load(f)

def get_random_photo():
    try:
        url = "https://www.davidahmed.me/barcelone"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers)
        soup = BeautifulSoup(response.text, 'html.parser')
        images = soup.find_all('img')
        
        image_urls = []
        for img in images:
            src = img.get('src')
            if src:
                if src.startswith('http'): image_urls.append(src)
                elif src.startswith('/'): image_urls.append(f"https://www.davidahmed.me{src}")
        
        return random.choice(image_urls) if image_urls else None
    except Exception as e:
        print(f"Erreur scan : {e}")
        return None

def generate_ai_caption():
    try:
        config = load_config()
        tone = config.get('ai_tone', 'professionnel, poétique et minimaliste')
        
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": f"Tu es un agent expert en photographie. Ton ton est {tone}."},
                {"role": "user", "content": "Rédige une courte légende Instagram pour une photo de Barcelone."}
            ],
            timeout=10 # On évite que le bot attende trop longtemps
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"Erreur OpenAI : {e}")
        return "Barcelone, l'instant capturé. (Légende de secours)"

def send_suggestion():
    token = os.environ.get('TELEGRAM_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    
    image_url = get_random_photo()
    ai_caption = generate_ai_caption() # L'intelligence artificielle intervient ici

    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    payload = {
        "chat_id": chat_id,
        "photo": image_url,
        "caption": f"✨ **Suggestion de l'IA** :\n\n{ai_caption}",
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
    return "Agent Photo Live"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)