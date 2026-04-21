# Checklist de mise en production — Agent Photo v2

Ordre recommandé. Coche au fur et à mesure.

## A. Sécurité urgente (avant le push git)

- [ ] **Rotate `THREADS_CLIENT_SECRET`** dans Meta for Developers.
      L'ancien secret `699da824164758986c163545220ab519` a été commité
      dans `renew_threads.py` au v1 — il est dans l'historique git public.
      → developers.facebook.com › App › Paramètres › Général › *Reset App Secret*
- [ ] Coller le nouveau secret dans **Render › Environment › `THREADS_CLIENT_SECRET`**
      (ne PAS le mettre dans le code).
- [ ] Vérifier que `.env` n'est pas tracké : `git check-ignore -v .env` doit matcher.

## B. Vérif locale

- [ ] `bash deploy.sh --check` → tout en vert.
- [ ] `python3 -c "import app"` ne lève pas d'exception.
- [ ] `cat .gitignore` contient bien `.env`, `photos.db`, `Dernier Token Insta`.

## C. Push git

- [ ] `git add .`
- [ ] `git status` — vérifier qu'AUCUN fichier avec secret n'est dans la liste.
- [ ] `git commit -m "Refactor v2 : modules, retries, cache, sécurité"`
- [ ] `git push origin main`

## D. Render — réglages du service web

| Champ             | Valeur                                                                                  |
|-------------------|-----------------------------------------------------------------------------------------|
| Build Command     | `pip install -r requirements.txt`                                                       |
| Start Command     | `gunicorn app:app --workers 1 --threads 4 --timeout 120 --access-logfile - --error-logfile -` |
| Auto-Deploy       | **ON**                                                                                  |
| Health Check Path | `/health`                                                                               |
| Disk mount        | `/data` (déjà créé — laisser tel quel)                                                  |
| Python version    | `.python-version` = 3.11 (auto-détecté)                                                 |

## E. Render — variables d'environnement

Obligatoires (probablement déjà présentes depuis v1) :

- [ ] `TELEGRAM_TOKEN`
- [ ] `TELEGRAM_CHAT_ID` **ou** `ALLOWED_CHAT_ID` (filtrage)
- [ ] `IG_ACCESS_TOKEN`, `IG_USER_ID` (ou `INSTAGRAM_BUSINESS_ID`), `IG_CLIENT_SECRET`
- [ ] `THREADS_ACCESS_TOKEN`, `THREADS_USER_ID`, `THREADS_CLIENT_SECRET` (rotaté, cf. A)
- [ ] `OPENAI_API_KEY`

Nouvelles (à créer) :

- [ ] `TELEGRAM_WEBHOOK_SECRET` — chaîne aléatoire 32 car (ex. `openssl rand -hex 16`)
- [ ] `CRON_SECRET` — chaîne aléatoire 32 car
- [ ] `OPENAI_MODEL` = `gpt-4o` (optionnel — valeur par défaut)

## F. Telegram — re-abonner le webhook avec le secret

```bash
TG=<ton TELEGRAM_TOKEN>
HOST=https://agent-photo.onrender.com
SEC=<même valeur que TELEGRAM_WEBHOOK_SECRET>

curl -s "https://api.telegram.org/bot$TG/setWebhook" \
     -d "url=$HOST/telegram-webhook" \
     -d "secret_token=$SEC" \
     -d "drop_pending_updates=true"

# Vérifier
curl -s "https://api.telegram.org/bot$TG/getWebhookInfo" | python3 -m json.tool
```

Attendu : `"ok": true`, `"url"` = ton domaine Render, `has_custom_certificate: false`.

## G. Render — Cron Job de renouvellement des tokens (tous les 45 j)

- [ ] *New › Cron Job*
- [ ] Schedule : `0 3 */45 * *`  (3 h du matin UTC, tous les 45 j)
- [ ] Command  :
      ```
      curl -fsS -X POST -H "X-Cron-Secret: $CRON_SECRET" https://agent-photo.onrender.com/cron/refresh-tokens
      ```

## H. Tests finaux

- [ ] `curl -fsS https://agent-photo.onrender.com/health | python3 -m json.tool`
      → `db: "ok"`, les deux tokens > 0 j.
- [ ] Sur Telegram : envoyer `/debug_db` → stats revenues.
- [ ] Sur Telegram : envoyer `/start` (ou n'importe quel texte) → menu avec les galeries
      (les nombres `done/total` doivent apparaître en < 2 s grâce au cache).
- [ ] Cliquer une galerie → photo + légende en < 15 s.
- [ ] Publier sur Threads + Insta → vérifier qu'il n'y a plus de message
      `Attente 25s (Upload Meta)...` dans les logs (le polling remplace le sleep).

## I. Nettoyage post-déploiement (quand tout tourne depuis 48 h)

- [ ] Supprimer le dossier `v2/` en doublon :
      ```
      rm -rf v2/
      git add -A && git commit -m "Cleanup: remove v2/ (already migrated to root)"
      git push
      ```
- [ ] Archiver `legacy/` ou le laisser en place (sert de référence).

---

## En cas de pépin

- Logs Render : Dashboard › Logs (live tail).
- Si le bot ne répond plus : `getWebhookInfo` Telegram, vérifier `last_error_message`.
- Si publication échoue : `token_status()` via `/health`, puis bouton 🔄 Renew.
- Rollback : `git revert HEAD && git push` — Render redéploie l'ancien v1 en 2 min.
