"""Smoke test : publie une image optimisée sur Threads pour vérifier le flow.

Lance-le avec :
    THREADS_ACCESS_TOKEN=xxx THREADS_USER_ID=yyy python test_api.py [URL]

URL par défaut : image de démonstration Squarespace. On ajoute ?format=1000w
automatiquement (c'est la clé pour que Meta ingère rapidement).
"""
import os
import sys

from http_client import safe_get, safe_post
from meta_api import _poll_container_status  # type: ignore
from settings import TH_API, THREADS_ACCESS_TOKEN, THREADS_USER_ID


DEFAULT_IMAGE = (
    "https://images.squarespace-cdn.com/content/v1/6274e5dd88ff573e9bd7999d/"
    "b718eaa4-540f-46bb-9b39-42b44b1ea580/L1012292.jpg"
)


def test_publish(image_url: str) -> int:
    token = THREADS_ACCESS_TOKEN
    user_id = THREADS_USER_ID

    if not (token and user_id):
        print("❌ THREADS_ACCESS_TOKEN / THREADS_USER_ID manquants.")
        return 1

    optimized = image_url.split("?")[0] + "?format=1000w"
    print(f"📉 URL d'origine : {image_url}")
    print(f"✅ URL optimisée : {optimized}")

    # 1. Vérification identité
    r = safe_get(
        f"{TH_API}me",
        params={"fields": "id,username", "access_token": token},
    )
    me = r.json()
    if "id" not in me:
        print(f"❌ Token invalide : {me}")
        return 2
    print(f"👤 Compte : @{me.get('username')} (id={me['id']})")

    # 2. Création conteneur
    print("\n📸 Création du conteneur...")
    r = safe_post(
        f"{TH_API}{user_id}/threads",
        data={
            "media_type": "IMAGE",
            "image_url": optimized,
            "text": "Test Image Optimisée (1000px) 🧪",
            "access_token": token,
        },
    )
    res = r.json()
    if "id" not in res:
        print(f"❌ Création échouée : {res}")
        return 3
    container_id = res["id"]
    print(f"✅ Conteneur : {container_id}")

    # 3. Polling au lieu de sleep fixe
    print("⏳ Polling FINISHED...")
    if not _poll_container_status(container_id, token, TH_API):
        print("❌ Conteneur pas FINISHED")
        return 4

    # 4. Publication
    pub = safe_post(
        f"{TH_API}{user_id}/threads_publish",
        data={"creation_id": container_id, "access_token": token},
    )
    pub_res = pub.json()
    if "id" in pub_res:
        print("🎉 VICTOIRE !")
        return 0
    print(f"❌ Publication ratée : {pub_res}")
    return 5


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_IMAGE
    sys.exit(test_publish(url))
