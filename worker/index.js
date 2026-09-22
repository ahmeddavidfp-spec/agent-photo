// Worker Cloudflare devant le conteneur Agent Photo.
// - Route TOUTE requete HTTP (webhook Telegram, Studio PWA, /api, /health, /cron)
//   vers l'unique instance du conteneur (app Flask + ffmpeg).
// - Transmet au conteneur, comme variables d'environnement, TOUTES les valeurs
//   texte de `env` (secrets Worker + vars). Ainsi, ajouter un secret via
//   `wrangler secret put` suffit : aucune modif de code par secret.
import { Container, getContainer } from "@cloudflare/containers";

export class AgentPhotoContainer extends Container {
  defaultPort = 8080;   // port ecoute par gunicorn dans le conteneur
  sleepAfter = "15m";   // s'endort apres 15 min sans requete (scale-to-zero)

  constructor(ctx, env) {
    super(ctx, env);
    // Ne transmet que les valeurs TEXTE (les bindings comme AGENT_PHOTO ou les
    // buckets R2 sont des objets → ignores).
    const vars = {};
    for (const [k, v] of Object.entries(env || {})) {
      if (typeof v === "string") vars[k] = v;
    }
    this.envVars = vars;
  }
}

export default {
  // Instance UNIQUE et partagee (une seule base SQLite) -> id constant.
  async fetch(request, env) {
    return getContainer(env.AGENT_PHOTO, "singleton").fetch(request);
  },

  // Cron Triggers (Phase 2) : reveillent le conteneur et declenchent le
  // traitement du scheduler (posts dus + reel quotidien). Le conteneur dort,
  // donc sa boucle interne ne tourne pas : c'est ce battement qui la remplace.
  async scheduled(event, env) {
    const c = getContainer(env.AGENT_PHOTO, "singleton");
    const secret = (env && env.CRON_SECRET) ? env.CRON_SECRET : "";
    await c.fetch(new Request("https://agent-photo.internal/cron/tick", {
      method: "POST",
      headers: { "X-Cron-Secret": secret },
    }));
  },
};
