import os
import requests
import yaml
import psycopg2
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify

app = Flask(__name__)

# 1. Charger la config
def load_config():
    with open("config.yaml", "r") as f:
        return yaml.safe_load(f)

# 2. Envoyer un message avec boutons sur Telegram
def send_telegram_suggestion(image_url, caption):
    token = os.environ.get('TELEGRAM_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    
    # Création des boutons (Inline Keyboard)
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Publier", "callback_data": "publish"},
                {"text": "🔄 Autre", "callback_data": "next"}
            ],
            [
                {"text": "📂 Galeries", "callback_data": "galleries"},
                {"text": "❌ Annuler", "callback_data": "cancel"}
            ]
        ]
    }
    
    payload = {
        "chat_id": chat_id,
        "photo": image_url,
        "caption": caption,
        "reply_markup": keyboard
    }
    
    return requests.post(url, json=payload)

@app.route("/telegram-webhook", methods=['POST'])
def telegram_webhook():
    data = request.json
    # Cette partie gérera les clics sur les boutons plus tard
    if "callback_query" in data:
        callback_data = data["callback_query"]["data"]
        chat_id = data["callback_query"]["message"]["chat"]["id"]
        
        # Réponse simple pour le test
        res_url = f"https://api.telegram.org/bot{os.environ.get('TELEGRAM_TOKEN')}/sendMessage"
        requests.post(res_url, json={"chat_id": chat_id, "text": f"Tu as choisi : {callback_data}"})
        
    return jsonify({"status": "ok"})

@app.route("/")
def index():
    return "Agent Photo Telegram en ligne !"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)