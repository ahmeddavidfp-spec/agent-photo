/**
 * Relais de récupération pour agent-photo.
 * ---------------------------------------------------------------------------
 * Squarespace bloque l'IP de Render (connect timeout). Ce petit Worker
 * Cloudflare récupère les pages du site à la place : son IP (Cloudflare) n'est
 * pas bloquée. Le bot appelle  https://<worker>/?url=<URL_encodée>  et reçoit
 * le HTML brut.
 *
 * Sécurité : n'accepte QUE les URLs de davidmertens.com (pas un proxy ouvert).
 *
 * ===========================================================================
 * INSTALLATION (gratuit, ~10 min)
 * ===========================================================================
 * 1. Crée un compte gratuit sur https://dash.cloudflare.com (si pas déjà).
 * 2. Menu de gauche → « Workers & Pages » → « Create » → « Create Worker ».
 * 3. Donne un nom (ex. agent-photo-relay) → « Deploy ».
 * 4. Clique « Edit code », efface tout, colle CE fichier entier, puis
 *    « Deploy » à nouveau.
 * 5. Copie l'URL du Worker affichée (ex. https://agent-photo-relay.TON-SOUS-
 *    DOMAINE.workers.dev).
 * 6. Sur Render → ton service → « Environment » → ajoute une variable :
 *        FETCH_PROXY = https://agent-photo-relay.TON-SOUS-DOMAINE.workers.dev
 *    (Render redéploie automatiquement.)
 * 7. Vérifie avec la commande /diag dans Telegram → tu dois voir « ✅ Site
 *    joignable ».
 *
 * Pour désactiver le relais : supprime la variable FETCH_PROXY sur Render.
 * ===========================================================================
 */
export default {
  async fetch(request) {
    const reqUrl = new URL(request.url);
    const target = reqUrl.searchParams.get("url");
    if (!target) {
      return new Response("missing ?url=", { status: 400 });
    }

    let host;
    try {
      host = new URL(target).hostname.toLowerCase();
    } catch (e) {
      return new Response("bad url", { status: 400 });
    }

    // Allowlist stricte : uniquement le site (jamais un proxy ouvert).
    if (host !== "davidmertens.com" && !host.endsWith(".davidmertens.com")) {
      return new Response("forbidden host", { status: 403 });
    }

    // Récupère la page avec une signature de navigateur, cache 5 min côté CF.
    let resp;
    try {
      resp = await fetch(target, {
        headers: {
          "User-Agent":
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " +
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
          "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
          "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        },
        cf: { cacheTtl: 300, cacheEverything: true },
      });
    } catch (e) {
      return new Response("upstream error: " + e, { status: 502 });
    }

    // Renvoie le corps tel quel + le content-type d'origine.
    const body = await resp.arrayBuffer();
    return new Response(body, {
      status: resp.status,
      headers: {
        "Content-Type": resp.headers.get("Content-Type") || "text/html; charset=utf-8",
        "Cache-Control": "public, max-age=300",
      },
    });
  },
};
