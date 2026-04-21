import requests

# Remplace par tes vraies infos 
CLIENT_ID = "1987102178888402" 
CLIENT_SECRET = "TON_CLIENT_SECRET_META"
CURRENT_TOKEN = "TON_TOKEN_ACTUEL"

def exchange_for_long_lived():
    print("⏳ Demande de jeton 60 jours en cours...")
    url = "https://graph.threads.net/access_token"
    params = {
        "grant_type": "th_exchange_token",
        "client_secret": 699da824164758986c163545220ab519,
        "access_token": THAAcPQkeBStJBUVRHSlJZAVlA4QkRISVJHY295N0ZA3cXpmcmdLLURMajhuUHNBRkRSYmRmUVdCSGt1TkNEZAS1qakxaakc2cXNYVExVYW9DU1ppanFoSVZAlVHE4LVdzVUtqYzdtTV9ZAcEFMMkJPUWJleWE0ZATR0XzZApMTE3STEtWnhXRWN3emFzNVlmLVR4X3JnOHZA6YWQzc2JtZAVhVRWk2UlhDWVRxZAwZDZD
    }
    
    r = requests.get(url, params=params)
    res = r.json()
    
    if "access_token" in res:
        print("✅ SUCCÈS !")
        print(f"Nouveau Token : {res['access_token']}")
        print(f"Expire dans environ : {res.get('expires_in', 0) // 86400} jours")
    else:
        print("❌ ÉCHEC")
        print(f"Détails : {res}")

if __name__ == "__main__":
    exchange_for_long_lived()