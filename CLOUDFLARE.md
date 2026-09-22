# Migration Agent Photo vers Cloudflare (Containers)

Objectif : héberger l'app (Python/Flask + ffmpeg) sur Cloudflare au lieu de
Render. Seule option qui garde le code quasi intact : **Cloudflare Containers**
(image Docker) frontée par un **Worker**.

## Prérequis (côté compte Cloudflare)

1. **Plan Workers Paid** (5 $/mois). Containers n'existe pas sur le plan gratuit.
2. `wrangler` authentifié sur le compte : `npx wrangler login`.
3. Docker installé en local (l'image est buildée puis poussée au `deploy`).

La conso du conteneur est facturée à l'usage. Pour la charge d'Agent Photo
(quelques tâches/jour), l'ordre de grandeur vise 5 à 10 $/mois au lieu de 25 $.

## Fichiers ajoutés

- `Dockerfile` : image de l'app (Python 3.11 + ffmpeg système + gunicorn sur 8080).
- `.dockerignore` : exclut `.git`, le dossier privé, snapshots, etc.
- `wrangler.jsonc` : config du Worker + du Container.
- `worker/index.js` : le Worker qui route les requêtes vers le conteneur.
- `package.json` : dépendances `wrangler` + `@cloudflare/containers`.

Render n'est pas touché : son runtime est `python` (piloté par le dashboard),
il ignore ces fichiers. Il reste en prod tant que Cloudflare n'est pas validé.

## Phases

- **Phase 0 (faite)** : fondation. Le conteneur build et sert l'app par HTTP.
  Valide le plus dur (image, ffmpeg, gunicorn, routage Worker).
  NON prêt pour la prod : la base SQLite est éphémère (perdue quand le conteneur
  dort). Ne pas basculer le trafic réel à ce stade.
- **Phase 1 (à venir)** : persistance. Remplacer le disque `/data` de Render par
  un bucket **R2** (télécharger la sauvegarde SQLite au boot, la réuploader
  périodiquement). Décommenter le binding R2 dans `wrangler.jsonc`.
- **Phase 2 (à venir)** : scheduler. Le conteneur dort, donc la boucle en mémoire
  ne tourne pas. Piloter par **Cron Triggers** (endpoint `/cron/tick`).
  DÉCISION À PRENDRE : fréquence de réveil = compromis coût / ponctualité des
  posts programmés (réveiller souvent coûte plus ; réveiller rarement = posts en
  léger retard).
- **Phase 3 (à venir)** : bascule. Secrets côté conteneur, `wrangler deploy`,
  tests réels (post + reel), repointer le webhook Telegram et l'URL du Studio,
  puis couper Render.

## Déploiement (Phase 0, pour tester le build)

```bash
npm install
npx wrangler login          # une fois
npx wrangler deploy         # build l'image, la pousse, deploie le Worker
```

Les secrets (tokens Telegram/Meta/OpenAI/Anthropic/Pinterest, STUDIO_TOKEN, etc.)
se posent ensuite en variables du Worker/conteneur via `npx wrangler secret put NOM`
(à faire avant la Phase 3, jamais commités).

Crédit : Scribeo (scribeo.be).
