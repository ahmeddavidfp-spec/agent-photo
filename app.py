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

# --- STATISTIQUES DE PERFORMANCE (INSIGHTS) ---

def get_latest_insights():
    token = os.environ.get('IG_ACCESS_TOKEN')
    ig_id = "17841453263147553"
    try:
        # 1. Récupérer les 3 derniers médias
        r = requests.get(f"https://graph.facebook.com/v21.0/{ig_id}/media", 
                         params={"access_token": token, "limit": 3}).json()
        media_data = r.get('data', [])
        if not media_data: return "Aucune publication trouvée."

        summary = "📈 **TES PERFORMANCES INSTAGRAM**\n\n"
        for m in media_data:
            m_id = m['id']
            # 2. Récupérer les metrics
            metrics = "reach,engagement,saved"
            res = requests.get(f"https://graph.facebook.com/v21.0/{m_id}/insights", 
                               params={"metric": metrics, "access_token": token}).json()
            
            # 3. Récupérer les infos de base (Lien et date)
            m_info = requests.get(f"https://graph.facebook.com/v21.0/{m_id}", 
                                  params={"fields": "permalink,timestamp", "access_token": token}).json()
            
            date = datetime.datetime.strptime(m_info['timestamp'], "%Y-%m-%dT%H:%M:%S%z").strftime("%d/%m")
            stats = {item['name']: item['values'][0]['value'] for item in res.get('data', [])}
            
            summary += f"📅 Post du {date} :\n"
            summary += f"👥 Portée : {stats.get('reach', 0)} | ❤️ Engagement : {stats.get('engagement', 0)}\n"
            summary += f"💾 Enregistrements : {stats.get('saved', 0)}\n"
            summary += f"🔗 {m_info['permalink']}\n\n"
        return summary
    except Exception as e:
        return f"⚠️ Erreur lors de la récupération des stats : {str(e)}"

# --- PUBLICATIONS ---

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

# --- IA ANALYSE EXPERT ---

def generate_ai_caption(image_url, galerie_nom):
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    config = load_config()
    galerie_link = f"{config.get('site_url').rstrip('/')}/{galerie_nom}"
    manual_hashtag = config.get('custom_hashtag', '')
    extra_tag = f"+ {manual_hashtag}" if manual_hashtag else ""
    
    # Nouvelles instructions basées sur tes critères SEO et Clarté
    instructions = f"""Tu es David Ahmed, photographe d'art. Analyse cette photo de {galerie_nom}.
    
    STRATÉGIE DE RÉDACTION :
    1. LE CROCHET (Hook) : La première phrase (le titre) doit être percutante et contenir un mot-clé principal lié à la photographie ou au lieu.
    2. LA VARIÉTÉ : Utilise un vocabulaire riche et des synonymes. Ne répète pas les mêmes termes.
    3. LA CLARTÉ : Évite les phrases banales. Rédige 2 à 3 phrases descriptives et analytiques qui apprennent quelque chose à l'algorithme et à l'audience.
    
    STRUCTURE STRICTE :
    - Ligne 1 : Ton Crochet (Hook).
    - Ligne 2 : Analyse technique et poétique claire (2-3 phrases).
    - (Saut de ligne)
    - Série complète sur : {galerie_link}
    - (Saut de ligne)
    - 5 hashtags variés {extra_tag}.
    
    STRICT : PAS de gras (**), PAS de symboles de titre (###), PAS de texte tout en majuscules."""
    
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": [{"type": "text", "text": instructions}, {"type": "image_url", "image_url": {"url": image_url, "detail": "high"}}]}],
        max_tokens=500, temperature=0.7
    )
    
    # --- NETTOYAGE ET FORMATAGE DE SÉCURITÉ FINAL ---
    raw_caption = response.choices[0].message.content
    
    # 1. Retrait des symboles Markdown (gras, titres)
    clean_caption = raw_caption.replace("**", "").replace("__", "")
    clean_caption = clean_caption.replace("### ", "").replace("## ", "").replace("# ", "")
    
    # 2. Forcer la casse normale sur la première ligne (le titre/hook)
    lines = clean_caption.split('\n')
    if lines:
        # Nettoie les espaces et force : 1ère lettre Majuscule, le reste en minuscules
        lines[0] = lines[0].strip().capitalize()
    
    return "\n".join(lines).strip()


# --- STATUT DES TOKENS ---
def get_token_status():
    status_msg = "📊 **ÉTAT DES ACCÈS**\n"
    tokens = {"IG/FB": os.environ.get('IG_ACCESS_TOKEN'), "Threads": os.environ.get('THREADS_ACCESS_TOKEN')}
    for name, token in tokens.items():
        if not token:
            status_msg += f"❌ {name} : Manquant\n"; continue
        try:
            endpoint = "graph.facebook.com" if name == "IG/FB" else "graph.threads.net"
            r = requests.get(f"https://{endpoint}/debug_token", params={"input_token": token, "access_token": token}, timeout=5).json()
            data = r.get('data', {})
            exp = data.get('data_access_expires_at') or data.get('expires_at')
            if exp == 0 or not exp: status_msg += f"✅ {name} : Permanent\n"
            else:
                days = (datetime.datetime.fromtimestamp(exp) - datetime.datetime.now()).days
                status_msg += f"⏳ {name} : {days} jours restants\n"
        except: status_msg += f"⚠️ {name} : Vérification impossible\n"
    return status_msg

# --- MENUS ---
def send_galerie_menu(chat_id):
    config = load_config()
    token = os.environ.get('TELEGRAM_TOKEN')
    status_table = get_token_status()
    conn = get_db_connection()
    sent_urls = [row[0] for row in conn.execute('SELECT url FROM sent_photos').fetchall()]
    conn.close()
    
    # Bouton STATS en haut
    keyboard = [[{"text": "📈 Voir les Statistiques", "callback_data": "view_stats"}]]
    
    buttons = []
    for g in config.get('galeries', []):
        url = f"{config.get('site_url')}/{g}"
        try:
            soup = BeautifulSoup(requests.get(url, timeout=10).text, 'html.parser')
            images = [img.get('src') for img in soup.find_all('img') if img.get('src')]
            valid = [s if s.startswith('http') else f"{config.get('site_url')}{s}" for s in images]
            buttons.append({"text": f"{g.capitalize()} {len([u for u in valid if u in sent_urls])}/{len(valid)}", "callback_data": f"select_{g}"})
        except: buttons.append({"text": g.capitalize(), "callback_data": f"select_{g}"})
    
    for i in range(0, len(buttons), 2):
        keyboard.append(buttons[i:i + 2])
        
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                  json={"chat_id": chat_id, "text": f"{status_table}\n---\nQuelle galerie ?", "reply_markup": {"inline_keyboard": keyboard}})

# --- WEBHOOK ---
@app.route("/telegram-webhook", methods=['POST'])
def telegram_webhook():
    data = request.json
    token = os.environ.get('TELEGRAM_TOKEN')
    if not data: return jsonify({"status": "ok"})

    if "message" in data and "text" in data["message"]:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"]["text"]
        session = get_session(chat_id)
        if session and session[1] == "WAITING_FOR_MANUAL":
            save_session(chat_id, session[0], text)
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": f"✅ Légende reçue !", "reply_markup": {"inline_keyboard": [[{"text": "🚀 Publier", "callback_data": "publish_all"}], [{"text": "📍 Lieu", "callback_data": "add_location"}, {"text": "📅 Programmer", "callback_data": "schedule_post"}]]}})
        elif session and session[1] == "WAITING_FOR_LOCATION":
            new_cap = f"📍 {text}\n\n{session[1]}"
            save_session(chat_id, session[0], new_cap)
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": f"✅ Lieu ajouté.", "reply_markup": {"inline_keyboard": [[{"text": "🚀 Publier", "callback_data": "publish_all"}]]}})
        elif session and session[1] == "WAITING_FOR_TIME":
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": f"✅ Programmé pour {text}."})
            save_session(chat_id, session[0], "POST_SCHEDULED")
        else: send_galerie_menu(chat_id)

    elif "callback_query" in data:
        chat_id = data["callback_query"]["message"]["chat"]["id"]
        action = data["callback_query"]["data"]
        session = get_session(chat_id)
        
        if action == "view_stats":
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "⏳ Analyse des données Instagram..."})
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": get_latest_insights(), "disable_web_page_preview": True})
        elif action == "menu": send_galerie_menu(chat_id)
        elif action.startswith("select_"): send_suggestion(chat_id, action.split("_")[1])
        elif action == "manual_edit" and session:
            save_session(chat_id, session[0], "WAITING_FOR_MANUAL")
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "✍️ Envoie ton texte complet."})
        elif action == "add_location" and session:
            save_session(chat_id, session[0], "WAITING_FOR_LOCATION")
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "📍 Quel lieu ?"})
        elif action == "schedule_post" and session:
            save_session(chat_id, session[0], "WAITING_FOR_TIME")
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "📅 Quelle heure ?"})
        elif action == "publish_all" and session:
            url, cap = session
            mark_photo_as_sent(url)
            publish_to_instagram(url, cap)
            publish_to_threads(url, cap)
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "✅ Posté sur Insta & Threads !"})
            
    return jsonify({"status": "ok"})

def send_suggestion(chat_id, galerie_nom):
    token = os.environ.get('TELEGRAM_TOKEN')
    config = load_config()
    try:
        soup = BeautifulSoup(requests.get(f"{config.get('site_url')}/{galerie_nom}").text, 'html.parser')
        imgs = [img.get('src') for img in soup.find_all('img') if img.get('src')]
        valid = [u if u.startswith('http') else f"{config.get('site_url')}{u}" for u in imgs]
        conn = get_db_connection(); sent = [row[0] for row in conn.execute('SELECT url FROM sent_photos').fetchall()]; conn.close()
        avail = [u for u in valid if u not in sent]
        if not avail: requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "Galerie terminée."}); return
        img_url = random.choice(avail); cap = generate_ai_caption(img_url, galerie_nom); save_session(chat_id, img_url, cap)
        requests.post(f"https://api.telegram.org/bot{token}/sendPhoto", json={"chat_id": chat_id, "photo": img_url, "caption": cap, "reply_markup": {"inline_keyboard": [[{"text": "🚀 Publier", "callback_data": "publish_all"}],[{"text": "📍 Lieu", "callback_data": "add_location"}, {"text": "📅 Programmer", "callback_data": "schedule_post"}],[{"text": "✍️ Manuel", "callback_data": "manual_edit"}, {"text": "🔄 Autre", "callback_data": f"select_{galerie_nom}"}],[{"text": "⬅️ Menu", "callback_data": "menu"}]]}})
    except: pass

def get_session(chat_id):
    conn = get_db_connection(); res = conn.execute('SELECT last_url, last_caption FROM current_session WHERE chat_id = ?', (chat_id,)).fetchone(); conn.close()
    return res

def save_session(chat_id, url, cap):
    conn = get_db_connection(); conn.execute('INSERT OR REPLACE INTO current_session VALUES (?, ?, ?)', (chat_id, url, cap)); conn.commit(); conn.close()

def mark_photo_as_sent(url):
    conn = get_db_connection(); conn.execute('INSERT OR IGNORE INTO sent_photos VALUES (?)', (url,)); conn.commit(); conn.close()

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))