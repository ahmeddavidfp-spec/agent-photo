"""Outil CLI simple pour renouveler manuellement le token Threads.

Usage :
    THREADS_CLIENT_SECRET=xxx THREADS_ACCESS_TOKEN=yyy python renew_threads.py

Aucun secret n'est codé en dur : ils viennent toujours de l'environnement.
"""
import os
import sys

from meta_api import renew_threads_token


def main() -> int:
    if not (os.environ.get("THREADS_CLIENT_SECRET") and os.environ.get("THREADS_ACCESS_TOKEN")):
        print("❌ THREADS_CLIENT_SECRET ou THREADS_ACCESS_TOKEN manquant.")
        return 1

    print("⏳ Échange de jeton 60j en cours...")
    ok, res = renew_threads_token()
    if ok:
        token, days = res
        print("✅ SUCCÈS !")
        print(f"Nouveau Token : {token}")
        print(f"Expire dans : {days} jours")
        return 0
    print("❌ ÉCHEC")
    print(f"Détails : {res}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
