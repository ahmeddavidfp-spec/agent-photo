// Worker Cloudflare devant le conteneur Agent Photo.
// Role Phase 0 : router TOUTE requete HTTP (webhook Telegram, Studio PWA, /api,
// /health) vers l'unique instance du conteneur (app Flask + ffmpeg).
import { Container, getContainer } from "@cloudflare/containers";

export class AgentPhotoContainer extends Container {
  defaultPort = 8080;   // port ecoute par gunicorn dans le conteneur
  sleepAfter = "15m";   // s'endort apres 15 min sans requete (scale-to-zero)
}

export default {
  async fetch(request, env) {
    // Instance UNIQUE et partagee (une seule base SQLite) -> id constant.
    // getContainer route vers le conteneur et le demarre au besoin.
    return getContainer(env.AGENT_PHOTO, "singleton").fetch(request);
  },

  // scheduled() (Cron Triggers) sera ajoute en Phase 2 pour piloter le
  // scheduler (posts dus + Reel quotidien) quand le conteneur dort.
};
