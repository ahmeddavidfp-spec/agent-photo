import os, requests, yaml, random, sqlite3, time, datetime, csv, threading, re, logging, sys
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from openai import OpenAI

# CONFIGURATION DES LOGS
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', stream=sys.stdout)
logger = logging.getLogger()

app = Flask(__name__)
DB_PATH = '/data/photos.db' if os.path.exists('/data') else 'photos.db'

# CONSTANTES URL (Safe)
TG_API = "https://" + "api.telegram.org/bot"
FB_API = "https://" + "graph.facebook.com/v21.0/"
TH_API = "https://" + "graph.threads.net/v1.0/"

def get_db_connection(): 
    return sqlite3.connect(DB_PATH)

def init_db():
    conn = get_db_connection()
    conn.execute('CREATE TABLE IF NOT EXISTS sent_photos (url TEXT PRIMARY KEY)')
    conn.execute('CREATE TABLE IF NOT EXISTS current_session (chat_id INTEGER PRIMARY KEY, last_url TEXT, last_caption TEXT)')
    conn.execute('''CREATE TABLE IF NOT EXISTS scheduled_posts 
                    (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, image_url TEXT, caption TEXT, run_at TEXT, status TEXT DEFAULT 'pending')''')
    cursor = conn.execute('PRAGMA table_info(sent_photos)')
    cols = [column[1] for column in cursor.fetchall()]
    if 'galerie' not in cols: conn.execute('ALTER TABLE sent_photos ADD COLUMN galerie TEXT')
    if 'date_envoi' not in cols: conn.execute('ALTER TABLE sent_photos ADD COLUMN date_envoi TEXT')
    conn.commit()
    conn.close()

def load_config():
    try:
        with open("config.yaml", "r") as f: return yaml.safe_load(f)
    except: return {"site_url": "https://www.davidahmed.me", "galeries": ["barcelone"]}

init_db()

# --- OUTILS ---
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

def mark_photo_as_sent(url, galerie):
    conn = get_db_connection()
    conn.execute('INSERT OR IGNORE INTO sent_photos (url, galerie, date_envoi) VALUES (?, ?, ?)', (url, galerie, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

def get_db_stats():
    conn = get_db_connection()
    stats = conn.execute('SELECT galerie, COUNT(*) FROM sent_photos GROUP BY galerie').fetchall()
    conn.close()
    if not stats: return "Base vide."
    msg = "📁 **RESUME :**\n"
    for s in stats: msg += f"- {s[0].capitalize() if s[0] else 'Inconnue'} : {s[1]}\n"
    return msg

def export_db_to_csv():
    conn = get_db_connection()
    cur = conn.execute('SELECT * FROM sent_photos')
    path = '/tmp/export.csv'
    with open(path, 'w', newline='') as f:
        csv.writer(f).writerow(['url', 'galerie', 'date'])
        csv.writer(f).writerows(cur.fetchall())
    conn.close()
    return path

def renew_threads_token():
    try:
        url = "https://" + "graph.threads.net/access_token"
        r = requests.get(url, params={"grant_type": "th_exchange_token", "client_secret": os.environ.get('THREADS_CLIENT_SECRET'), "access_token": os.environ.get('THREADS_ACCESS_TOKEN')})
        res = r.json()
        if "access_token" in res: return True, (res['access_token'], res.get('expires_in', 0) // 86400)
        return False, res
    except Exception as e: return False, str(e)

def get_token_status():
    msg = "📊 **ETAT**\n"
    fb_debug = "https://" + "graph.facebook.com/debug_token"
    th_debug = "https://" + "graph.threads.net/debug_token"
    for lbl, env, url in [("IG", "IG_ACCESS_TOKEN", fb_debug), ("TH", "THREADS_ACCESS_TOKEN", th_debug)]:
        tk = os.environ.get(env)
        if tk:
            try:
                exp = requests.get(url, params={"input_token": tk, "access_token": tk}, timeout=5).json().get('data', {}).get('expires_at')
                msg += f"✅ {lbl} : {((datetime.datetime.fromtimestamp(exp)-datetime.datetime.now()).days) if exp else 'OK'}\n"
            except: msg += f"⚠️ {lbl} : Erreur\n"
        else: msg += f"❌ {lbl} : Manquant\n"
    return msg

# --- IA ---
def generate_ai_caption(image_url, galerie_nom):
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    cfg = load_config()
    link = f"{cfg.get('site_url', '').replace('https://', '').rstrip('/')}/{galerie_nom}"
    
    instr = f"""Tu es David Ahmed, photographe. Analyse cette photo de {galerie_nom}.
    Output: Titre, 2 phrases analyse, question, (cc mentions), {link}, hashtags.
    Separe la description visuelle par |||."""
    
    try:
        res = client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": [{"type": "text", "text": instr}, {"type": "image_url", "image_url": {"url": image_url}}]}], max_tokens=500)
        raw = res.choices[0].message.content.replace("```", "")
        return raw if "|||" in raw else f"{raw}|||Photo de {galerie_nom}"
    except: return f"Photo de {galerie_nom}\n\n{link}|||Art photography"

# --- RESEAUX ---
def split_content(txt):
    return (txt.split("|||")[0].strip(), txt.split("|||")[1].strip()) if "|||" in txt else (txt, "Art photo")

def final_security_check(text):
    return text

def publish_to_instagram(img, txt):
    # Instagram continue de recevoir l'image physique
    tk = os.environ.get('IG_ACCESS_TOKEN')
    id = "17841453263147553"
    cap, _ = split_content(txt)
    try:
        c_id = requests.post(f"{FB_API}{id}/media", data={'image_url': img, 'caption': cap, 'access_token': tk}).json().get('id')
        if not c_id: return False, "Err Crea"
        time.sleep(8)
        requests.post(f"{FB_API}{id}/media_publish", data={'creation_id': c_id, 'access_token': tk})
        return True, "OK"
    except Exception as e: return False, str(e)

def publish_to_threads(image_url, full_text):
    """
    MODIFICATION: Publie uniquement du TEXTE contenant le LIEN de l'image.
    Cela contourne les erreurs d'upload d'image (Code 1/Geo-blocking).
    Threads generera un apercu de l'image via le lien.
    """
    tk = os.environ.get('THREADS_ACCESS_TOKEN')
    id = os.environ.get('THREADS_USER_ID')
    caption, _ = split_content(full_text)
    
    if not id or not tk: return False, "Token manquant"

    # Optimisation lien Squarespace pour bel apercu
    if "squarespace" in image_url: 
        clean_url = image_url.split('?')[0] + "?format=1000w"
    else: 
        clean_url = image_url

    logger.info(f"🧵 Mode Lien Uniquement | URL: {clean_url}")

    try:
        # 1. Creation du conteneur TEXTE (avec l'URL dedans)
        # On ajoute 2 sauts de ligne avant le lien pour que ce soit propre
        final_content = f"{caption}\n\n{clean_url}"
        
        url = f"{TH_API}{id}/threads"
        payload = {
            'media_type': 'TEXT',
            'text': final_content,
            'access_token': tk
        }
        
        r = requests.post(url, data=payload)
        res = r.json()
        
        if 'id' not in res:
            logger.error(f"❌ Erreur Creation Threads: {res}")
            return False, res
        
        creation_id = res['id']
        
        # 2. Publication Immediate (Pas de polling necessaire pour du texte)
        url_pub = f"{TH_API}{id}/threads_publish"
        r_pub = requests.post(url_pub, data={'creation_id': creation_id, 'access_token': tk})
        
        if 'id' in r_pub.json():
            return True, "OK (Mode Lien)"
        else:
            return False, r_pub.json()

    except Exception as e:
        logger.error(f"🔥 Crash Threads: {e}")
        return False, str(e)

# --- BACKEND ---
def background_publish(chat_id, token, mode, img, cap):
    logger.info(f"🚀 Tache de fond demarree pour {chat_id}")
    ok_ig = ok_th = False
    
    if mode in ["both", "ig"]: ok_ig, _ = publish_to_instagram(img, cap)
    if mode in ["both", "th"]: ok_th, _ = publish_to_threads(img, cap)
    
    msg = "🚀 **Resultat :**\n"
    msg += f"IG: {'✅' if ok_ig else '❌'}\nTH: {'✅' if ok_th else '❌'}"
    
    if ok_ig or ok_th:
        mark_photo_as_sent(img, "Auto")
        conn = get_db_connection()
        conn.execute('DELETE FROM current_session WHERE chat_id=?', (chat_id,))
        conn.commit()
        conn.close()
        
    requests.post(f"{TG_API}{token}/sendMessage", json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"})

# --- ROUTES ---
@app.route("/telegram-webhook", methods=['POST'])
def webhook():
    data = request.json
    if not data: return jsonify({"status": "ok"})
    tk = os.environ.get('TELEGRAM_TOKEN')
    
    msg = data.get("message", {})
    cb = data.get("callback_query", {})
    chat_id = msg.get("chat", {}).get("id") or cb.get("message", {}).get("chat", {}).get("id")
    txt = msg.get("text", "").strip()
    act = cb.get("data", "")
    
    if txt: logger.info(f"Texte recu: {txt}")
    if act: logger.info(f"Action recue: {act}")
    
    if txt == "/start" or act == "menu":
        send_galerie_menu(chat_id, tk)
    elif act.startswith("select_"):
        send_suggestion(chat_id, tk, act.split("_")[1])
    elif act in ["pub_both", "pub_ig", "pub_th"]:
        sess = get_session(chat_id)
        if sess:
            requests.post(f"{TG_API}{tk}/sendMessage", json={"chat_id": chat_id, "text": "⏳ Envoi en cours..."})
            mode = "both" if act == "pub_both" else ("ig" if act == "pub_ig" else "th")
            threading.Thread(target=background_publish, args=(chat_id, tk, mode, sess[0], sess[1])).start()
    
    return jsonify({"status": "ok"})

def send_galerie_menu(cid, tk):
    cfg = load_config()
    kb = []
    row = []
    for g in cfg.get('galeries', []):
        row.append({"text": g.capitalize(), "callback_data": f"select_{g}"})
        if len(row) == 2: 
            kb.append(row)
            row = []
    if row: kb.append(row)
    requests.post(f"{TG_API}{tk}/sendMessage", json={"chat_id": cid, "text": "Quelle galerie ?", "reply_markup": {"inline_keyboard": kb}})

def send_suggestion(cid, tk, g):
    cfg = load_config()
    try:
        soup = BeautifulSoup(requests.get(f"{cfg.get('site_url')}/{g}").text, 'html.parser')
        imgs = [i.get('src') for i in soup.find_all('img') if i.get('src')]
        valid = [u if u.startswith('http') else f"{cfg.get('site_url')}{u}" for u in imgs]
        conn = get_db_connection()
        sent = [r[0] for r in conn.execute('SELECT url FROM sent_photos').fetchall()]
        conn.close()
        avail = [u for u in valid if u not in sent]
        
        if not avail: 
            requests.post(f"{TG_API}{tk}/sendMessage", json={"chat_id": cid, "text": "Galerie vide !"})
            return

        img = random.choice(avail)
        cap = generate_ai_caption(img, g)
        save_session(cid, img, cap)
        
        kb = [[{"text": "🚀 Les deux", "callback_data": "pub_both"}],
              [{"text": "IG", "callback_data": "pub_ig"}, {"text": "TH", "callback_data": "pub_th"}],
              [{"text": "Autre", "callback_data": f"select_{g}"}, {"text": "Menu", "callback_data": "menu"}]]
              
        requests.post(f"{TG_API}{tk}/sendPhoto", json={"chat_id": cid, "photo": img, "caption": cap.split("|||")[0], "reply_markup": {"inline_keyboard": kb}})
    except Exception as e:
        requests.post(f"{TG_API}{tk}/sendMessage", json={"chat_id": cid, "text": f"Erreur : {str(e)}"})

# Planificateur (Scheduler)
def scheduler_loop():
    while True:
        try:
            conn = get_db_connection()
            rows = conn.execute("SELECT id, chat_id, image_url, caption, run_at FROM scheduled_posts WHERE status='pending'").fetchall()
            now_utc = datetime.datetime.utcnow()
            for row in rows:
                post_id, chat_id, img, full_text, run_at_str = row
                run_at = datetime.datetime.strptime(run_at_str, "%Y-%m-%d %H:%M:%S")
                if now_utc >= run_at:
                    tk = os.environ.get('TELEGRAM_TOKEN')
                    ok_ig, _ = publish_to_instagram(img, full_text)
                    ok_th, _ = publish_to_threads(img, full_text) # Utilise la nvelle fonction texte+lien
                    status = 'sent' if (ok_ig and ok_th) else 'error'
                    conn.execute("UPDATE scheduled_posts SET status = ? WHERE id = ?", (status, post_id))
                    conn.commit()
                    msg = f"⏰ **Post Programme !**\nIG: {'✅' if ok_ig else '❌'}\nTH: {'✅' if ok_th else '❌'}"
                    requests.post(f"{TG_API}{tk}/sendMessage", json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"})
                    if ok_ig or ok_th: mark_photo_as_sent(img, "Programme")
            conn.close()
        except: pass
        time.sleep(20)

threading.Thread(target=scheduler_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))