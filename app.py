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
    except Exception as e: return False, str(e)

def publish_to_threads(image_url, caption):
    token = os.environ.get('IG_ACCESS_TOKEN')
    th_id = os.environ.get('THREADS_USER_ID')
    if not th_id: return False, "ID Threads manquant"
    try:
        r = requests.post(f"https://graph.threads.net/v1.0/{th_id}/threads", 
                          data={'image_url': image_url, 'text': caption, 'access_token': token})
        c_id = r.json().get('id')
        if not c_id: return False, r.json()
        time.sleep(10)
        requests.post(f"https://graph.threads.net/v1.0/{th_id}/threads_publish", data={'creation_id': c_id, 'access_token': token})
        return True, "OK"
    except Exception as e: return False, str(e)

# --- GÉNÉRATION DE LÉGENDE AMÉLIORÉE ---

def generate_ai_caption(image_url, galerie_nom):
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    config = load_config()
    site_url = config.get('site_url', 'https://www.davidahmed.me').rstrip('/')
    galerie_link = f"{site_url}/{galerie_nom}"

    instructions = f"""Tu es David Ahmed, photographe de rue expert.
    MISSION : Analyse cette photo à {galerie_nom} pour Instagram et Threads.
    
    STYLE ÉDITORIAL :
    - Ton : Cinématographique, mélancolique, minimaliste.
    - Vocabulaire : Évite les répétitions. Ne force pas les mots techniques si la scène ne s'y prête pas. 
    - Focus : Décris l'émotion de l'instant, le silence, ou la géométrie sans être scolaire.
    
    STRUCTURE STRICTE :
    1. TITRE : Court, en MAJUSCULES. JAMAIS de symboles comme '*' ou '_'.
    2. TEXTE : À la première personne. MAX 350 caractères. Pas de Markdown.
    3. CTA : "Série complète disponible sur : {galerie_link}"
    4. HASHTAGS : Sélectionne les 6-8 plus adaptés au contenu visuel parmi : {config.get('hashtags', '')}
    
    IMPORTANT : Pas d'émojis. Rythme sec et poétique."""
    
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": [{"type": "text", "text": instructions}, {"type": "image_url", "image_url": {"url": image_url}}]}],
        max_tokens=500,
        temperature=0.8 # Augmentation de la créativité pour éviter la répétition
    )
    return response.choices[0].message.content

# --- GESTION WEBHOOK & SESSIONS ---

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

# (Le reste du code pour les menus et webhooks reste identique à ta structure fonctionnelle)