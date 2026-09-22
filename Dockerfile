# Image du conteneur Cloudflare (et utilisable en local).
# Cloudflare Containers exige linux/amd64 -> on le force pour builder aussi
# depuis un Mac Apple Silicon.
FROM --platform=linux/amd64 python:3.11-slim

# ffmpeg COMPLET au niveau systeme (a le filtre xfade des transitions Reel).
# On l'installe une fois dans l'image -> aucun telechargement au runtime
# (le disque du conteneur est ephemere). reel.py l'utilise via FFMPEG_BINARY.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FFMPEG_BINARY=/usr/bin/ffmpeg \
    PORT=8080

WORKDIR /app

# Couche de deps mise en cache tant que requirements.txt ne bouge pas.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Le reste de l'app (voir .dockerignore pour les exclusions).
COPY . .

EXPOSE 8080

# Meme serveur qu'sur Render (1 worker, 8 threads, timeout 120), mais lie au
# port du conteneur.
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT} --workers 1 --threads 8 --timeout 120 --access-logfile - --error-logfile -"]
