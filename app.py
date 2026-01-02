import os
import requests
import yaml
import psycopg2
from bs4 import BeautifulSoup
from flask import Flask

app = Flask(__name__)

# 1. Charger la configuration depuis le fichier YAML
def load_config():
    with open("config.yaml", "r") as f:
        return yaml.safe_load(f)

# 2. Initialiser la base de données PostgreSQL
def init_db():
    db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        return
    conn = psycopg2.connect(db_url)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS processed_images (
            id SERIAL PRIMARY KEY,
            image_url TEXT UNIQUE NOT NULL,
            gallery_name TEXT,
            status TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    conn.commit()
    cur.close()
    conn.close()

# 3. Fonction pour scanner ton site Squarespace
def get_images_from_gallery(gallery_name):
    config = load_config()
    url = f"{config['site_url']}/{gallery_name}"
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
    
    try:
        response = requests.get(url, headers=headers)
        soup = BeautifulSoup(response.text, 'html.parser')
        images = []
        for img in soup.find_all('img'):
            src = img.get('data-src') or img.get('src')
            if src:
                if 'format=' not in src: src = f"{src}?format=2500w"
                if src.startswith('//'): src = "https:" + src
                images.append(src)
        return list(set(images))
    except Exception as e:
        return []

@app.route("/")
def index():
    return "L'agent photo de David est en ligne et prêt !"

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)