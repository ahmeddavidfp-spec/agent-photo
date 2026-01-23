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

# --- PUBLICATIONS (SANS FACEBOOK) ---

def publish_to_instagram(image_url, caption):
    token = os.environ.get('IG_ACCESS_TOKEN')
    ig_id = "17841453263147553" 
    try:
        r = requests.post(f"https://graph.facebook.com/v21.0/{ig_id}/media", 
                          data={'image_url': image_url, 'caption': caption, 'access_token': token})
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
    try:
        url = f"https://graph.threads.net/v1.0/{th_id}/threads"
        r = requests.post(url, data={'media_type': 'IMAGE', 'image_url': clean_url, 'text': caption, 'access_token': token}, timeout=30)
        res = r.json()
        if 'id' not in res: return False, res
        time.sleep(15) 
        pub_url = f"https://graph.threads.net/v1.0/{th_id}/threads_publish"
        r_pub = requests.post(pub_url, data={'creation_id': res['id'], 'access_token': token}, timeout=30)
        return (True, "OK") if r_pub.status_code == 200 else (False, r_pub.text)
    except Exception as e: return False, str(e)

# --- IA STYLE ÉPURÉ ---

# --- IA STYLE AFFINÉ ---

def generate_ai_caption(image_url, galerie_nom):
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    config = load_config()
    galerie_link = f"{config.get('site_url').rstrip('/')}/{galerie_nom}"
    
    # Instructions précises pour le formatage David
    instructions = f"""Tu es David Ahmed, photographe. Rédige une légende élégante. 
    FORMAT :
    - Ligne 1 : Un titre descriptif (Casse normale, PAS tout en majuscules).
    - Ligne 2 : Une courte description poétique.
    - Ligne 4 : La phrase "Série complète sur {galerie_link}"
    - Ligne 6 : 5 hashtags pertinents.
    
    STRICT : PAS de chiffres, PAS d'astérisques, PAS de gras. 
    Saute bien une ligne avant la série complète et avant les hashtags."""
    
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": [{"type": "text", "text": instructions}, {"type": "image_url", "image_url": {"url": image_url}}]}],
        max_tokens=400, temperature=0.7
    )
    return response.choices[0].message.content
# --- MENU DEUX COLONNES AVEC COMPTEUR ---

def send_galerie_menu(chat_id):
    config = load_config()
    token = os.environ.get('TELEGRAM_TOKEN')
    buttons = []
    
    conn = get_db_connection()
    sent_urls = [row[0] for row in conn.execute('SELECT url FROM sent_photos').fetchall()]
    conn.close()

    for g in config.get('galeries', []):
        url = f"{config.get('site_url')}/{g}"
        try:
            soup = BeautifulSoup(requests.get(url, timeout=10).text, 'html.parser')
            images = [img.get('src') for img in soup.find_all('img') if img.get('src')]
            valid_photos = [src if src.startswith('http') else f"{config.get('site_url')}{src}" for src in images]
            
            total = len(valid_photos)
            publiees = len([u for u in valid_photos if u in sent_urls])
            btn_text = f"{g.capitalize()} {publiees}/{total}"
            buttons.append({"text": btn_text, "callback_data": f"select_{g}"})
        except:
            buttons.append({"text": g.capitalize(), "callback_data": f"select_{g}"})

    # Organisation en 2 colonnes
    keyboard = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]

    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                  json={"chat_id": chat_id, "text": "Quelle galerie voulez-vous explorer ?", "reply_markup": {"inline_keyboard": keyboard}})

# --- WEBHOOK & SESSIONS ---

@app.route("/telegram-webhook", methods=['POST'])
def telegram_webhook():
    data = request.json
    token = os.environ.get('TELEGRAM_TOKEN')
    if not data: return jsonify({"status": "no data"})

    if "message" in data:
        send_galerie_menu(data["message"]["chat"]["id"])
    elif "callback_query" in data:
        chat_id = data["callback_query"]["message"]["chat"]["id"]
        action = data["callback_query"]["data"]
        session = get_session(chat_id)
        
        if action == "menu": 
            send_galerie_menu(chat_id)
        elif action.startswith("select_"): 
            send_suggestion(chat_id, action.split("_")[1])
        elif action == "publish_all" and session:
            url, cap = session
            mark_photo_as_sent(url)
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "⏳ Publication double lancée (Insta + Threads)..."})
            
            ok_ig, err_ig = publish_to_instagram(url, cap)
            ok_th, err_th = publish_to_threads(url, cap)
            
            msg = f"📸 Instagram : {'✅' if ok_ig else '❌'}\n🧵 Threads : {'✅' if ok_th else '❌'}"
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": msg})
            
    return jsonify({"status": "ok"})

def send_suggestion(chat_id, galerie_nom):
    token = os.environ.get('TELEGRAM_TOKEN')
    config = load_config()
    url = f"{config.get('site_url')}/{galerie_nom}"
    try:
        soup = BeautifulSoup(requests.get(url, timeout=10).text, 'html.parser')
        images = [img.get('src') for img in soup.find_all('img') if img.get('src')]
        valid_photos = [src if src.startswith('http') else f"{config.get('site_url')}{src}" for src in images]
        
        conn = get_db_connection()
        sent = [row[0] for row in conn.execute('SELECT url FROM sent_photos').fetchall()]
        conn.close()
        available = [u for u in valid_photos if u not in sent]
        
        if not available:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "Galerie terminée."})
            return
        
        img_url = random.choice(available)
        cap = generate_ai_caption(img_url, galerie_nom)
        save_session(chat_id, img_url, cap)
        
        requests.post(f"https://api.telegram.org/bot{token}/sendPhoto", json={
            "chat_id": chat_id, "photo": img_url, "caption": cap, 
            "reply_markup": {"inline_keyboard": [[{"text": "🚀 Publier", "callback_data": "publish_all"}],
                                                [{"text": "🔄 Autre", "callback_data": f"select_{galerie_nom}"}, {"text": "⬅️ Menu", "callback_data": "menu"}]]}
        })
    except Exception as e: requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": f"Erreur: {e}"})

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

@app.route("/")
def index(): return "David Ahmed Agent (IG/Threads) - OK"

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))