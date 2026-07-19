"""Dashboard HTML `/stats` : visualise reach / saves / engagement des posts.

Lit `post_metrics` (rempli 24h+ après publication par le scheduler). Page
autonome (CSS inline, thème clair/sombre, responsive). Aucune dépendance
externe : juste de la string HTML rendue par Flask.
"""
import html
import json
from datetime import datetime
from typing import Optional

from db import _engagement_score, connection


def _collected_rows() -> list[dict]:
    """Posts avec metrics collectées, parsés et normalisés (IG + TH)."""
    with connection() as conn:
        rows = conn.execute(
            "SELECT platform, galerie, caption, published_at, metrics_json "
            "FROM post_metrics WHERE metrics_json IS NOT NULL "
            "ORDER BY published_at ASC"
        ).fetchall()
    out = []
    for platform, galerie, caption, published_at, metrics_json in rows:
        try:
            m = json.loads(metrics_json) if metrics_json else {}
        except (json.JSONDecodeError, TypeError):
            m = {}
        out.append({
            "platform": platform or "?",
            "galerie": galerie or "inconnue",
            "caption": caption or "",
            "published_at": published_at or "",
            "reach": int(m.get("reach") or m.get("views") or 0),
            "likes": int(m.get("like_count") or m.get("likes") or 0),
            "comments": int(m.get("comments_count") or m.get("replies") or 0),
            "saves": int(m.get("saved") or m.get("reposts") or 0),
            "eng": _engagement_score(m),
        })
    return out


def _pending_count() -> int:
    with connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM post_metrics WHERE metrics_json IS NULL"
        ).fetchone()[0]


def _avg(nums: list) -> float:
    nums = [n for n in nums if n is not None]
    return sum(nums) / len(nums) if nums else 0.0


def _fmt(n: float) -> str:
    """1234 -> '1 234', 12.3 -> '12.3'."""
    if isinstance(n, float) and not n.is_integer():
        return f"{n:.1f}"
    return f"{int(n):,}".replace(",", " ")


def _bar(value: float, vmax: float) -> str:
    pct = 0 if vmax <= 0 else max(2, round(value / vmax * 100))
    return f'<div class="bar"><span style="width:{pct}%"></span></div>'


# =========================================================================
# RENDU HTML
# =========================================================================

_CSS = """
:root{--bg:#f7f7f8;--card:#fff;--fg:#1a1a1a;--muted:#6b7280;--line:#e5e7eb;
--accent:#2563eb;--bar:#93c5fd;--barfill:#2563eb;}
@media(prefers-color-scheme:dark){:root{--bg:#0e0f11;--card:#17181b;--fg:#f3f4f6;
--muted:#9ca3af;--line:#26282d;--accent:#60a5fa;--bar:#1e3a5f;--barfill:#60a5fa;}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;padding:24px}
.wrap{max-width:960px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}h2{font-size:15px;text-transform:uppercase;
letter-spacing:.04em;color:var(--muted);margin:32px 0 12px}
.sub{color:var(--muted);margin:0 0 8px;font-size:13px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.kpi .n{font-size:28px;font-weight:700}.kpi .l{color:var(--muted);font-size:13px;margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.bar{background:var(--bar);border-radius:5px;height:9px;width:100%;overflow:hidden;margin-top:5px}
.bar span{display:block;height:100%;background:var(--barfill);border-radius:5px}
.cap{color:var(--muted);font-size:13px;max-width:420px}
.scroll{overflow-x:auto}.pill{display:inline-block;background:var(--bar);color:var(--fg);
border-radius:999px;padding:1px 8px;font-size:12px}
.empty{background:var(--card);border:1px dashed var(--line);border-radius:12px;
padding:32px;text-align:center;color:var(--muted)}
footer{color:var(--muted);font-size:12px;margin-top:32px;text-align:center}
"""


def _snippet(caption: str, n: int = 90) -> str:
    first = (caption or "").strip().split("\n", 1)[0]
    if len(first) > n:
        first = first[:n].rstrip() + "…"
    return html.escape(first) or "<i>(sans texte)</i>"


def render_stats_html() -> str:
    rows = _collected_rows()
    pending = _pending_count()
    ig = [r for r in rows if r["platform"] == "IG"]
    th = [r for r in rows if r["platform"] == "TH"]

    if not rows:
        body = (
            '<div class="empty"><b>Pas encore de données.</b><br>'
            "Les insights sont collectés automatiquement <b>~24h après</b> chaque "
            "publication.<br>"
            f"{pending} post(s) publié(s) en attente de collecte."
            "</div>"
        )
        return _page(body)

    # --- KPIs Instagram (l'objectif principal) ---
    kpis = [
        ("Posts IG", _fmt(len(ig))),
        ("Reach moyen", _fmt(_avg([r["reach"] for r in ig]))),
        ("Saves moyens", _fmt(_avg([r["saves"] for r in ig]))),
        ("Engagement moyen", _fmt(_avg([r["eng"] for r in ig])) + " %"),
    ]
    kpi_html = "".join(
        f'<div class="card kpi"><div class="n">{v}</div><div class="l">{k}</div></div>'
        for k, v in kpis
    )
    pending_note = (
        f'<p class="sub">🕒 {pending} post(s) en attente de collecte (insights à J+1).</p>'
        if pending else ""
    )

    # --- Reach par galerie (IG) ---
    gal: dict = {}
    for r in ig:
        g = gal.setdefault(r["galerie"], {"reach": [], "saves": [], "eng": [], "n": 0})
        g["reach"].append(r["reach"]); g["saves"].append(r["saves"])
        g["eng"].append(r["eng"]); g["n"] += 1
    gal_agg = [
        {"g": g, "n": d["n"], "reach": _avg(d["reach"]),
         "saves": _avg(d["saves"]), "eng": _avg(d["eng"])}
        for g, d in gal.items()
    ]
    gal_agg.sort(key=lambda d: d["reach"], reverse=True)
    gmax = max((d["reach"] for d in gal_agg), default=0)
    gal_rows = "".join(
        f'<tr><td>{html.escape(d["g"])} <span class="pill">{d["n"]}</span>'
        f'{_bar(d["reach"], gmax)}</td>'
        f'<td class="num">{_fmt(d["reach"])}</td>'
        f'<td class="num">{_fmt(d["saves"])}</td>'
        f'<td class="num">{_fmt(d["eng"])} %</td></tr>'
        for d in gal_agg
    )
    gal_html = (
        '<div class="card scroll"><table><thead><tr><th>Galerie</th>'
        '<th class="num">Reach moy.</th><th class="num">Saves moy.</th>'
        '<th class="num">Engag.</th></tr></thead><tbody>'
        f"{gal_rows}</tbody></table></div>"
    )

    # --- Top posts par reach (IG) ---
    top = sorted(ig, key=lambda r: r["reach"], reverse=True)[:8]
    top_rows = "".join(
        f'<tr><td class="cap">{_snippet(r["caption"])}<br>'
        f'<span class="pill">{html.escape(r["galerie"])}</span> '
        f'<span style="color:var(--muted)">{r["published_at"][:10]}</span></td>'
        f'<td class="num">{_fmt(r["reach"])}</td>'
        f'<td class="num">{_fmt(r["saves"])}</td>'
        f'<td class="num">{_fmt(r["likes"])}</td>'
        f'<td class="num">{_fmt(r["comments"])}</td></tr>'
        for r in top
    )
    top_html = (
        '<div class="card scroll"><table><thead><tr><th>Post</th>'
        '<th class="num">Reach</th><th class="num">Saves</th>'
        '<th class="num">Likes</th><th class="num">Comm.</th></tr></thead><tbody>'
        f"{top_rows}</tbody></table></div>"
    )

    # --- Évolution récente : reach des 15 derniers posts IG (voir la tendance) ---
    recent = ig[-15:]
    rmax = max((r["reach"] for r in recent), default=0)
    evo_rows = "".join(
        f'<tr><td style="width:120px;color:var(--muted)">{r["published_at"][:10]}</td>'
        f'<td>{_bar(r["reach"], rmax)}</td>'
        f'<td class="num">{_fmt(r["reach"])}</td></tr>'
        for r in recent
    )
    evo_html = (
        '<div class="card scroll"><table><tbody>'
        f"{evo_rows}</tbody></table></div>"
    )

    th_note = (
        f'<p class="sub">Threads : {len(th)} post(s) collecté(s) '
        f'(reach moyen {_fmt(_avg([r["reach"] for r in th]))}).</p>'
        if th else ""
    )

    body = (
        f'<p class="sub">Basé sur {len(ig)} post(s) Instagram avec insights collectés. '
        "Les vues « gratuites » viennent surtout du reach non-abonnés.</p>"
        f"{pending_note}"
        f'<div class="kpis">{kpi_html}</div>'
        "<h2>Reach par galerie</h2>"
        f"{gal_html}"
        "<h2>Meilleurs posts (par reach)</h2>"
        f"{top_html}"
        "<h2>Évolution récente (15 derniers posts)</h2>"
        f'<p class="sub">Regarde si le reach monte depuis le carrousel + SEO.</p>'
        f"{evo_html}"
        f"{th_note}"
    )
    return _page(body)


def _page(body: str, now: Optional[str] = None) -> str:
    stamp = now or datetime.now().strftime("%Y-%m-%d %H:%M")
    return (
        "<!doctype html><html lang='fr'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Agent Photo — Stats</title>"
        f"<style>{_CSS}</style></head><body><div class='wrap'>"
        "<h1>📊 Agent Photo — Statistiques</h1>"
        f"{body}"
        f"<footer>Généré le {stamp} · données collectées ~24h après publication</footer>"
        "</div></body></html>"
    )
