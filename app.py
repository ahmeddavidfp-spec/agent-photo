import os, requests, yaml, random, sqlite3, time
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from openai import OpenAI

app = Flask(__name__)
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

# --- LOGIQUE STATS X/Y ---
def get_galerie_stats(galerie_nom):
    try:
        config = load_config()
        url = f"{config.get('site_url')}/{galerie_nom}"
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
        soup = BeautifulSoup(response.text, 'html.parser')
        images = [img.get('src') for img in soup.find_all('img') if img.get('src')]
        all_urls = [src if src.startswith('http') else f"{config.get('site_url')}{src}" for src in images]
        if not all_urls: return 0, 0
        conn = get_db_connection()
        placeholders = ','.join(['?'] * len(all_urls))
        already_sent = conn.execute(f'SELECT COUNT(*) FROM sent_photos WHERE url IN ({placeholders})', all_urls).fetchone()[0]
        conn.close()
        return already_sent, len(all_urls)
    except: return 0, 0

# --- PUBLICATIONS ---
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
    except: return False, "Erreur Instagram"

def publish_to_threads(image_url, caption):
    token = os.environ.get('IG_ACCESS_TOKEN')
    th_id = os.environ.get('THREADS_USER_ID')
    try:
        r = requests.post(f"https://graph.threads.net/v1.0/{th_id}/threads", 
                          data={'image_url': image_url, 'text': caption, 'access_token': token})
        c_id = r.json().get('id')
        if not c_id: return False, r.json()
        time.sleep(10)
        requests.post(f"https://graph.threads.net/v1.0/{th_id}/threads_publish", data={'creation_id': c_id, 'access_token': token})
        return True, "OK"
    except: return False, "Erreur Threads"

# --- IA ---
def generate_ai_caption(image_url, galerie_nom):
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    config = load_config()
    site_url = config.get('site_url', 'https://www.davidahmed.me').rstrip('/')
    galerie_link = f"{site_url}/{galerie_nom}"
    instructions = f"""Tu es David Ahmed. Bio: {config.get('photographer_bio', '')}
    MISSION : Analyse cette photo à {galerie_nom}. TON : {config.get('ai_tone', '')}
    FORMAT : TITRE EN MAJUSCULES (SANS SYMBOLES), description cinématographique (MAX 350 car.), "Série complète disponible sur : {galerie_link}", puis 8 hashtags pertinents parmi : {config.get('hashtags', '')}
    STRICT : Pas d'émojis. Pas de Markdown (pas de **). Vocabulaire varié."""
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": [{"type": "text", "text": instructions}, {"type": "image_url", "image_url": {"url": image_url}}]}],
        max_tokens=500, temperature=0.8
    )
    return response.choices[0].message.content

# --- TELEGRAM ---
def send_galerie_menu(chat_id):
    config = load_config()
    galeries = config.get('galeries', [])
    token = os.environ.get('TELEGRAM_TOKEN')
    keyboard = []
    for i in range(0, len(galeries), 2):
        row = []
        for g in galeries[i:i+2]:
            sent, total = get_galerie_stats(g)
            row.append({"text": f"{g.capitalize()} ({sent}/{total})", "callback_data": f"select_{g}"})
        keyboard.append(row)
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                  json={"chat_id": chat_id, "text": "Quelle galerie explorer ?", "reply_markup": {"inline_keyboard": keyboard}})

def send_suggestion(chat_id, galerie_nom):
    token = os.environ.get('TELEGRAM_TOKEN')
    config = load_config()
    url = f"{config.get('site_url')}/{galerie_nom}"
    soup = BeautifulSoup(requests.get(url).text, 'html.parser')
    images = [img.get('src') for img in soup.find_all('img') if img.get('src')]
    valid_photos = [src if src.startswith('http') else f"{config.get('site_url')}{src}" for src in images]
    
    # Filtrer déjà envoyées
    valid_photos = [u for u in valid_photos if not conn_check_sent(u)]
    if not valid_photos:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "Plus de photos."})
        return
    
    img_url = random.choice(valid_photos)
    cap = generate_ai_caption(img_url, galerie_nom)
    save_session(chat_id, img_url, cap)
    payload = {"chat_id": chat_id, "photo": img_url, "caption": cap, "reply_markup": {"inline_keyboard": [
        [{"text": "🚀 Publier Partout", "callback_data": "publish_all"}],
        [{"text": "🔄 Autre", "callback_data": f"select_{galerie_nom}"}, {"text": "⬅️ Menu", "callback_data": "menu"}]
    ]}}
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
        session = get_session(chat_id)
        if action == "menu": send_galerie_menu(chat_id)
        elif action.startswith("select_"): send_suggestion(chat_id, action.split("_")[1])
        elif action == "publish_all" and session:
            url, cap = session
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "⏳ Publication..."})
            publish_to_instagram(url, cap)
            publish_to_threads(url, cap)
            mark_photo_as_sent(url)
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "🚀 En ligne !"})
    return jsonify({"status": "ok"})

def conn_check_sent(url):
    conn = get_db_connection()
    res = conn.execute('SELECT 1 FROM sent_photos WHERE url = ?', (url,)).fetchone()
    conn.close()
    return res is not None

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