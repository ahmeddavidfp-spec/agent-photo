#!/usr/bin/env bash
# =============================================================
#  GO.sh — commandes prêtes à exécuter pour déployer v2.
#  Ne PAS lancer d'un coup. Lis, copie-colle section par section.
# =============================================================

# -------------------------------------------------------------
# SECRETS générés pour toi (pré-remplis ci-dessous)
# -------------------------------------------------------------
TELEGRAM_WEBHOOK_SECRET='jyvYqCsh1qfXkVMSOAdo0WDeJvWfd2Ql'
CRON_SECRET='novWi9-12Vz64q-71visTYzx6Eo5xpp2'

# -------------------------------------------------------------
# ÉTAPE 0 — TELEGRAM_TOKEN
# Exporte-le avant de continuer (ne le commit JAMAIS)
# -------------------------------------------------------------
# export TELEGRAM_TOKEN='ton_token_ici'
# export RENDER_HOST='https://agent-photo.onrender.com'   # adapte si différent

# =============================================================
# ÉTAPE 1 — VÉRIF LOCALE
# =============================================================
cd ~/agent-photo
bash deploy.sh --check
# attendu : "Checks OK"

# =============================================================
# ÉTAPE 2 — GIT : committer et pousser
# =============================================================
git status              # relire la liste
git add .
git status              # vérifier qu'aucun token ni .env n'est dans la liste
git commit -m "Refactor v2 : modules, retries, cache, sécurité"
git push origin main    # déclenche le déploiement Render

# =============================================================
# ÉTAPE 3 — RENDER DASHBOARD (manuel, 3 min)
#   URL : https://dashboard.render.com → service "agent-photo"
# =============================================================
#
# A. Settings › Build & Deploy
#    • Build Command   : pip install -r requirements.txt
#    • Start Command   : gunicorn app:app --workers 1 --threads 4 --timeout 120 --access-logfile - --error-logfile -
#    • Auto-Deploy     : ON
#    • Health Check    : /health
#
# B. Environment › Add Env Var (trois nouvelles) :
#    TELEGRAM_WEBHOOK_SECRET = jyvYqCsh1qfXkVMSOAdo0WDeJvWfd2Ql
#    CRON_SECRET             = novWi9-12Vz64q-71visTYzx6Eo5xpp2
#    OPENAI_MODEL            = gpt-4o    (si absent)
#
#    → Save, Changes; Render redémarre tout seul.

# =============================================================
# ÉTAPE 4 — META : ROTATION du THREADS_CLIENT_SECRET  ⚠️ urgent
# =============================================================
#   1. https://developers.facebook.com/apps  → ton app
#   2. Paramètres › Général › App Secret › "Reset"
#   3. Copie le nouveau secret
#   4. Render › Environment › THREADS_CLIENT_SECRET › Edit → colle → Save
#
#   (l'ancien secret 699da824…519 a fuité dans l'historique git v1)

# =============================================================
# ÉTAPE 5 — TELEGRAM : re-souscrire le webhook avec le secret
# =============================================================
TG="$TELEGRAM_TOKEN"
HOST="${RENDER_HOST:-https://agent-photo.onrender.com}"
SEC='jyvYqCsh1qfXkVMSOAdo0WDeJvWfd2Ql'

curl -s "https://api.telegram.org/bot${TG}/setWebhook" \
     -d "url=${HOST}/telegram-webhook" \
     -d "secret_token=${SEC}" \
     -d "drop_pending_updates=true" \
   | python3 -m json.tool
# attendu : {"ok": true, "result": true, "description": "Webhook was set"}

curl -s "https://api.telegram.org/bot${TG}/getWebhookInfo" | python3 -m json.tool
# attendu : url = $HOST/telegram-webhook, pending_update_count = 0

# =============================================================
# ÉTAPE 6 — RENDER CRON JOB (renew tokens tous les 45 j)
#   Dashboard Render → New → Cron Job
# =============================================================
#   Name        : agent-photo-refresh-tokens
#   Schedule    : 0 3 */45 * *
#   Command     : curl -fsS -X POST -H "X-Cron-Secret: novWi9-12Vz64q-71visTYzx6Eo5xpp2" https://agent-photo.onrender.com/cron/refresh-tokens
#   → Create Cron Job

# =============================================================
# ÉTAPE 7 — TESTS FINAUX
# =============================================================
HOST='https://agent-photo.onrender.com'
curl -fsS "${HOST}/health" | python3 -m json.tool
# attendu : "db": "ok", "tokens" mentionnant "IG : Xj" et "TH : Xj"

# Sur Telegram, à ton bot :
#   /debug_db        → stats
#   /start           → menu (< 2 s grâce au cache)
#   clic une galerie → photo + légende en < 15 s
#   clic "🚀 Publier sur les deux" → vérifie qu'il n'y a plus
#                     "Attente 25s (Upload Meta)..." dans les logs Render

echo "🎉 Déploiement terminé — garde les secrets dans un password manager."
