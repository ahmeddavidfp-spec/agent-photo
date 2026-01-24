import os, requests, yaml, random, sqlite3, time, datetime, csv
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from openai import OpenAI

# =================================================================
# SECTION 1 : CONFIGURATION ET INITIALISATION
# =================================================================
app = Flask(__name__)
DB_PATH = '/data/photos.db' if os.path.exists('/data') else 'photos.db'

def get_db_connection():
    """Établit la connexion à la base SQLite locale."""
    return sqlite3.connect(DB_PATH)

def init_db():
    conn = get_db_connection()
    conn.execute('CREATE TABLE IF NOT EXISTS sent_photos (url TEXT PRIMARY KEY)')
    conn.execute('CREATE TABLE IF NOT EXISTS current_session (chat_id INTEGER PRIMARY KEY, last_url TEXT, last_caption TEXT)')
    cursor = conn.execute('PRAGMA table_info(sent_photos)')
    existing_columns = [column[1] for column in cursor.fetchall()]
    if 'galerie' not in existing_columns:
        conn.execute('ALTER TABLE sent_photos ADD COLUMN galerie TEXT')
    if 'date_envoi' not in existing_columns:
        conn.execute('ALTER TABLE sent_photos ADD COLUMN date_envoi TEXT')
    conn.commit()
    conn.close()

def load_config():
    try:
        with open("config.yaml", "r") as f: return yaml.safe_load(f)
    except: return {"site_url": "https://www.davidahmed.me", "galeries": ["barcelone"]}

init_db()

# =================================================================
# SECTION 2 : MOTEUR D'INTELLIGENCE ARTIFICIELLE (RÉTABLIE)
# =================================================================
def generate_ai_caption(image_url, galerie_nom):
    """Analyse l'image et génère une légende structurée (Hook SEO + Technique)."""
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    config = load_config()
    
    base_url = config.get('site_url', 'davidahmed.me').replace('https://', '').replace('http://', '').rstrip('/')
    display_link = f"{base_url}/{galerie_nom}"
    
    manual_hashtag = config.get('custom_hashtag', '')
    extra_tag = f"+ {manual_hashtag}" if manual_hashtag else ""
    
    instructions = f"""Tu es David Ahmed, photographe d'art. Analyse cette photo de {galerie_nom}.
    
    STRATÉGIE : 1. Crochet (Hook) percutant. 2. Vocabulaire riche. 3. 2-3 phrases claires.
    
    STRUCTURE STRICTE :
    - Ligne 1 : Titre percutant.
    - Ligne 2 : Analyse (2-3 phrases).
    - (Saut de ligne)
    - Ligne : Voir la galerie : {display_link}
    - (Saut de ligne)
    - Hashtags {extra_tag}.
    
    STRICT : 
    - PAS de gras (**), PAS de ###, PAS de majuscules intégrales. 
    - Ne transforme PAS le lien en format Markdown [texte](url). Affiche-le tel quel.
    - Max 480 caractères au total."""
    
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": [{"type": "text", "text": instructions}, {"type": "image_url", "image_url": {"url": image_url, "detail": "high"}}]}],
        max_tokens=500, temperature=0.7
    )
    
    raw = response.choices[0].message.content
    clean = raw.replace("**", "").replace("__", "").replace("### ", "").replace("## ", "").replace("# ", "")
    lines = clean.split('\n')
    if lines:
        lines[0] = lines[0].strip().capitalize()
    
    return "\n".join(lines).strip()[:495]

# =================================================================
# SECTION 3 : LOGIQUE DES RÉSEAUX SOCIAUX
# =================================================================
def publish_to_instagram(image_url, caption):
    token = os.environ.get('IG_ACCESS_TOKEN')
    ig_id = "17841453263147553" 
    try:
        r = requests.post(f"https://graph.facebook.com/v21.0/{ig_id}/media", data={'image_url': image_url, 'caption': caption, 'access_token': token})
        c_id = r.json().get('id')
        if not c_id: return False, r.json()
        time.sleep(10)
        requests.post(f"https://graph.facebook.com/v21.0/{ig_id}/media_publish", data={'creation_id': c_id, 'access_token': token})
        return True, "OK"
    except Exception as e: return False, str(e)

def publish_to_threads(image_url, caption):
    token = os.environ.get('THREADS_ACCESS_TOKEN')
    th_id = os.environ.get('THREADS_USER_ID')
    clean_url = image_url.split('?')[0] 
    try:
        r = requests.post(f"https://graph.threads.net/v1.0/{th_id}/threads", data={'media_type': 'IMAGE', 'image_url': clean_url, 'text': caption[:495], 'access_token': token})
        res = r.json()
        if 'id' not in res: return False, res
        time.sleep(15)
        r_pub = requests.post(f"https://graph.threads.net/v1.0/{th_id}/threads_publish", data={'creation_id': res['id'], 'access_token': token})
        return (True, "OK") if r_pub.status_code == 200 else (False, r_pub.text)
    except Exception as e: return False, str(e)

# =================================================================
# SECTION 4 : GESTION DE LA BASE DE DONNÉES
# =================================================================
def mark_photo_as_sent(url, galerie):
    date_jour = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db_connection()
    conn.execute('INSERT OR IGNORE INTO sent_photos (url, galerie, date_envoi) VALUES (?, ?, ?)', (url, galerie, date_jour))
    conn.commit()
    conn.close()

def get_token_status():
    status_msg = "📊 **ÉTAT DES ACCÈS**\n"
    for label, env_name, url in [("IG/FB", "IG_ACCESS_TOKEN", "https://graph.facebook.com/debug_token"), ("Threads", "THREADS_ACCESS_TOKEN", "https://graph.threads.net/debug_token")]:
        tk = os.environ.get(env_name)
        if tk:
            try:
                r = requests.get(url, params={"input_token": tk, "access_token": tk}, timeout=5).json()
                exp = r.get('data', {}).get('expires_at')
                if not exp: status_msg += f"✅ {label} : Permanent\n"
                else:
                    days = (datetime.datetime.fromtimestamp(exp) - datetime.datetime.now()).days
                    status_msg += f"⏳ {label} : {days} jours\n"
            except: status_msg += f"⚠️ {label} : Vérif impossible\n"
        else: status_msg += f"❌ {label} : Manquant\n"
    return status_msg

def export_db_to_csv():
    conn = get_db_connection()
    cursor = conn.execute('SELECT * FROM sent_photos')
    file_path = '/tmp/export.csv'
    with open(file_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['url', 'galerie', 'date_envoi'])
        writer.writerows(cursor.fetchall())
    conn.close()
    return file_path

def renew_threads_token():
    client_secret = os.environ.get('THREADS_CLIENT_SECRET')
    current_token = os.environ.get('THREADS_ACCESS_TOKEN')
    if not client_secret: return False, "SECRET manquant"
    try:
        r = requests.get("https://graph.threads.net/access_token", params={"grant_type": "th_exchange_token", "client_secret": client_secret, "access_token": current_token})
        res = r.json()
        if "access_token" in res:
            return True, (res['access_token'], res.get('expires_in', 0) // 86400)
        return False, res
    except Exception as e: return False, str(e)

def get_db_stats():
    conn = get_db_connection()
    stats = conn.execute('SELECT galerie, COUNT(*) FROM sent_photos GROUP BY galerie').fetchall()
    conn.close()
    if not stats: return "Base vide."
    msg = "📁 **RÉSUMÉ DES PUBLICATIONS :**\n"
    for s in stats:
        name = s[0].capitalize() if s[0] else "Inconnue"
        msg += f"- {name} : {s[1]} photos\n"
    return msg

# =================================================================
# SECTION 5 : INTERFACE TELEGRAM
# =================================================================
@app.route("/telegram-webhook", methods=['POST'])
def telegram_webhook():
    data = request.json
    token = os.environ.get('TELEGRAM_TOKEN')
    if not data: return jsonify({"status": "ok"})
    chat_id = data.get("message", {}).get("chat", {}).get("id") or data.get("callback_query", {}).get("message", {}).get("chat", {}).get("id")
    text = data.get("message", {}).get("text", "").strip()
    action = data.get("callback_query", {}).get("data", "")

    # 1. PRIORITÉ ABSOLUE : LES COMMANDES TEXTE
    if text:
        if text == "/renew_threads":
            success, result = renew_threads_token()
            msg = f"✅ **TOKEN RENOUVELÉ ({result[1]}j)**\n`{result[0]}`" if success else f"❌ {result}"
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"})
            return jsonify({"status": "ok"})

        elif text == "/debug_db":
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": get_db_stats(), "parse_mode": "Markdown"})
            return jsonify({"status": "ok"})

    # 2. TRAITEMENT DES BOUTONS (CALLBACKS)
    if action:
        if action == "renew_threads_btn":
            success, result = renew_threads_token()
            msg = f"✅ **TOKEN RENOUVELÉ ({result[1]}j)**\n`{result[0]}`" if success else f"❌ {result}"
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"})
            return jsonify({"status": "ok"})
        
        elif action == "view_stats":
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": get_db_stats(), "parse_mode": "Markdown"})
        
        elif action == "export_db_btn":
            path = export_db_to_csv()
            with open(path, 'rb') as f: requests.post(f"https://api.telegram.org/bot{token}/sendDocument", data={"chat_id": chat_id}, files={"document": f})
        
        elif action == "menu": 
            send_galerie_menu(chat_id)
            
        elif action.startswith("select_"): 
            send_suggestion(chat_id, action.split("_")[1])
            
        else:
            session = get_session(chat_id)
            if session:
                if action == "pub_both":
                    ok_ig, res_ig = publish_to_instagram(session[0], session[1])
                    ok_th, res_th = publish_to_threads(session[0], session[1])
                    if ok_ig and ok_th:
                        mark_photo_as_sent(session[0], "Auto")
                        conn = get_db_connection()
                        conn.execute('DELETE FROM current_session WHERE chat_id = ?', (chat_id,))
                        conn.commit()
                        conn.close()
                        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "🚀 Insta & Threads : ✅"})
                    else:
                        msg = f"⚠️ Résultat partiel :\nIG: {'✅' if ok_ig else '❌ ' + str(res_ig)}\nTH: {'✅' if ok_th else '❌ ' + str(res_th)}"
                        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": msg})
                
                elif action == "pub_ig":
                    ok, res = publish_to_instagram(session[0], session[1])
                    if ok:
                        mark_photo_as_sent(session[0], "Auto")
                        conn = get_db_connection()
                        conn.execute('DELETE FROM current_session WHERE chat_id = ?', (chat_id,))
                        conn.commit()
                        conn.close()
                        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "📸 Insta : ✅"})

                elif action == "pub_th":
                    ok, res = publish_to_threads(session[0], session[1])
                    if ok:
                        mark_photo_as_sent(session[0], "Auto")
                        conn = get_db_connection()
                        conn.execute('DELETE FROM current_session WHERE chat_id = ?', (chat_id,))
                        conn.commit()
                        conn.close()
                        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "🧵 Threads : ✅"})

                elif action == "manual_edit":
                    save_session(chat_id, session[0], "WAITING_FOR_MANUAL")
                    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "✍️ Envoie ton texte."})
        
        return jsonify({"status": "ok"})

    # 3. GESTION DES ÉTATS DE SESSION (Texte classique)
    if text:
        session = get_session(chat_id)
        if session and session[1] == "WAITING_FOR_MANUAL":
            save_session(chat_id, session[0], text)
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "✅ Prêt !", "reply_markup": {"inline_keyboard": [[{"text": "🚀 Les deux", "callback_data": "pub_both"}], [{"text": "📸 Insta", "callback_data": "pub_ig"}, {"text": "🧵 Threads", "callback_data": "pub_th"}]]}})
        else:
            send_galerie_menu(chat_id)
                
    return jsonify({"status": "ok"})

# =================================================================
# SECTION 6 : FONCTIONS AUXILIAIRES
# =================================================================
def send_galerie_menu(chat_id):
    config = load_config()
    token = os.environ.get('TELEGRAM_TOKEN')
    status = get_token_status()
    
    conn = get_db_connection()
    sent_urls = [row[0] for row in conn.execute('SELECT url FROM sent_photos').fetchall()]
    conn.close()

    keyboard = [[{"text": "📈 Stats", "callback_data": "view_stats"}, 
                 {"text": "📥 Export", "callback_data": "export_db_btn"}, 
                 {"text": "🔄 Renew", "callback_data": "renew_threads_btn"}]]
    
    buttons = []
    for g in config.get('galeries', []):
        try:
            soup = BeautifulSoup(requests.get(f"{config.get('site_url')}/{g}", timeout=10).text, 'html.parser')
            imgs = [img.get('src') for img in soup.find_all('img') if img.get('src')]
            valid = [s if s.startswith('http') else f"{config.get('site_url')}{s}" for s in imgs]
            count = f"{len([u for u in valid if u in sent_urls])}/{len(valid)}"
            buttons.append({"text": f"{g.capitalize()} {count}", "callback_data": f"select_{g}"})
        except: 
            buttons.append({"text": g.capitalize(), "callback_data": f"select_{g}"})
            
    for i in range(0, len(buttons), 2): 
        keyboard.append(buttons[i:i + 2])
        
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                  json={"chat_id": chat_id, "text": f"{status}\n---\nQuelle galerie ?", "reply_markup": {"inline_keyboard": keyboard}})

def send_suggestion(chat_id, galerie_nom):
    token = os.environ.get('TELEGRAM_TOKEN')
    config = load_config()
    soup = BeautifulSoup(requests.get(f"{config.get('site_url')}/{galerie_nom}").text, 'html.parser')
    valid = [s if s.startswith('http') else f"{config.get('site_url')}{s}" for s in [img.get('src') for img in soup.find_all('img') if img.get('src')]]
    
    conn = get_db_connection()
    sent = [row[0] for row in conn.execute('SELECT url FROM sent_photos').fetchall()]
    conn.close()
    
    avail = [u for u in valid if u not in sent]
    if not avail:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "Galerie vide."})
        return

    img_url = random.choice(avail)
    cap = generate_ai_caption(img_url, galerie_nom)
    save_session(chat_id, img_url, cap)

    kb = [
        [{"text": "🚀 Publier sur les deux", "callback_data": "pub_both"}],
        [{"text": "📸 Insta", "callback_data": "pub_ig"}, {"text": "🧵 Threads", "callback_data": "pub_th"}],
        [{"text": "🔄 Autre", "callback_data": f"select_{galerie_nom}"}, {"text": "⬅️ Menu", "callback_data": "menu"}]
    ]
    requests.post(f"https://api.telegram.org/bot{token}/sendPhoto", 
                  json={"chat_id": chat_id, "photo": img_url, "caption": cap, "reply_markup": {"inline_keyboard": kb}})

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

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))