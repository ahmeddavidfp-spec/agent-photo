import os, requests

# --- CONFIGURATION (Remplace par tes vraies valeurs pour le test local) ---
TOKEN = "EAAMVZA8ZA7xwIBQs7XDMULNZAYgo7jKZATPWWpgH1VGNMUZC6YVUjnFc5QxnI14hVuaFoVjN6xTmVJ4AsRZA1gLRecYqBdzL7kB72OH3hH8RMkjmI1LHsHGLczW9TZC0VaZA6ZCS0FuUBwFBTJNud1oLnBSxzxfutrZAN1ZBhETmUgHS7zyKjGRIJRZCtpUZCvc1oz1kOMmdOGhFYLLBYzAoc6XZADejJ1sgKxb5g0ZAUXSIOR7IUZAGHqtdB7QCtr0dOuFoB0rv9cJ2sy0vi4BUuNtGSu1vbHGb" # Ton jeton complet
FB_PAGE_ID = "1922962171929204"
IG_BUSINESS_ID = "17841453263147553"
THREADS_USER_ID = "25679272328409797" # À vérifier

def test_connections():
    print("🚀 DÉMARRAGE DU TEST DES CONNEXIONS META\n")

    # 1. TEST FACEBOOK
    print("📘 Test Facebook Page...")
    fb_url = f"https://graph.facebook.com/v19.0/{FB_PAGE_ID}?fields=name&access_token={TOKEN}"
    res_fb = requests.get(fb_url).json()
    if 'name' in res_fb:
        print(f"   ✅ Connecté à la page : {res_fb['name']}")
    else:
        print(f"   ❌ Erreur FB : {res_fb}")

    # 2. TEST INSTAGRAM
    print("\n📸 Test Instagram Pro...")
    ig_url = f"https://graph.facebook.com/v19.0/{IG_BUSINESS_ID}?fields=username&access_token={TOKEN}"
    res_ig = requests.get(ig_url).json()
    if 'username' in res_ig:
        print(f"   ✅ Connecté à Instagram : @{res_ig['username']}")
    else:
        print(f"   ❌ Erreur IG : {res_ig}")

    # 3. TEST THREADS
    print("\n🧵 Test Threads...")
    # On vérifie si le token a la permission Threads
    th_url = f"https://graph.threads.net/v1.0/me?fields=id,username&access_token={TOKEN}"
    try:
        res_th = requests.get(th_url).json()
        if 'id' in res_th:
            print(f"   ✅ Connecté à Threads : @{res_th['username']} (ID: {res_th['id']})")
        else:
            print(f"   ❌ Erreur Threads : {res_th}")
    except:
        print("   ❌ Erreur Threads : Impossible de joindre l'API Threads.")

if __name__ == "__main__":
    test_connections()