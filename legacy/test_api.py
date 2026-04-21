import requests, time

# --- TON TOKEN THREADS (Celui qui fonctionne pour le texte) ---
TOKEN = "THAAcPQkeBStJBUVJzX1V0X1lLUFdoWlh2UnU4cDhMTE1ucWM4anhxUVpVWDNnbmVTcTNMRkNXZAzhMd0hHclZAOaXBOa1M4ZAnFQUkxKbGFzcl85UDAySVRHRndGTk1lNms0VnFBSTM3LVZAPdGNURmhOLVAtWFpQN2xDLURyb1llVnc5UQZDZD"

# L'image exacte qui a planté tout à l'heure
ORIGINAL_IMAGE = "https://images.squarespace-cdn.com/content/v1/6274e5dd88ff573e9bd7999d/b718eaa4-540f-46bb-9b39-42b44b1ea580/L1012292.jpg"

def test_optimized_publish():
    print("🚀 TEST PUBLICATION THREADS (OPTIMISATION SQUARESPACE)...")

    # 1. OPTIMISATION DE L'URL (C'est la clé !)
    # On ajoute ?format=1000w pour que Squarespace donne une image légère
    optimized_url = ORIGINAL_IMAGE + "?format=100w"
    
    print(f"📉 URL Originale : {ORIGINAL_IMAGE}")
    print(f"✅ URL Optimisée : {optimized_url}")

    # 2. Récupération ID
    try:
        r = requests.get(f"https://graph.threads.net/v1.0/me?fields=id,username&access_token={TOKEN}")
        data = r.json()
        if 'id' not in data:
            print(f"❌ Token invalide : {data}")
            return
        user_id = data['id']
        print(f"👤 Compte : @{data.get('username')} (ID: {user_id})")
    except Exception as e:
        print(f"❌ Erreur co : {e}")
        return

    # 3. Création du Post
    print("\n📸 Envoi de l'image optimisée...")
    url = f"https://graph.threads.net/v1.0/{user_id}/threads"
    
    payload = {
        'media_type': 'IMAGE',
        'image_url': optimized_url,
        'text': 'Test Image Optimisée (100px) 🧪',
        'access_token': TOKEN
    }
    
    # On utilise data= (form-data) pour être sûr
    r = requests.post(url, data=payload)
    res = r.json()
    
    if 'id' in res:
        c_id = res['id']
        print(f"✅ SUCCÈS ! Conteneur créé : {c_id}")
        print("⏳ Attente 25s (Traitement chez Meta)...")
        time.sleep(15)
        
        # 4. Publication
        print("🚀 Publication finale...")
        url_pub = f"https://graph.threads.net/v1.0/{user_id}/threads_publish"
        r_pub = requests.post(url_pub, data={'creation_id': c_id, 'access_token': TOKEN})
        res_pub = r_pub.json()
        
        if 'id' in res_pub:
            print(f"🎉 VICTOIRE ! L'image est en ligne !")
            print("👉 La solution '?format=1000w' fonctionne !")
        else:
            print(f"❌ ECHEC PUBLICATION : {res_pub}")
    else:
        print(f"❌ ECHEC CREATION : {res}")

if __name__ == "__main__":
    test_optimized_publish()