import os
import requests
import yaml
import random
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify

app = Flask(__name__)

# 1. Fonction pour scanner le site davidahmed.me
def get_random_photo():
    try:
        # On va sur la galerie Barcelone
        url = "https://www.davidahmed.me/barcelone"
        response = requests.get(url)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # On cherche toutes les images sur la page
        images = soup.find_all('img')
        image_urls = [img.get('src') for img in images if img.get('src') and 'http' in img.get('src')]
        
        if image_urls:
            return random.choice(image_urls)
        return None
    except Exception as e:
        print(f"Erreur scan : {e}")
        return None

# 2. Envoyer la suggestion avec boutons
def send_suggestion():
    token = os.environ.get('TELEGRAM_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    
    image_url = get_random_photo()
    if not image_url:
        image_url = "https://via.placeholder.com/600x400.png?text=Pas+de+photo+trouvee"

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
        "caption": "David, j'ai trouvé cette photo dans ta galerie Barcelone. On la publie ?",
        "reply_markup": keyboard
    }
    requests.post(url, json=payload)

@app.route("/telegram-webhook", methods=['POST'])
def telegram_webhook():
    data = request.json
    
    # Si on reçoit un message texte (comme ton "Salut")
    if "message" in data:
        chat_id = data["message"]["chat"]["id"]
        # On déclenche l'envoi d'une photo dès que tu écris n'importe quoi
        send_suggestion()
        
    # Si on clique sur un bouton
    if "callback_query" in data:
        action = data["callback_query"]["data"]
        res_url = f"https://api.telegram.org/bot{os.environ.get('TELEGRAM_TOKEN')}/sendMessage"
        
        if action == "next":
            send_suggestion() # On envoie une autre photo
        else:
            requests.post(res_url, json={"chat_id": os.environ.get('TELEGRAM_CHAT_ID'), "text": f"Action enregistrée : {action}"})
        
    return jsonify({"status": "ok"})

@app.route("/")
def index():
    return "Agent Photo en ligne et prêt !"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)