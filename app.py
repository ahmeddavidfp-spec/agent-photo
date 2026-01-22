def publish_to_threads(image_url, caption):
    token = os.environ.get('IG_ACCESS_TOKEN')
    th_id = os.environ.get('THREADS_USER_ID')
    
    if not th_id:
        return False, "ID Threads manquant sur Render"

    try:
        # Étape 1 : Créer le conteneur du post
        r = requests.post(
            f"https://graph.threads.net/v1.0/{th_id}/threads", 
            data={'image_url': image_url, 'text': caption, 'access_token': token}
        )
        res_data = r.json()
        container_id = res_data.get('id')
        
        if not container_id:
            # Récupère l'erreur précise de Meta
            msg = res_data.get('error', {}).get('message', 'Erreur inconnue')
            return False, f"Threads Error (Conteneur) : {msg}"

        # Attente obligatoire pour que Meta traite l'image
        time.sleep(15) 

        # Étape 2 : Publier le conteneur
        r_pub = requests.post(
            f"https://graph.threads.net/v1.0/{th_id}/threads_publish", 
            data={'creation_id': container_id, 'access_token': token}
        )
        
        if r_pub.status_code == 200:
            return True, "OK"
        else:
            return False, f"Threads Error (Publication) : {r_pub.json()}"
            
    except Exception as e:
        return False, str(e)