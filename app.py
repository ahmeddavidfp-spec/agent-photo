import os, requests, yaml, random, sqlite3, time
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from openai import OpenAI

app = Flask(__name__)

# --- Base de Données Persistante ---
DB_PATH = '/data/photos.db' if os.path.exists('/data') else 'photos.db'

def get_db_connection(): return sqlite3.connect(DB_PATH)

def init_db():
    conn = get_db_connection()
    conn.execute('CREATE TABLE IF NOT EXISTS sent_photos (url TEXT PRIMARY KEY)')
    conn.execute('CREATE TABLE IF NOT EXISTS current_session (chat_id INTEGER PRIMARY KEY, last_url TEXT, last_caption TEXT)')
    conn.commit()
    conn.close()

init_db()

def load_config():
    try:
        with open("config.yaml", "r") as f: return yaml.safe_load(f)
    except: return {"site_url": "https://www.davidahmed.me", "galeries": ["barcelone"]}

# --- LOGIQUE DE PUBLICATION ---

def publish_to_instagram(image_url, caption):
    token = os.environ.get('IG_ACCESS_TOKEN')
    ig_id = os.environ.get('INSTAGRAM_BUSINESS_ID')
    try:
        r = requests.post(f"https://graph.facebook.com/v19.0/{ig_id}/media", 
                          data={'image_url': image_url, 'caption': caption, 'access_token': token})
        c_id = r.json().get('id')
        if not c_id: return False, r.json()
        time.sleep(10)
        requests.post(f"https://graph.facebook.com/v19.0/{ig_id}/media_publish", data={'creation_id': c_id, 'access_token': token})
        return True, "OK"
    except Exception as e: return False, str(e)

# --- GÉNÉRATION DE LÉGENDE ---

def generate_ai_caption(image_url, galerie_nom):
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    config = load_config()
    site_url = config.get('site_url', 'https://www.davidahmed.me').rstrip('/')
    galerie_link = f"{site_url}/{galerie_nom}"

    instructions = f"""Tu es David Ahmed. Bio: {config.get('photographer_bio', '')}
    MISSION : Agis en photographe et curateur. Analyse cette photo à {galerie_nom}. 
    TON : {config.get('ai_tone', '')}
    FORMAT : TITRE EN MAJUSCULES, description (MAX 350 car.), "Série complète disponible sur : {galerie_link}", puis 8 hashtags pertinents parmi : {config.get('hashtags', '')}
    STRICT : Pas d'émojis. Pas de Markdown (pas de **)."""
    
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": [{"type": "text", "text": instructions}, {"type": "image_url", "image_url": {"url": image_url}}]}],
        max_tokens=500,
        temperature=0.8
    )
    return response.choices[0].message.content

# --- GESTION TELEGRAM ---

def send_galerie_menu(chat_id):
    config = load_config()
    galeries = config.get('galeries', [])
    token = os.environ.get('TELEGRAM_TOKEN')
    keyboard = []
    for i in range(0, len(galeries), 2):
        row = [{"text": g.capitalize(), "callback_data": f"select_{g}"} for g in galeries[i:i+2]]
        keyboard.append(row)
    
    payload = {"chat_id": chat_id, "text": "Quelle galerie souhaites-tu explorer, David ?", "reply_markup": {"inline_keyboard": keyboard}}
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)

def send_suggestion(chat_id, galerie_nom):
    token = os.environ.get('TELEGRAM_TOKEN')
    config = load_config()
    url = f"{config.get('site_url')}/{galerie_nom}"
    soup = BeautifulSoup(requests.get(url).text, 'html.parser')
    images = [img.get('src') for img in soup.find_all('img') if img.get('src')]
    
    valid_photos = []
    for src in images:
        full_url = src if src.startswith('http') else f"{config.get('site_url')}{src}"
        conn = get_db_connection()
        res = conn.execute('SELECT 1 FROM sent_photos WHERE url = ?', (full_url,)).fetchone()
        conn.close()
        if not res: valid_photos.append(full_url)

    if not valid_photos:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "Plus de photos disponibles dans cette galerie."})
        return

    image_url = random.choice(valid_photos)
    caption = generate_ai_caption(image_url, galerie_nom)
    save_session(chat_id, image_url, caption)

    payload = {
        "chat_id": chat_id, "photo": image_url, "caption": caption,
        "reply_markup": {
            "inline_keyboard": [
                [{"text": "✅ Publier", "callback_data": "publish"}],
                [{"text": "🔄 Autre", "callback_data": f"select_{galerie_nom}"}, {"text": "⬅️ Menu", "callback_data": "menu"}]
            ]
        }
    }
    requests.post(f"https://api.telegram.org/bot{token}/sendPhoto", json=payload)

@app.route("/telegram-webhook", methods=['POST'])
def telegram_webhook():
    data = request.json
    token = os.environ.get('TELEGRAM_TOKEN')
    if "message" in data:
        chat_id = data["message"]["chat"]["id"]
        send_galerie_menu(chat_id)
    elif "callback_query" in data:
        chat_id = data["callback_query"]["message"]["chat"]["id"]
        action = data["callback_query"]["data"]
        session = get_session(chat_id)

        if action == "menu": send_galerie_menu(chat_id)
        elif action.startswith("select_"): send_suggestion(chat_id, action.split("_")[1])
        elif action == "publish" and session:
            url, cap = session
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "⏳ Publication en cours..."})
            ok, msg = publish_to_instagram(url, cap)
            if ok: mark_photo_as_sent(url)
            res_text = "🚀 Publié !" if ok else f"❌ Erreur : {msg}"
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": res_text})
    return jsonify({"status": "ok"})

# --- Fonctions Session ---
def get_session(chat_id):
    conn = get_db_connection()
    res = conn.execute('SELECT last_url, last_caption FROM current_session WHERE chat_id = ?', (chat_id,)).fetchone()
    conn.close()
    return res

def save_session(chat_id, url, cap):
    conn = get_db_connection()
    conn.execute('INSERT OR REPLACE INTO current_session VALUES (?, ?, ?)', (chat_id, url, cap))
    conn.commit()
    conn.close()

def mark_photo_as_sent(url):
    conn = get_db_connection()
    conn.execute('INSERT OR IGNORE INTO sent_photos VALUES (?)', (url,))
    conn.commit()
    conn.close()

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))