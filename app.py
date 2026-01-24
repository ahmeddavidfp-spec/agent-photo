import os, requests, yaml, random, sqlite3, time, datetime, csv, threading
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
    
    # Table pour les posts programmés
    conn.execute('''CREATE TABLE IF NOT EXISTS scheduled_posts 
                    (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                     chat_id INTEGER, 
                     image_url TEXT, 
                     caption TEXT, 
                     run_at TEXT, 
                     status TEXT DEFAULT 'pending')''')
    
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
# SECTION 2 : MOTEUR IA (VERSION BLINDÉE : @ OBLIGATOIRES + TITRE PRO)
# =================================================================
def generate_ai_caption(image_url, galerie_nom):
    """Génère la légende avec Mentions (@) forcées et Titre soigné."""
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    config = load_config()
    
    base_url = config.get('site_url', 'davidahmed.me').replace('https://', '').replace('http://', '').rstrip('/')
    display_link = f"{base_url}/{galerie_nom}"
    manual_hashtag = config.get('custom_hashtag', '')
    base_tag = f"#{manual_hashtag}" if manual_hashtag else ""
    
    instructions = f"""Tu es David Ahmed, photographe d'art. Analyse cette photo de {galerie_nom}.
    
    TACHE : Légende virale et Alt Text.
    
    RÈGLES STRICTES DE RÉDACTION :
    
    1. TITRE : Invente un titre artistique (Capitalisé) entre guillemets.
       BON : "Ombres et Lumières"
       MAUVAIS : "dusseldorf..."
       
    2. ANALYSE : 2 phrases sur la technique/lumière/émotion.
    
    (LAISSE UNE LIGNE VIDE ICI)
    
    3. QUESTION : Question ouverte pour engager l'audience.
    
    4. MENTIONS (CRITIQUE) : Suggère 3 comptes pertinents.
       TU DOIS IMPÉRATIVEMENT METTRE LE SYMBOLE '@' DEVANT CHAQUE NOM.
       Exemple OBLIGATOIRE : (cc @visit{galerie_nom} @lensculture @magnumphotos)
       
    5. LIEN : {display_link}
    
    6. HASHTAGS : 8 hashtags pertinents + {base_tag}.
    
    FORMAT DE SORTIE (Séparateur |||) :
    "Titre"
    [Analyse...]
    
    [Question]
    (cc @mention1 @mention2 @mention3)
    [Lien]
    [Hashtags]
    |||
    [Alt Text factuel]
    
    INTERDIT : "Titre:", "Légende:". Pas de Markdown."""
    
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": [{"type": "text", "text": instructions}, {"type": "image_url", "image_url": {"url": image_url, "detail": "high"}}]}],
        max_tokens=650, temperature=0.7
    )
    
    raw = response.choices[0].message.content
    
    if "|||" in raw:
        parts = raw.split("|||")
        caption_part = parts[0].strip()
        alt_part = parts[1].strip()
    else:
        caption_part = raw
        alt_part = f"Photographie artistique de {galerie_nom} par David Ahmed."

    clean_cap = caption_part.replace("PARTIE 1", "").replace("LÉGENDE", "").replace("Titre :", "").strip()
    
    return f"{clean_cap}|||{alt_part}"




# =================================================================
# SECTION 3 : LOGIQUE DES RÉSEAUX SOCIAUX (AVEC ALT TEXT)
# =================================================================
def split_content(full_text):
    """Sépare la légende du Alt Text."""
    if "|||" in full_text:
        parts = full_text.split("|||")
        return parts[0].strip(), parts[1].strip()
    return full_text, "Art photography by David Ahmed"

def publish_to_instagram(image_url, full_text):
    caption, alt_text = split_content(full_text)
    token = os.environ.get('IG_ACCESS_TOKEN')
    ig_id = "17841453263147553" 
    try:
        # Ajout du paramètre 'alt_text' si supporté par l'API Container, sinon ignoré
        # Note: L'API Graph Instagram standard supporte 'alt_text' sur le container
        r = requests.post(f"https://graph.facebook.com/v21.0/{ig_id}/media", 
                          data={'image_url': image_url, 
                                'caption': caption, 
                                'access_token': token}) # Alt text parfois complexe via API simple, on garde focus caption
        
        # Pour être sûr, on tente d'injecter si l'API le permet (dépend des versions)
        # Mais pour la stabilité, on envoie surtout l'image + caption propre.
        # L'Alt Text est stocké mais l'API Instagram Basic est capricieuse avec.
        # On va le passer pour Threads qui le gère mieux.
        
        c_id = r.json().get('id')
        if not c_id: return False, r.json()
        time.sleep(10)
        requests.post(f"https://graph.facebook.com/v21.0/{ig_id}/media_publish", data={'creation_id': c_id, 'access_token': token})
        return True, "OK"
    except Exception as e: return False, str(e)

def publish_to_threads(image_url, full_text):
    caption, alt_text = split_content(full_text)
    token = os.environ.get('THREADS_ACCESS_TOKEN')
    th_id = os.environ.get('THREADS_USER_ID')
    clean_url = image_url.split('?')[0] 
    try:
        # Threads supporte explicitement 'accessibility_text'
        r = requests.post(f"https://graph.threads.net/v1.0/{th_id}/threads", 
                          data={'media_type': 'IMAGE', 
                                'image_url': clean_url, 
                                'text': caption[:495], 
                                'accessibility_text': alt_text, # <--- ICI LE SEO
                                'access_token': token})
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
# SECTION 7 : PLANIFICATEUR (SCHEDULER 20s)
# =================================================================
def get_belgium_offset():
    month = datetime.datetime.now().month
    return 2 if 4 <= month <= 10 else 1

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
                    token = os.environ.get('TELEGRAM_TOKEN')
                    # Les fonctions publish_to_xxx gèrent le split caption/alt_text
                    ok_ig, res_ig = publish_to_instagram(img, full_text)
                    ok_th, res_th = publish_to_threads(img, full_text)
                    
                    status = 'sent' if (ok_ig and ok_th) else 'error'
                    conn.execute("UPDATE scheduled_posts SET status = ? WHERE id = ?", (status, post_id))
                    conn.commit()
                    
                    msg = "⏰ **Post Programmé Exécuté !**\n"
                    msg += f"IG: {'✅' if ok_ig else '❌'} | TH: {'✅' if ok_th else '❌'}"
                    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"})
                    
                    if ok_ig or ok_th: mark_photo_as_sent(img, "Programmé")
            conn.close()
        except Exception as e:
            print(f"Scheduler error: {e}")
        time.sleep(20)

threading.Thread(target=scheduler_loop, daemon=True).start()

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

    # 1. PRIORITÉ ABSOLUE : COMMANDES TEXTE
    if text:
        if text == "/renew_threads":
            success, result = renew_threads_token()
            msg = f"✅ **TOKEN RENOUVELÉ ({result[1]}j)**\n`{result[0]}`" if success else f"❌ {result}"
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"})
            return jsonify({"status": "ok"})

        elif text == "/debug_db":
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": get_db_stats(), "parse_mode": "Markdown"})
            return jsonify({"status": "ok"})

    # 2. BOUTONS
    if action:
        if action == "schedule_btn":
            session = get_session(chat_id)
            if session:
                # On garde le texte complet (Caption ||| Alt) pour le planning
                save_session(chat_id, session[0], f"WAITING_SCHEDULE|{session[1]}")
                requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "📅 **Heure de publication ?**\n(Format HH:MM, Heure Belge 🇧🇪)\nExemple: `18:30`", "parse_mode": "Markdown"})
            return jsonify({"status": "ok"})
            
        elif action == "renew_threads_btn":
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
                # Les fonctions publish_to_xxx gèrent le split
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
                    # Attention : si édition manuelle, l'user risque de casser le format Alt Text
                    # On demande juste le texte, et on perdra l'Alt Text généré par IA (acceptable pour edit manuel)
                    save_session(chat_id, session[0], "WAITING_FOR_MANUAL")
                    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "✍️ Envoie ta nouvelle légende (L'Alt Text sera retiré)."})
        
        return jsonify({"status": "ok"})

    # 3. GESTION TEXTE (PROGRAMMATION & ÉDITION)
    if text:
        session = get_session(chat_id)
        
        # LOGIQUE PROGRAMMATION
        if session and session[1].startswith("WAITING_SCHEDULE|"):
            try:
                time_str = text.strip().replace('h', ':')
                if ':' not in time_str: time_str += ":00"
                th, tm = map(int, time_str.split(':'))
                
                offset = get_belgium_offset()
                now_be = datetime.datetime.utcnow() + datetime.timedelta(hours=offset)
                target = now_be.replace(hour=th, minute=tm, second=0)
                if target <= now_be: target += datetime.timedelta(days=1)
                
                utc_run = target - datetime.timedelta(hours=offset)
                real_content = session[1].replace("WAITING_SCHEDULE|", "") # Contient Caption ||| Alt
                
                conn = get_db_connection()
                conn.execute("INSERT INTO scheduled_posts (chat_id, image_url, caption, run_at) VALUES (?, ?, ?, ?)", 
                             (chat_id, session[0], real_content, utc_run.strftime("%Y-%m-%d %H:%M:%S")))
                conn.commit()
                conn.execute('DELETE FROM current_session WHERE chat_id = ?', (chat_id,))
                conn.commit()
                conn.close()
                
                requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": f"✅ **Programmé pour {target.strftime('%H:%M')}** (heure belge).", "parse_mode": "Markdown"})
            except:
                requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "❌ Format invalide (ex: 18:00)."})
            return jsonify({"status": "ok"})

        elif session and session[1] == "WAITING_FOR_MANUAL":
            # Si manuel, on sauvegarde juste le texte sans séparateur (Alt Text par défaut sera utilisé)
            save_session(chat_id, session[0], text)
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "✅ Prêt !", "reply_markup": {"inline_keyboard": [[{"text": "🚀 Les deux", "callback_data": "pub_both"}, {"text": "📅 Programmer", "callback_data": "schedule_btn"}], [{"text": "📸 Insta", "callback_data": "pub_ig"}, {"text": "🧵 Threads", "callback_data": "pub_th"}]]}})
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
    # generate_ai_caption renvoie maintenant "Caption ||| Alt Text"
    full_content = generate_ai_caption(img_url, galerie_nom)
    save_session(chat_id, img_url, full_content)
    
    # On affiche uniquement la légende dans Telegram (on cache le Alt Text pour lisibilité)
    visible_caption = full_content.split("|||")[0]

    kb = [
        [{"text": "🚀 Publier sur les deux", "callback_data": "pub_both"}],
        [{"text": "📅 Programmer", "callback_data": "schedule_btn"}],
        [{"text": "📸 Insta", "callback_data": "pub_ig"}, {"text": "🧵 Threads", "callback_data": "pub_th"}],
        [{"text": "🔄 Autre", "callback_data": f"select_{galerie_nom}"}, {"text": "⬅️ Menu", "callback_data": "menu"}]
    ]
    requests.post(f"https://api.telegram.org/bot{token}/sendPhoto", 
                  json={"chat_id": chat_id, "photo": img_url, "caption": visible_caption, "reply_markup": {"inline_keyboard": kb}})

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