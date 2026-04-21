# Agent Photo v2

Agent qui scanne les galeries de [davidahmed.me](https://www.davidahmed.me), choisit une photo jamais publiée, génère une légende bilingue via GPT-4o et la publie sur Instagram + Threads. Piloté par Telegram, déployé sur Render.

## Qu'est-ce qui a changé par rapport à v1 ?

**Sécurité**
- Plus aucun secret en dur (IG user ID, tokens, client secret).
- Webhook Telegram vérifié via `X-Telegram-Bot-Api-Secret-Token`.
- Accès restreint à `ALLOWED_CHAT_ID` si défini.
- Endpoint `/cron/refresh-tokens` protégé par `X-Cron-Secret`.
- `try/except` nus remplacés par de vrais log `logger.exception`.

**Fiabilité**
- `requests.Session` global avec retry exponentiel (3 tentatives, 1-2-4s).
- Timeout par défaut (15s) sur toutes les requêtes.
- Publication Meta : **polling du `status_code` jusqu'à FINISHED** au lieu de `time.sleep(25)` en aveugle.
- Fuseau horaire via `zoneinfo("Europe/Brussels")` — gère DST automatiquement.
- SQLite ouvert via context manager + index sur `(status, run_at)` et `(galerie)`.

**Structure**
- `app.py` monolithique (691 lignes) → modules dédiés (`db`, `ai`, `meta_api`, `telegram_bot`, `scheduler`, `gallery`, `http_client`, `settings`, `timezones`).
- Prompt GPT externalisé dans `prompts/caption.txt` (itérable sans redéploiement).
- Fallback propre si OpenAI tombe.

**Observabilité**
- `/health` renvoie statut DB + tokens (pour UptimeRobot).
- Commande Telegram `/health`.
- Commande `/renew_instagram` ajoutée (v1 n'avait que Threads).

## Fichiers

```
v2/
├── app.py              # Flask + dispatch webhook Telegram
├── settings.py         # Centralisation env + config.yaml
├── db.py               # Accès SQLite
├── ai.py               # OpenAI + split caption/alt
├── meta_api.py         # Instagram + Threads + polling
├── gallery.py          # Scraping davidahmed.me
├── telegram_bot.py     # Wrappers API Telegram
├── scheduler.py        # Thread qui publie les posts programmés
├── timezones.py        # Europe/Brussels ↔ UTC
├── http_client.py      # requests.Session avec retries
├── renew_threads.py    # CLI pour échanger le token 60j
├── test_api.py         # Smoke test publication Threads
├── prompts/
│   └── caption.txt     # Template GPT
├── config.yaml         # Galeries + bio + hashtags
├── requirements.txt
├── Procfile            # gunicorn 1 worker / 4 threads / 120s timeout
├── .env.example
└── .gitignore
```

## Configuration (Render)

1. Créer un service web Python + un disque persistant monté sur `/data`.
2. Renseigner les variables d'environnement (cf. `.env.example`).
3. Enregistrer le webhook Telegram :

```bash
curl -X POST "https://api.telegram.org/bot$TELEGRAM_TOKEN/setWebhook" \
  -d "url=https://<ton-service>.onrender.com/telegram-webhook" \
  -d "secret_token=$TELEGRAM_WEBHOOK_SECRET"
```

4. Ajouter un **Cron Job Render** toutes les 45 jours :

```
POST https://<ton-service>.onrender.com/cron/refresh-tokens
Header : X-Cron-Secret: <CRON_SECRET>
```

5. Brancher UptimeRobot sur `https://<ton-service>.onrender.com/health` (200 OK si la DB répond).

## Commandes Telegram

| Commande           | Effet                                           |
| ------------------ | ----------------------------------------------- |
| `/debug_db`        | Nombre de photos envoyées par galerie           |
| `/health`          | Jours restants sur les tokens IG & TH           |
| `/renew_threads`   | Régénère le token Threads 60j                   |
| `/renew_instagram` | Régénère le token Instagram 60j                 |
| (texte libre)      | Ouvre le menu galerie                           |

## Points d'attention

- **Gunicorn** doit tourner en **1 seul worker** (le scheduler est un thread démon ; avec plusieurs workers il se dupliquerait et publierait les posts programmés N fois). `--threads 4` suffit pour la latence webhook.
- Si tu passes à plusieurs workers un jour, sors le scheduler dans un **Background Worker Render** séparé ou utilise un **Cron Job** qui POST sur `/scheduler/tick`.
- SQLite sur `/data` Render coûte 1 $/mois. Alternative gratuite : Postgres Render (plan hobby).

## Développement local

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && $EDITOR .env
python app.py
```

Smoke test Threads :

```bash
python test_api.py
```
