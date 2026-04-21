#!/usr/bin/env bash
# ---------------------------------------------------------------
#  Agent Photo — script de vérification + aide au déploiement
# ---------------------------------------------------------------
#  Ce script NE déploie PAS tout seul. Il :
#    1. vérifie la syntaxe Python de tous les modules
#    2. vérifie la présence des fichiers essentiels
#    3. imprime les commandes à exécuter (git, curl Telegram, etc.)
#
#  Usage :
#    bash deploy.sh            # vérifs + affichage des commandes
#    bash deploy.sh --check    # vérifs uniquement (CI)
# ---------------------------------------------------------------
set -eo pipefail
cd "$(dirname "$0")"

BOLD="\033[1m"; GREEN="\033[32m"; RED="\033[31m"; YELLOW="\033[33m"; RESET="\033[0m"

say()  { printf "${BOLD}➜ %s${RESET}\n" "$1"; }
ok()   { printf "  ${GREEN}✓${RESET} %s\n" "$1"; }
fail() { printf "  ${RED}✗${RESET} %s\n" "$1"; exit 1; }
warn() { printf "  ${YELLOW}!${RESET} %s\n" "$1"; }

# ---------------------------------------------------------------
# 1. Syntaxe Python
# ---------------------------------------------------------------
say "Vérification syntaxe Python"
PY_FILES=(app.py ai.py db.py gallery.py http_client.py meta_api.py \
          scheduler.py settings.py telegram_bot.py timezones.py)
for f in "${PY_FILES[@]}"; do
  [[ -f "$f" ]] || fail "$f manquant"
  python3 -m py_compile "$f" || fail "$f ne compile pas"
done
ok "Tous les modules compilent"

# ---------------------------------------------------------------
# 2. Fichiers essentiels
# ---------------------------------------------------------------
say "Fichiers de configuration"
for f in Procfile requirements.txt .python-version config.yaml .gitignore .env.example README.md; do
  [[ -f "$f" ]] && ok "$f" || fail "$f manquant"
done

# ---------------------------------------------------------------
# 3. Pas de secret en dur
# ---------------------------------------------------------------
say "Scan des secrets hard-codés"
if grep -R --include='*.py' -nE '(EAAB[A-Za-z0-9]{30,}|THAA[A-Za-z0-9]{30,})' . 2>/dev/null | grep -v legacy/ | grep -v __pycache__ | grep -v v2/; then
  fail "Un token Meta semble hard-codé — à retirer avant de pousser."
else
  ok "Pas de token Meta détecté dans les .py (hors legacy/)"
fi

# ---------------------------------------------------------------
# 4. .env local (optionnel)
# ---------------------------------------------------------------
say ".env local"
if [[ -f .env ]]; then
  ok ".env présent (utilisé en dev uniquement)"
else
  warn ".env absent (normal en prod — Render fournit les env vars)"
fi

[[ "${1:-}" == "--check" ]] && { echo; ok "Checks OK"; exit 0; }

# ---------------------------------------------------------------
# 5. Aide au déploiement
# ---------------------------------------------------------------
cat <<'EOF'

─────────────────────────────────────────────────────────────────
 ÉTAPES MANUELLES — copie-colle ce qu'il te faut
─────────────────────────────────────────────────────────────────

 1) GIT — committer les nouveaux fichiers
    git add .
    git status                       # relire
    git commit -m "Refactor v2 : modules, retries, cache, sécurité"
    git push origin main             # déclenche Render si Auto-Deploy ON

 2) RENDER — réglages à vérifier dans le dashboard
    Settings › Build & Deploy
      Build Command   : pip install -r requirements.txt
      Start Command   : gunicorn app:app --workers 1 --threads 4 --timeout 120 --access-logfile - --error-logfile -
      Auto-Deploy     : ON
      Health Check    : /health
      Pre-Deploy Cmd  : (laisser vide)

    Environment › ajouter si absent
      TELEGRAM_WEBHOOK_SECRET = <chaîne aléatoire 32 car>
      CRON_SECRET             = <chaîne aléatoire 32 car>
      OPENAI_MODEL            = gpt-4o   (si absent)

    Disks
      Mount path /data  (déjà OK)      — base SQLite persistante

 3) TELEGRAM — abonner le webhook avec le secret
    (remplace $TG et $HOST par les vraies valeurs)

    TG=<TELEGRAM_TOKEN>
    HOST=https://agent-photo.onrender.com
    SEC=<même valeur que TELEGRAM_WEBHOOK_SECRET>

    curl -s "https://api.telegram.org/bot$TG/setWebhook" \
         -d "url=$HOST/telegram-webhook" \
         -d "secret_token=$SEC" \
         -d "drop_pending_updates=true"

    # Vérifier :
    curl -s "https://api.telegram.org/bot$TG/getWebhookInfo" | python3 -m json.tool

 4) META — ⚠️ rotation du THREADS_CLIENT_SECRET
    L'ancien secret a été commité dans renew_threads.py (historique git).
    → developers.facebook.com › ton app › Paramètres › Général › Reset App Secret
    → puis remettre la nouvelle valeur dans Render › Environment › THREADS_CLIENT_SECRET

 5) RENDER CRON JOB — renouveler les tokens tous les 45 jours
    New › Cron Job
      Schedule : 0 3 */45 * *
      Command  : curl -fsS -X POST -H "X-Cron-Secret: $CRON_SECRET" $HOST/cron/refresh-tokens

 6) TEST EN PROD
    curl -fsS $HOST/health | python3 -m json.tool
    → puis envoyer "/debug_db" au bot sur Telegram

─────────────────────────────────────────────────────────────────
EOF

say "Checks OK — suis les 6 étapes ci-dessus et tu es en production."
