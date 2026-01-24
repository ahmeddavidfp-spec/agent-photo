import os, requests, yaml, random, sqlite3, time, datetime
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

def publish_to_instagram(image_url, caption):
    token = os.environ.get('IG_ACCESS_TOKEN')
    ig_id = "17841453263147553" 
    try:
        r = requests.post(f"https://graph.facebook.com/v21.0/{ig_id}/media", data={'image_url': image_url, 'caption': caption, 'access_token': token})
        res = r.json()
        c_id = res.get('id')
        if not c_id: return False, res
        time.sleep(10)
        requests.post(f"https://graph.facebook.com/v21.0/{ig_id}/media_publish", data={'creation_id': c_id, 'access_token': token})
        return True, "OK"
    except Exception as e: return False, str(e)

def publish_to_threads(image_url, caption):
    token = os.environ.get('THREADS_ACCESS_TOKEN')
    th_id = os.environ.get('THREADS_USER_ID')
    clean_url = image_url.split('?')[0]
    # Sécurité SEO : Threads limite à 500 chars. On coupe si besoin.
    short_caption = caption[:495] if len(caption) > 500 else caption
    try:
        url = f"https://graph.threads.net/v1.0/{th_id}/threads"
        r = requests.post(url, data={'media_type': 'IMAGE', 'image_url': clean_url, 'text': short_caption, 'access_token': token}, timeout=30)
        res = r.json()
        if 'id' not in res: return False, res
        time.sleep(15) 
        pub_url = f"https://graph.threads.net/v1.0/{th_id}/threads_publish"
        r_pub = requests.post(pub_url, data={'creation_id': res['id'], 'access_token': token}, timeout=30)
        return (True, "OK") if r_pub.status_code == 200 else (False, r_pub.text)
    except Exception as e: return False, str(e)

def generate_ai_caption(image_url, galerie_nom):
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    config = load_config()
    galerie_link = f"{config.get('site_url').rstrip('/')}/{galerie_nom}"
    extra_tag = f"+ {config.get('custom_hashtag', '')}" if config.get('custom_hashtag') else ""
    
    instructions = f"""Tu es David Ahmed, photographe d'art. Analyse cette photo.
    1. Hook SEO percutant. 2. Vocabulaire riche. 3. 2-3 phrases claires.
    STRICT : Max 400 caractères au total. Pas de gras, pas de ###, pas de MAJUSCULES intégrales."""
    
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": [{"type": "text", "text": instructions}, {"type": "image_url", "image_url": {"url": image_url, "detail": "high"}}]}],
        max_tokens=400, temperature=0.7
    )
    raw = response.choices[0].message.content
    clean = raw.replace("**", "").replace("__", "").replace("### ", "").replace("## ", "").replace("# ", "")
    lines = clean.split('\n')
    if lines: lines[0] = lines[0].strip().capitalize()
    final = "\n".join(lines).strip()
    return final[:495] # Double sécurité pour Threads

def get_token_status():
    status_msg = "📊 **ÉTAT DES ACCÈS**\n"
    tokens = {"IG/FB": os.environ.get('IG_ACCESS_TOKEN'), "Threads": os.environ.get('THREADS_ACCESS_TOKEN')}
    for name, token in tokens.items():
        if not token: status_msg += f"❌ {name} : Manquant\n"; continue
        try:
            endpoint = "graph.facebook.com" if name == "IG/FB" else "graph.threads.net"
            r = requests.get(f"https://{endpoint}/debug_token", params={"input_token": token, "access_token": token}, timeout=5).json()
            data = r.get('data', {})
            exp = data.get('expires_at')
            if not exp or exp == 0: status_msg += f"✅ {name} : Permanent\n"
            else:
                days = (datetime.datetime.fromtimestamp(exp) - datetime.datetime.now()).days
                status_msg += f"⏳ {name} : {days} jours\n"
        except: status_msg += f"⚠️ {name} : Vérif impossible\n"
    return status_msg

def send_galerie_menu(chat_id):
    config = load_config()
    token = os.environ.get('TELEGRAM_TOKEN')
    status = get_token_status()
    conn = get_db_connection()
    sent_urls = [row[0] for row in conn.execute('SELECT url FROM sent_photos').fetchall()]
    conn.close()
    keyboard = [[{"text": "📈 Voir les Statistiques", "callback_data": "view_stats"}]]
    buttons = []
    for g in config.get('galeries', []):
        url = f"{config.get('site_url')}/{g}"
        try:
            soup = BeautifulSoup(requests.get(url, timeout=10).text, 'html.parser')
            imgs = [img.get('src') for img in soup.find_all('img') if img.get('src')]
            valid = [s if s.startswith('http') else f"{config.get('site_url')}{s}" for s in imgs]
            # Restauration du compteur x/x
            count = f"{len([u for u in valid if u in sent_urls])}/{len(valid)}"
            buttons.append({"text": f"{g.capitalize()} {count}", "callback_data": f"select_{g}"})
        except: buttons.append({"text": g.capitalize(), "callback_data": f"select_{g}"})
    for i in range(0, len(buttons), 2): keyboard.append(buttons[i:i + 2])
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": f"{status}\n---\nQuelle galerie ?", "reply_markup": {"inline_keyboard": keyboard}})

@app.route("/telegram-webhook", methods=['POST'])
def telegram_webhook():
    data = request.json
    token = os.environ.get('TELEGRAM_TOKEN')
    if not data: return jsonify({"status": "ok"})
    if "message" in data:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"]["text"]
        session = get_session(chat_id)
        if session and session[1] == "WAITING_FOR_MANUAL":
            save_session(chat_id, session[0], text)
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "✅ Légende reçue !", "reply_markup": {"inline_keyboard": [[{"text": "📸 Instagram", "callback_data": "pub_ig"}, {"text": "🧵 Threads", "callback_data": "pub_th"}], [{"text": "📍 Lieu", "callback_data": "add_location"}, {"text": "📅 Heure", "callback_data": "schedule_post"}]]}})
        elif session and session[1] == "WAITING_FOR_LOCATION":
            new_cap = f"📍 {text}\n\n{session[1]}"
            save_session(chat_id, session[0], new_cap)
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "✅ Lieu ajouté.", "reply_markup": {"inline_keyboard": [[{"text": "📸 Instagram", "callback_data": "pub_ig"}, {"text": "🧵 Threads", "callback_data": "pub_th"}]]}})
        else: send_galerie_menu(chat_id)
    elif "callback_query" in data:
        chat_id = data["callback_query"]["message"]["chat"]["id"]
        action = data["callback_query"]["data"]
        session = get_session(chat_id)
        if action == "view_stats": requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": get_latest_insights()})
        elif action.startswith("select_"): send_suggestion(chat_id, action.split("_")[1])
        elif action == "pub_ig" and session:
            ok, res = publish_to_instagram(session[0], session[1])
            mark_photo_as_sent(session[0])
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": f"📸 Insta : {'✅' if ok else '❌ ' + str(res)}"})
        elif action == "pub_th" and session:
            ok, res = publish_to_threads(session[0], session[1])
            mark_photo_as_sent(session[0])
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": f"🧵 Threads : {'✅' if ok else '❌ ' + str(res)}"})
        elif action == "manual_edit":
            save_session(chat_id, session[0], "WAITING_FOR_MANUAL")
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "✍️ Envoie ton texte."})
        elif action == "add_location":
            save_session(chat_id, session[0], "WAITING_FOR_LOCATION")
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "📍 Lieu ?"})
    return jsonify({"status": "ok"})

def send_suggestion(chat_id, galerie_nom):
    token = os.environ.get('TELEGRAM_TOKEN')
    config = load_config()
    soup = BeautifulSoup(requests.get(f"{config.get('site_url')}/{galerie_nom}").text, 'html.parser')
    valid = [s if s.startswith('http') else f"{config.get('site_url')}{s}" for s in [img.get('src') for img in soup.find_all('img') if img.get('src')]]
    conn = get_db_connection(); sent = [row[0] for row in conn.execute('SELECT url FROM sent_photos').fetchall()]; conn.close()
    avail = [u for u in valid if u not in sent]
    if not avail: requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "Galerie terminée."}); return
    img_url = random.choice(avail); cap = generate_ai_caption(img_url, galerie_nom); save_session(chat_id, img_url, cap)
    requests.post(f"https://api.telegram.org/bot{token}/sendPhoto", json={"chat_id": chat_id, "photo": img_url, "caption": cap, "reply_markup": {"inline_keyboard": [[{"text": "📸 Instagram", "callback_data": "pub_ig"}, {"text": "🧵 Threads", "callback_data": "pub_th"}],[{"text": "📍 Lieu", "callback_data": "add_location"}, {"text": "✍️ Manuel", "callback_data": "manual_edit"}],[{"text": "🔄 Autre", "callback_data": f"select_{galerie_nom}"}, {"text": "⬅️ Menu", "callback_data": "menu"}]]}})

def get_session(chat_id):
    conn = get_db_connection(); res = conn.execute('SELECT last_url, last_caption FROM current_session WHERE chat_id = ?', (chat_id,)).fetchone(); conn.close()
    return res

def save_session(chat_id, url, cap):
    conn = get_db_connection(); conn.execute('INSERT OR REPLACE INTO current_session VALUES (?, ?, ?)', (chat_id, url, cap)); conn.commit(); conn.close()

def mark_photo_as_sent(url):
    conn = get_db_connection(); conn.execute('INSERT OR IGNORE INTO sent_photos VALUES (?)', (url,)); conn.commit(); conn.close()

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))