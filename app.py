"""
Teste ton CV™ — VB Evolution Pro
MVP web : dépose ton CV (PDF) → score d'employabilité ATS instantané + radar + verdict.
Réutilise le moteur CLARA (vision multimodale + radar) du système VBEP.

Lancer :  ../.venv/Scripts/python.exe -m uvicorn app:app --reload --port 8000
Puis ouvrir http://localhost:8000
"""
import os, sys, re, json, math, base64, time, logging, html
from collections import deque, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# Accès au moteur CLARA (clara_tools dans le dossier parent bots/)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import anthropic
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

import moteur as ct  # FT + radar — module LOCAL autonome (plus de dépendance au système de bots)

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
RADAR_LABELS = ["Lisibilité ATS", "Mots-clés ATS", "Cohérence", "Adéquation marché", "Positionnement"]


def esc(x) -> str:
    """Échappe le HTML. INDISPENSABLE pour toute donnée issue du modèle ou du
    visiteur (un CV piégé peut faire renvoyer du <script> par l'IA → XSS reflété
    dans la page ET dans l'email). Tout texte non constant DOIT passer par esc()."""
    return html.escape(str(x if x is not None else ""))

log = logging.getLogger("teste_ton_cv")

# ─── Protections anti-abus (indispensables AVANT hébergement public) ──────────
# Tous réglables sans toucher au code via variables d'environnement.
MAX_UPLOAD_MB   = int(os.getenv("MAX_UPLOAD_MB", "8"))      # taille max d'un PDF
MAX_UPLOAD      = MAX_UPLOAD_MB * 1024 * 1024
RL_PER_IP       = int(os.getenv("RL_PER_IP", "5"))          # analyses / IP / fenêtre
RL_WINDOW_SEC   = int(os.getenv("RL_WINDOW_SEC", "3600"))   # fenêtre (1 h par défaut)
RL_GLOBAL_DAY   = int(os.getenv("RL_GLOBAL_DAY", "300"))    # garde-fou budget Anthropic / jour

_hits_by_ip: dict[str, deque] = defaultdict(deque)   # ip -> timestamps récents
_global_hits: deque = deque()                        # timestamps des dernières 24 h


def _client_ip(request: Request) -> str:
    # Derrière un hébergeur/proxy, l'IP réelle est dans X-Forwarded-For.
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _rate_limited(ip: str) -> str | None:
    """Renvoie un message si la requête doit être refusée, sinon None (et l'enregistre)."""
    now = time.time()
    # Garde-fou global (protège votre crédit API contre un afflux massif)
    while _global_hits and now - _global_hits[0] > 86400:
        _global_hits.popleft()
    if len(_global_hits) >= RL_GLOBAL_DAY:
        return "Le service a atteint sa limite quotidienne d'analyses. Merci de revenir demain."
    # Limite par IP
    dq = _hits_by_ip[ip]
    while dq and now - dq[0] > RL_WINDOW_SEC:
        dq.popleft()
    if len(dq) >= RL_PER_IP:
        mins = max(1, int((RL_WINDOW_SEC - (now - dq[0])) / 60))
        return f"Vous avez déjà testé plusieurs CV récemment. Merci de réessayer dans ~{mins} min."
    dq.append(now)
    _global_hits.append(now)
    return None


app = FastAPI(title="Teste ton CV — VB Evolution Pro")

# ─── Logo VB Evolution Pro (charte : logo en en-tête PARTOUT) ─────────────────
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://teste-ton-cv.onrender.com").rstrip("/")
_LOGO_PATH = Path(__file__).parent / "logo.png"
_LOGO_BYTES = _LOGO_PATH.read_bytes() if _LOGO_PATH.exists() else b""


@app.get("/logo.png")
def logo_png():
    return Response(_LOGO_BYTES, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


# Visuels réseaux sociaux (offres.jpg, conseils.jpg, leo.png, …) servis publiquement
# → URLs directes utilisables par LÉO/Make comme image_url (Instagram exige une URL publique).
_VISUELS_DIR = Path(__file__).parent / "visuels"
_UPLOADS_DIR = _VISUELS_DIR / "uploads"     # images déposées (ex. photo Telegram) → lien public
_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)   # garantit visuels/ + uploads/
app.mount("/visuels", StaticFiles(directory=str(_VISUELS_DIR)), name="visuels")

# Jeton partagé pour protéger le dépôt d'image (à régler dans l'hébergeur via UPLOAD_TOKEN).
UPLOAD_TOKEN = os.getenv("UPLOAD_TOKEN", "vbep-leo-2026")


@app.post("/upload")
async def upload_image(file: UploadFile = File(...), token: str = Form("")):
    """Dépôt d'une image (ex. photo envoyée à LÉO dans Telegram) → renvoie un lien PUBLIC
    direct, utilisable comme image_url pour publier sur Instagram/Facebook.
    Protégé par un jeton. Stockage éphémère : suffit le temps que Meta récupère l'image."""
    if token != UPLOAD_TOKEN:
        return Response("forbidden", status_code=403)
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        return Response("too large", status_code=413)
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    base = re.sub(r"[^a-z0-9]", "", (file.filename or "img").lower())[:10] or "img"
    name = f"tg_{int(time.time())}_{base}{ext}"
    (_UPLOADS_DIR / name).write_bytes(data)
    return {"url": f"{PUBLIC_URL}/visuels/uploads/{name}"}

# ─── Mesure conversion (MAYA : « données d'abord ») ───────────────────────────
# Compteurs en mémoire + journal. Étape « rapide » ; l'étape structurelle = écrire
# ces chiffres dans la table Airtable « KPI & Pilotage » pour le bilan mensuel.
_metrics = {"analyses": 0, "leads": 0, "clics_di": 0}


def _kpi_bump(d_aa: int = 0, d_leads: int = 0, d_clics: int = 0):
    """Incrémente la ligne KPI mensuelle du tunnel « Teste ton CV » dans Airtable
    (table « KPI & Pilotage »). Crée la ligne du mois si absente, sinon l'incrémente —
    les compteurs PERSISTENT donc au-delà des redémarrages de l'app. Best-effort :
    ne bloque jamais l'UX (toute erreur est seulement journalisée)."""
    key = os.getenv("AIRTABLE_API_KEY")
    base = os.getenv("AIRTABLE_BASE_ID", "")
    if not key or not base:
        return
    import urllib.parse
    url = f"https://api.airtable.com/v0/{base}/" + urllib.parse.quote("KPI & Pilotage", safe="")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    mois = datetime.now(timezone.utc).strftime("%Y-%m")
    nom = f"Tunnel Teste ton CV — {mois}"
    try:
        formula = "{Nom KPI/Indicateur}='" + nom + "'"
        q = f"{url}?maxRecords=1&filterByFormula=" + urllib.parse.quote(formula)
        rec_id, aa, leads, clics = None, 0, 0, 0
        rg = requests.get(q, headers=headers, timeout=8)
        if rg.status_code < 300:
            recs = rg.json().get("records", [])
            if recs:
                rec_id = recs[0]["id"]
                f = recs[0].get("fields", {})
                aa = int(f.get("Nombre de AA", 0) or 0)
                leads = int(f.get("Valeur actuelle", 0) or 0)
                clics = int(f.get("Total cumulé", 0) or 0)
        aa += d_aa
        leads += d_leads
        clics += d_clics
        fields = {
            "Nom KPI/Indicateur": nom, "Mois": mois,
            "Nombre de AA": aa, "Valeur actuelle": leads, "Total cumulé": clics,
            "Description": (f"Tunnel Teste ton CV — {mois} (mise à jour auto par l'app) : "
                            f"{aa} analyses · {leads} leads consentis · {clics} clics DI."),
            "Date dernière mise à jour": datetime.now(timezone.utc).date().isoformat(),
        }
        if rec_id:
            requests.patch(f"{url}/{rec_id}", headers=headers,
                           json={"fields": fields, "typecast": True}, timeout=8)
        else:
            requests.post(url, headers=headers,
                          json={"fields": fields, "typecast": True}, timeout=8)
    except Exception:
        log.exception("MAJ KPI tunnel échouée (non bloquant)")


@app.get("/go/di", response_class=HTMLResponse)
def go_di(e: str = ""):
    """Information précontractuelle + acceptation des CGV et du droit de rétractation
    AVANT le paiement (Code de la consommation, art. L221-5 / L221-18). Le paiement
    effectif (redirection Stripe) se fait ensuite via /go/di/pay."""
    import urllib.parse
    e_q = ("?e=" + urllib.parse.quote(e)) if (e and "@" in e) else ""
    return PAGE_HEAD + f"""
<div class="card legal">
  <div class=h1band>Diagnostic Invisibilité™ — 99 €</div>
  <p style="margin-top:12px">Avant de régler, voici l'essentiel (information précontractuelle) :</p>
  <ul style="font-size:14px;line-height:1.7">
    <li><b>Prestation :</b> audit complet de votre employabilité (CV + présence en ligne + marché de votre métier), restitué sous 48-72 h.</li>
    <li><b>Prix :</b> 99 € net — TVA non applicable (art. 293 B du CGI). Paiement sécurisé via Stripe.</li>
    <li><b>Droit de rétractation :</b> 14 jours à compter de votre commande (art. L221-18). Si vous demandez le démarrage immédiat et que la prestation est pleinement réalisée avant la fin de ce délai, ce droit ne s'applique plus (art. L221-28).</li>
    <li><b>Médiation :</b> en cas de litige non résolu, médiateur de la consommation SMP — <a href="https://www.mediateur-consommation-smp.fr/" target=_blank>mediateur-consommation-smp.fr</a>.</li>
  </ul>
  <label class=consent><input type=checkbox id=cgv>
    <span>J'ai lu et j'accepte les <a href="/cgv" target=_blank>conditions générales de vente</a> et l'information sur le droit de rétractation.</span></label>
  <label class=consent><input type=checkbox id=start>
    <span>Je demande le <b>démarrage immédiat</b> de la prestation (restituée sous 48-72 h) et je reconnais qu'une fois celle-ci pleinement exécutée avant la fin du délai de 14 jours, je perdrai mon droit de rétractation (art. L221-28).</span></label>
  <div style="text-align:center;margin-top:6px">
    <a id=payer class="btn off" href="/go/di/pay{e_q}">Procéder au paiement — 99 € →</a>
  </div>
  <p class=note><a href="/" style="color:#7A6075">← Retour</a></p>
</div>
<style>.btn.off{{pointer-events:none;background:#A892B5;cursor:not-allowed}}</style>
<script>
var BASE="/go/di/pay{e_q}";
function maj(){{
  var ok=document.getElementById('cgv').checked, s=document.getElementById('start').checked, p=document.getElementById('payer');
  p.classList.toggle('off',!ok);
  p.href=BASE+(s?(BASE.indexOf('?')>-1?'&':'?')+'start=1':'');
}}
document.getElementById('cgv').onchange=maj; document.getElementById('start').onchange=maj; maj();
</script>""" + PAGE_FOOT


@app.get("/go/di/pay")
def go_di_pay(e: str = "", start: str = ""):
    """Redirection effective vers Stripe, après acceptation des CGV. Compte le clic DI.
    `e` = email du visiteur → pré-rempli au checkout + client_reference_id (attribution).
    `start=1` = le client a demandé expressément le démarrage immédiat (renonciation au
    droit de rétractation à exécution complète, art. L221-28) — tracé pour preuve."""
    _metrics["clics_di"] += 1
    log.info("Clic CTA DI #%s → Stripe (demarrage_anticipe=%s)", _metrics["clics_di"], start == "1")
    if start == "1":
        log.info("DI: demande EXPRESSE de demarrage immediat (renonciation retractation a execution complete) - email=%s", e or "?")
    _kpi_bump(d_clics=1)  # persiste le clic DI dans la table KPI & Pilotage
    import urllib.parse
    url = OFFRES["DI"][1]
    if e and "@" in e:
        sep = "&" if "?" in url else "?"
        url += (f"{sep}prefilled_email=" + urllib.parse.quote(e)
                + "&client_reference_id=" + urllib.parse.quote(e))
    return RedirectResponse(url, status_code=302)


@app.get("/cgv", response_class=HTMLResponse)
def cgv():
    """CGV résumées (version web). Les CGV complètes sont remises avec le contrat."""
    return PAGE_HEAD + """
<div class="card legal">
  <div class=h1band>Conditions générales de vente</div>
  <p class=note>VB Evolution Pro — Valérie Blanchard, micro-entreprise, SIRET 99523129700012, 26 rue de Touraine, 41300 Salbris · TVA non applicable (art. 293 B du CGI).</p>
  <h2>1. Prestations &amp; prix</h2>
  <p>Analyse Augmentée™ : gratuite. Diagnostic Invisibilité™ : 99 € net. Reposition Pro™ : 490 € net. Reprends Ta Place™ : 1 600 € net. Prestations réalisées à distance.</p>
  <h2>2. Paiement</h2>
  <p>Par carte (Stripe) ou virement ; paiement échelonné possible. Tout montant déjà réglé est déduit de l'offre supérieure.</p>
  <h2>3. Droit de rétractation</h2>
  <p>14 jours à compter de la commande (art. L221-18). En cas de demande expresse de démarrage immédiat, la rétractation n'est plus possible une fois la prestation pleinement exécutée (art. L221-28) ; si vous vous rétractez en cours d'exécution, vous réglez la part déjà réalisée (art. L221-25).</p>
  <h2>4. Garantie Reposition Pro™</h2>
  <p>80 % remboursé si insatisfaction signalée sous 14 jours ; prolongation gratuite si aucun entretien à 90 jours (sous réserve des actions réalisées). Le Prestataire est tenu d'une obligation de moyens, non de résultat.</p>
  <h2>5. Données personnelles</h2>
  <p>Traitement conforme au RGPD — voir la <a href="/confidentialite">politique de confidentialité</a>.</p>
  <h2>6. Réclamations &amp; médiation</h2>
  <p>valerie.blanchard@vb-evopro.fr · 02 54 97 96 38. À défaut de solution amiable, médiateur de la consommation SMP — <a href="https://www.mediateur-consommation-smp.fr/" target=_blank>mediateur-consommation-smp.fr</a>.</p>
  <h2>7. Droit applicable</h2>
  <p>Droit français.</p>
  <p class=note style="margin-top:14px">Version résumée — les CGV complètes sont remises avec le contrat de prestation de services.</p>
  <p style="text-align:center;margin-top:10px"><a href="/">← Retour</a></p>
</div>""" + PAGE_FOOT


@app.get("/retractation", response_class=HTMLResponse)
def retractation():
    """Droit de rétractation + formulaire type (Code de la consommation, art. L221-18
    à L221-28, formulaire annexe à l'art. R221-1). Accessible depuis le pied de page."""
    return PAGE_HEAD + """
<div class="card legal">
  <div class=h1band>Droit de rétractation</div>
  <p style="margin-top:12px">Vous disposez d'un délai de <b>14 jours</b> à compter de la conclusion du contrat pour exercer votre droit de rétractation, sans avoir à motiver votre décision (art. L221-18 du Code de la consommation). En cas de rétractation, vous êtes remboursé(e) de tout paiement sous 14 jours.</p>
  <h2>Comment vous rétracter</h2>
  <p>Notifiez-nous votre décision par une déclaration dénuée d'ambiguïté, avant l'expiration du délai :</p>
  <ul style="font-size:14px;line-height:1.7">
    <li>par email : <a href="mailto:valerie.blanchard@vb-evopro.fr">valerie.blanchard@vb-evopro.fr</a> ;</li>
    <li>par courrier : VB Evolution Pro — Valérie Blanchard, 26 rue de Touraine, 41300 Salbris.</li>
  </ul>
  <h2>Exception — prestation démarrée immédiatement</h2>
  <p>Si vous demandez expressément le <b>démarrage immédiat</b> de la prestation et qu'elle est <b>pleinement exécutée</b> avant la fin du délai, le droit de rétractation ne s'applique plus (art. L221-28). Si vous vous rétractez en cours d'exécution, vous réglez la part déjà réalisée (art. L221-25).</p>
  <div style="background:#EDE5F2;border-left:4px solid #CAB6D2;border-radius:0 10px 10px 0;padding:15px 18px;margin-top:14px;font-size:14px;line-height:1.7">
    <b>Formulaire type de rétractation</b>
    <div style="color:#7A6075;font-size:12px">(À compléter et nous renvoyer uniquement si vous souhaitez vous rétracter.)</div>
    <p style="margin:10px 0 0">À l'attention de VB Evolution Pro — Valérie Blanchard, 26 rue de Touraine, 41300 Salbris — valerie.blanchard@vb-evopro.fr :</p>
    <p style="margin:10px 0 0">Je vous notifie par la présente ma rétractation du contrat portant sur la prestation de services ci-dessous :</p>
    <p style="margin:10px 0 0">— Commandée le : ____________________<br>
    — Nom du (des) consommateur(s) : ____________________<br>
    — Adresse du (des) consommateur(s) : ____________________<br>
    — Date : ____________________<br>
    — Signature (uniquement en cas de notification papier) : ____________________</p>
  </div>
  <p class=note style="margin-top:14px">Voir aussi les <a href="/cgv">conditions générales de vente</a>.</p>
  <p style="text-align:center;margin-top:10px"><a href="/">← Retour</a></p>
</div>""" + PAGE_FOOT

# ─────────────────────────────────────────────────────────────────────────────
# Moteur d'analyse : 1 appel Claude vision sur le PDF → scores structurés
# ─────────────────────────────────────────────────────────────────────────────
PROMPT = """Tu es CLARA, experte en employabilité et systèmes ATS pour VB Evolution Pro.
Analyse ce CV (contenu ET mise en page visuelle telle que la verrait un ATS).

Retourne UNIQUEMENT un objet JSON valide, sans aucun texte autour, avec EXACTEMENT ces clés :
{
  "poste_detecte": "intitulé du poste visé déduit du CV",
  "code_rome": "code ROME France Travail le plus proche du poste (1 lettre + 4 chiffres, ex. M1502). Si incertain, le plus probable.",
  "presentation": <0-10, lisibilité ATS de la mise en page (structure, format, sections)>,
  "mots_cles": <0-10, présence des mots-clés métier recherchés par les recruteurs>,
  "coherence": <0-10, cohérence du parcours, gaps, progression>,
  "marche": <0-10, adéquation du profil au marché de l'emploi actuel>,
  "positionnement": <0-10, clarté de la valeur/du positionnement en 6 secondes>,
  "score_global": <0-100, score d'employabilité global>,
  "stade_advp": "Exploration | Cristallisation | Spécification | Réalisation — stade de maturité du projet déduit du CV. Signaux : Exploration=projet flou, parcours dispersé, pas de cap ; Cristallisation=2-3 directions possibles, hésitation ; Spécification=cap choisi, manque outils/plan ; Réalisation=poste cible clair, CV à optimiser.",
  "verdict": "2-3 phrases franches et bienveillantes (vouvoiement) sur l'employabilité",
  "frein_principal": "le frein n°1 d'invisibilité, en une phrase",
  "points_forts": ["3 points forts max"],
  "mots_cles_manquants": ["4 mots-clés ATS manquants max"],
  "bloqueurs_ats": ["0 à 4 éléments de MISE EN PAGE qui empêchent un logiciel ATS de lire le CV : colonnes multiples, tableaux, zones de texte, photo, en-tête/pied de page, icônes, police exotique, infos dans une image… Chaque élément formulé en une courte phrase concrète. Liste VIDE si le CV est propre pour les ATS."],
  "actions": [{"action": "action concrète et précise à faire", "impact": "bénéfice attendu en une formule courte"}],
  "nom": "NOM de famille (extrait du CV), \"\" si absent",
  "prenom": "Prénom (extrait du CV), \"\" si absent",
  "telephone": "téléphone du CV, \"\" si absent",
  "code_postal": "code postal du CV, \"\" si absent",
  "secteur": "secteur / domaine d'activité visé, \"\" si inconnu",
  "niveau_experience": "ex. Junior / Confirmé / Senior / 20 ans+, \"\" si inconnu",
  "type_contrat": "type de contrat visé (CDI, CDD, freelance…), \"\" si non indiqué",
  "competences_cles": ["5 à 8 compétences clés issues du CV"],
  "situation_actuelle": "En poste / En recherche / Reconversion…, \"\" si inconnu",
  "duree_recherche": "durée de recherche si mentionnée, sinon \"\"",
  "candidatures_sans_reponse": "ordre de grandeur si mentionné, sinon \"\"",
  "potentiel": "🔥 Chaud | 🌤 Tiède | 🧊 Froid — potentiel de conversion estimé (Chaud = besoin fort + projet clair + prêt à agir ; Tiède = besoin réel mais projet à mûrir ; Froid = peu de signaux d'achat)"
}
Les champs d'identité et de profil sont extraits UNIQUEMENT du CV ; laisse "" si absent (n'invente JAMAIS).
N'extrais JAMAIS de données sensibles (numéro de sécurité sociale, état de santé, adresse de rue complète).
La clé "actions" contient 3 à 5 actions PRIORITAIRES pour les 30 prochains jours, classées par impact × facilité
(la n°1 = le plus fort levier le plus simple). Elles découlent du frein principal et des mots-clés manquants.
Tu analyses UNIQUEMENT le CV (ni LinkedIn, ni réseau, ni stratégie de candidature) : c'est un aperçu.
Sois exigeant et concret. N'invente AUCUNE statistique chiffrée (pas de pourcentage ni de chiffre non sourcé). Le score_global doit être cohérent avec la moyenne des 5 axes (x10).
Ne recommande PAS d'offre toi-même : l'offre est calculée à partir du score et du stade."""


def analyser_cv(pdf_bytes: bytes) -> dict:
    b64 = base64.standard_b64encode(pdf_bytes).decode()
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    msg = client.messages.create(
        model=MODEL, max_tokens=3000,
        messages=[{"role": "user", "content": [
            {"type": "document",
             "source": {"type": "base64", "media_type": "application/pdf", "data": b64}},
            {"type": "text", "text": PROMPT},
        ]}],
    )
    txt = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    m = re.search(r"\{.*\}", txt, re.DOTALL)
    if not m:
        raise ValueError("Réponse modèle non-JSON")
    return json.loads(m.group(0))


def radar_data_uri(d: dict, nom: str) -> str:
    png = ct._generer_radar_png(
        float(d.get("presentation", 0)), float(d.get("mots_cles", 0)),
        float(d.get("coherence", 0)), float(d.get("marche", 0)),
        float(d.get("positionnement", 0)),
        int(d.get("score_global", 0)), nom, labels=RADAR_LABELS,
    )
    if not png:
        return ""
    return "data:image/png;base64," + base64.b64encode(png).decode()


# ─────────────────────────────────────────────────────────────────────────────
# Marché réel France Travail : l'axe « Marché » est SOURCÉ, pas estimé par l'IA.
# Claude propose un code ROME → on le VALIDE contre le référentiel FT → on compte
# les offres actives au niveau national (en-tête Content-Range de l'API search).
# Réutilise le token OAuth FT mis en cache par clara_tools.
# ─────────────────────────────────────────────────────────────────────────────
FT_SEARCH_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"
FT_REFERENTIEL_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/referentiel/metiers"
_ROME_RE = re.compile(r"^[A-N]\d{4}$")
_ref_rome: dict = {}  # cache {code ROME -> libellé}, chargé une fois


def _ft_referentiel() -> dict:
    """Index {code ROME -> libellé} du référentiel métiers FT (chargé une seule fois)."""
    if _ref_rome:
        return _ref_rome
    try:
        token = ct._ft_token()
        r = requests.get(FT_REFERENTIEL_URL,
                         headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                         timeout=15)
        if r.status_code == 200:
            for m in r.json():
                _ref_rome.setdefault(m.get("code", ""), m.get("libelle", ""))
    except Exception:
        log.exception("Référentiel ROME FT indisponible")
    return _ref_rome


def _rome_depuis_poste(poste: str) -> str:
    """Fallback : déduit un code ROME valide depuis l'intitulé via la recherche FT."""
    try:
        r = ct.rechercher_code_rome(poste)
    except Exception:
        return ""
    ref = _ft_referentiel()
    for item in (r.get("resultats") or []):
        c = (item.get("code_rome") or "").strip().upper()
        if c in ref:
            return c
    return ""


def _nb_offres_national(code_rome: str):
    """Nombre total d'offres FT actives pour un code ROME (lu dans Content-Range), ou None."""
    try:
        token = ct._ft_token()
        resp = requests.get(FT_SEARCH_URL,
                            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                            params={"codeROME": code_rome, "range": "0-1"}, timeout=20)
        if resp.status_code in (200, 206):
            m = re.search(r"/(\d+)\s*$", resp.headers.get("Content-Range", ""))
            if m:
                return int(m.group(1))
    except Exception:
        log.exception("Comptage offres FT échoué")
    return None


def _score_marche(nb: int) -> int:
    """Volume d'offres national → score 0-10 (échelle log : ~100→4, ~1k→8, ~10k→10)."""
    return max(0, min(10, round(2 + 2 * math.log10(nb + 1))))


def _commune_depuis_ville(ville: str):
    """(code INSEE, libellé FT) de la ville, ou (None, None). Réutilise le référentiel communes FT."""
    if not (ville or "").strip():
        return None, None
    try:
        comms = (ct.rechercher_commune_ft(ville) or {}).get("communes") or []
        if comms:
            return comms[0].get("code", ""), comms[0].get("libelle", "")
    except Exception:
        log.exception("Commune FT introuvable")
    return None, None


def _nb_offres_zone(code_rome: str, commune: str, rayon: int = 30):
    """Nombre d'offres FT actives pour un code ROME dans un rayon autour d'une commune, ou None."""
    try:
        token = ct._ft_token()
        resp = requests.get(FT_SEARCH_URL,
                            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                            params={"codeROME": code_rome, "commune": commune,
                                    "distance": rayon, "range": "0-1"}, timeout=20)
        if resp.status_code in (200, 206):
            m = re.search(r"/(\d+)\s*$", resp.headers.get("Content-Range", ""))
            if m:
                return int(m.group(1))
    except Exception:
        log.exception("Comptage offres zone FT échoué")
    return None


def analyser_marche_ft(code_rome_ia: str, poste: str, ville: str = "") -> dict:
    """
    Renvoie l'état du marché sourcé FT :
    {sourced, code_rome, libelle, nb_offres, score, [nb_local, ville_libelle, rayon_km]}.
    Le score reste fondé sur le NATIONAL (stable) ; le nb local (30 km) est un
    complément de personnalisation affiché en plus, si la ville est fournie.
    sourced=False si aucun code ROME validable ou API FT indisponible → l'IA fait foi.
    """
    code = (code_rome_ia or "").strip().upper()
    if not (_ROME_RE.match(code) and code in _ft_referentiel()):
        code = _rome_depuis_poste(poste)
    if not code:
        return {"sourced": False}
    nb = _nb_offres_national(code)
    libelle = _ft_referentiel().get(code, "")
    if nb is None:
        return {"sourced": False, "code_rome": code, "libelle": libelle}
    res = {"sourced": True, "code_rome": code, "libelle": libelle,
           "nb_offres": nb, "score": _score_marche(nb)}
    # Marché LOCAL (30 km autour de la ville), EN PLUS du national, si ville fournie.
    commune, ville_lib = _commune_depuis_ville(ville)
    if commune:
        nb_local = _nb_offres_zone(code, commune, 30)
        if nb_local is not None:
            res.update({"nb_local": nb_local, "ville_libelle": ville_lib, "rayon_km": 30})
    return res


# Liens de paiement / offres (depuis la charte VBEP)
OFFRES = {
    "AA":  ("Analyse Augmentée™ — GRATUITE",       "#"),
    "DI":  ("Diagnostic Invisibilité™ — 99 €",     "https://buy.stripe.com/6oU5kx4O71aQ5Be0tU3Je08"),
    "RP":  ("Reposition Pro™ — 490 €",             "#"),
    "RTP": ("Reprends Ta Place™ — 1 600 €",        "#"),
}

# ─────────────────────────────────────────────────────────────────────────────
# Recommandation d'offre selon METHODE.md : on CROISE le score et le stade ADVP.
# Seuils score : 80+→AA (optim.), 60-79→DI, 40-59→RP, <40→RTP.
# Stade ADVP  : Réalisation→DI, Spécification/Cristallisation→RP, Exploration→RTP.
# Règle de croisement : on retient l'offre la PLUS intensive des deux
# (« le stade, pas l'âge » — le stade peut tirer l'offre vers le haut).
# ─────────────────────────────────────────────────────────────────────────────
_OFFRE_RANG = {"AA": 0, "DI": 1, "RP": 2, "RTP": 3}
_RANG_OFFRE = {r: o for o, r in _OFFRE_RANG.items()}
_OFFRE_PAR_STADE = {
    "exploration": "RTP", "cristallisation": "RP",
    "specification": "RP", "spécification": "RP",
    "realisation": "DI", "réalisation": "DI",
}


def _offre_selon_score(score: int) -> str:
    if score >= 80:
        return "AA"
    if score >= 60:
        return "DI"
    if score >= 40:
        return "RP"
    return "RTP"


def _offre_methode(score: int, stade: str) -> str:
    """Offre recommandée = la plus intensive entre le score et le stade ADVP."""
    rangs = [_OFFRE_RANG[_offre_selon_score(int(score))]]
    stade_off = _OFFRE_PAR_STADE.get((stade or "").strip().lower())
    if stade_off:
        rangs.append(_OFFRE_RANG[stade_off])
    return _RANG_OFFRE[max(rangs)]


# Montant potentiel du lead = valeur de l'offre méthode (pour le pipeline CRM).
OFFRE_MONTANT = {"AA": 0, "DI": 99, "RP": 490, "RTP": 1600}
# Délai avant la 1ʳᵉ relance email automatique (jours). La séquence suivante est
# pilotée par le scénario Make qui lit le champ « Date prochaine action ».
RELANCE_J1 = int(os.getenv("RELANCE_J1", "2"))

# ─────────────────────────────────────────────────────────────────────────────
# Front : charte VBEP (violet, Comic Sans titres, fond clair)
# ─────────────────────────────────────────────────────────────────────────────
PAGE_HEAD = """<!doctype html><html lang=fr><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Teste ton CV™ — VB Evolution Pro</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>%F0%9F%8E%AF</text></svg>">
<meta name="description" content="Bien plus qu'un scanner de CV : votre score de visibilité ET le marché réel de votre métier (France Travail). Diagnostic d'employabilité par IA, en 30 secondes.">
<meta property="og:type" content="website">
<meta property="og:title" content="Teste ton CV™ — VB Evolution Pro">
<meta property="og:description" content="Bien plus qu'un scanner de CV : votre score + le marché réel de votre métier. Diagnostic d'employabilité en 30 secondes.">
<meta property="og:site_name" content="VB Evolution Pro">
<meta name="twitter:card" content="summary">
<style>
@import url('https://fonts.googleapis.com/css2?family=Open+Sans:ital,wght@0,400;0,600;1,400&display=swap');
*{box-sizing:border-box} body{font-family:'Open Sans',sans-serif;background:#FEFEFD;color:#3A2535;margin:0}
h1,h2,.cs{font-family:'Comic Sans MS','Open Sans',cursive}
.wrap{max-width:760px;margin:0 auto;padding:28px 20px 60px}
.head{display:flex;align-items:center;gap:14px;border-bottom:3px solid #CAB6D2;padding-bottom:14px}
.head h1{color:#784171;margin:0;font-size:26px}
.sub{color:#7A6075;font-size:14px}
.card{background:#fff;border:1px solid #EDE5F2;border-radius:16px;padding:26px;margin-top:22px;box-shadow:0 4px 18px rgba(120,65,113,.06)}
.h1band{background:#CAB6D2;color:#784171;border-radius:10px;padding:10px 16px;font-weight:700}
.drop{border:2px dashed #CAB6D2;border-radius:14px;padding:34px;text-align:center;background:#FBF7FD}
input[type=email],input[type=url],input[type=text]{width:100%;padding:12px;border:1px solid #CAB6D2;border-radius:10px;margin:10px 0;font-size:15px}
.btn{background:#784171;color:#fff;border:0;border-radius:30px;padding:14px 30px;font-size:16px;font-weight:700;cursor:pointer}
.btn:hover{background:#5C2D6E}
.score{font-size:60px;font-weight:800;color:#784171;line-height:1}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px;align-items:center}
.axis{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px dashed #EDE5F2}
.bar{height:9px;background:#EEF8EC;border-radius:6px;overflow:hidden;width:130px}
.bar>i{display:block;height:100%;background:#9B6FB0}
.tag{display:inline-block;background:#EDE5F2;color:#784171;border-radius:20px;padding:4px 12px;margin:3px;font-size:13px}
.consent{display:flex;gap:8px;align-items:flex-start;font-size:13px;color:#7A6075;margin:6px 0 14px;line-height:1.45}
.consent input{margin-top:3px}
#loading{display:none;position:fixed;inset:0;background:rgba(254,254,253,.96);z-index:9999;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:24px}
.spin{width:54px;height:54px;border:6px solid #EDE5F2;border-top-color:#784171;border-radius:50%;animation:sp 1s linear infinite;margin-bottom:18px}
@keyframes sp{to{transform:rotate(360deg)}}
.legal{font-size:14px;line-height:1.6}.legal h2{background:#EEF8EC;color:#784171;border-radius:8px;padding:8px 14px;font-size:17px}
.legal a{color:#784171}
.cta{background:#784171;color:#fff;border-radius:14px;padding:18px;text-align:center;margin-top:18px}
.cta a{color:#fff;font-weight:700}
.foot{height:8px;background:linear-gradient(90deg,#E8A0BF 33%,#9B6FB0 33% 66%,#8DBf8a 66%);border-radius:6px;margin-top:26px}
.note{color:#7A6075;font-size:12px;text-align:center;margin-top:10px}
@media(max-width:640px){.grid{grid-template-columns:1fr}}
</style></head><body><div class=wrap>
<div class=head><img src="/logo.png" alt="VB Evolution Pro" width="44" height="44" style="border-radius:8px;display:block">
<div><h1>Teste ton CV™</h1><div class=sub>VB Evolution Pro · Diagnostic d'employabilité par IA</div></div></div>"""

PAGE_FOOT = ('<div class=foot></div><div class=note>VB Evolution Pro · vb-evopro.fr · Valérie Blanchard, CIP · '
             '<a href="/confidentialite" style="color:#7A6075">Confidentialité & mentions légales</a> · '
             '<a href="/cgv" style="color:#7A6075">Conditions générales</a> · '
             '<a href="/retractation" style="color:#7A6075">Rétractation</a></div></div></body></html>')


# ─────────────────────────────────────────────────────────────────────────────
# Document Analyse Augmentée™ — généré pour chaque visiteur (sans RDV).
# Document autonome (charte VBEP intégrée), servi à l'écran ET envoyé par email.
# ─────────────────────────────────────────────────────────────────────────────
AA_STYLE = """<style>
@import url('https://fonts.googleapis.com/css2?family=Open+Sans:ital,wght@0,400;0,600;1,400&display=swap');
*{box-sizing:border-box} body{font-family:'Open Sans',sans-serif;background:#FEFEFD;color:#3A2535;margin:0}
h1,.cs{font-family:'Comic Sans MS','Open Sans',cursive}
.wrap{max-width:760px;margin:0 auto;padding:28px 20px 60px}
.head{display:flex;align-items:center;gap:14px;border-bottom:3px solid #CAB6D2;padding-bottom:14px}
.head h1{color:#784171;margin:0;font-size:26px}
.sub{color:#7A6075;font-size:14px}
.card{background:#fff;border:1px solid #EDE5F2;border-radius:16px;padding:26px;margin-top:22px}
.h1band{background:#CAB6D2;color:#784171;border-radius:10px;padding:10px 16px;font-weight:700;margin-bottom:8px}
.score{font-size:60px;font-weight:800;color:#784171;line-height:1}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px;align-items:center}
.axis{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px dashed #EDE5F2}
.bar{height:9px;background:#EEF8EC;border-radius:6px;overflow:hidden;width:130px}.bar>i{display:block;height:100%;background:#9B6FB0}
.tag{display:inline-block;background:#EDE5F2;color:#784171;border-radius:20px;padding:4px 12px;margin:3px;font-size:13px}
table.plan{width:100%;border-collapse:collapse;margin:10px 0;font-size:14px}
table.plan th{background:#CAB6D2;color:#784171;font-family:'Comic Sans MS',cursive;padding:8px 11px;text-align:left;border:1px solid #D5CCD9}
table.plan td{padding:8px 11px;border:1px solid #D5CCD9;vertical-align:top}
table.plan tr:nth-child(even) td{background:#EEF8EC}
.cta{background:#784171;color:#fff;border-radius:14px;padding:18px;text-align:center;margin-top:18px}
.cta a{color:#fff;font-weight:700}
.btn{background:#784171;color:#fff;border:0;border-radius:30px;padding:14px 30px;font-size:16px;font-weight:700;cursor:pointer}
.foot{height:8px;background:linear-gradient(90deg,#E8A0BF 33%,#9B6FB0 33% 66%,#8DBf8a 66%);border-radius:6px;margin-top:26px}
.note{color:#7A6075;font-size:12px;text-align:center;margin-top:10px}
@media print{.noprint{display:none!important}.card{border:0}}
@media(max-width:640px){.grid{grid-template-columns:1fr}}
</style>"""


def _aa_axes_html(d: dict) -> str:
    axes = [("Lisibilité ATS", d.get("presentation", 0)), ("Mots-clés ATS", d.get("mots_cles", 0)),
            ("Cohérence parcours", d.get("coherence", 0)), ("Adéquation marché", d.get("marche", 0)),
            ("Clarté positionnement", d.get("positionnement", 0))]
    return "".join(
        f'<div class=axis><span>{n}</span><span style="display:flex;align-items:center;gap:10px">'
        f'<span class=bar><i style="width:{float(v)*10:.0f}%"></i></span><b>{float(v):.0f}/10</b></span></div>'
        for n, v in axes)


def _aa_marche_html(marche: dict) -> str:
    if not marche.get("sourced"):
        return ('<div class=note style="text-align:left">Axe « Marché » estimé par l\'IA '
                '(données France Travail momentanément indisponibles).</div>')
    nb = f"{marche['nb_offres']:,}".replace(",", " ")
    local = ""
    if marche.get("nb_local") is not None and marche.get("ville_libelle"):
        nbl = f"{marche['nb_local']:,}".replace(",", " ")
        ville = esc(marche["ville_libelle"].title())
        local = (f'<br>📍 dont <b>{nbl}</b> offre(s) dans un rayon de {marche.get("rayon_km", 30)} km '
                 f'autour de <b>{ville}</b>')
    return ('<div style="background:#EEF8EC;border-left:4px solid #8DBf8a;border-radius:10px;padding:14px 16px;margin-top:14px">'
            '<div class=cs style="color:#2F6B3A;font-weight:700;margin-bottom:4px">📊 Votre marché, en temps réel</div>'
            f'<b>{nb}</b> offres actives en France pour <b>{esc(marche["libelle"])}</b>{local} '
            f'<span class=sub>(ROME {esc(marche["code_rome"])} · France Travail, aujourd\'hui)</span>'
            '<div class=note style="text-align:left;margin-top:5px">Ce n\'est pas une comparaison à une offre que vous collez : '
            'c\'est le <b>volume réel d\'offres de votre métier</b> — ce que les simples scanners de CV ne montrent jamais.</div></div>')


def _aa_bloqueurs_html(d: dict) -> str:
    """Amorce CLARA : 1 bloqueur ATS visuel concret montré (preuve d'expertise),
    le reste verrouillé → renvoyé au DI. Rien si le CV est propre."""
    bl = [x for x in d.get("bloqueurs_ats", []) if x]
    if not bl:
        return ""
    extra = len(bl) - 1
    lock = (f' <span class=tag style="opacity:.6">🔒 +{extra} autre(s) dans le DI</span>'
            if extra > 0 else "")
    return ('<div style="background:#FBF3F6;border-left:4px solid #C9779B;border-radius:10px;'
            'padding:12px 16px;margin-top:14px"><b>🧱 Bloqueur ATS repéré :</b> '
            f'{esc(bl[0])}{lock}'
            '<div class=note style="text-align:left;margin-top:4px">Ce type d\'élément empêche les '
            'logiciels de tri de lire correctement votre CV.</div></div>')


def _aa_actions_html(actions) -> str:
    items = [x for x in (actions or []) if isinstance(x, dict) and x.get("action")][:5]
    if not items:
        return ""
    # AMORCE : on offre l'action n°1 en entier (preuve que les conseils marchent),
    # puis on VERROUILLE les suivantes (le plan complet = valeur du DI, on ne le
    # donne pas gratuitement → on crée le manque au lieu de le combler).
    a0 = items[0]
    rows = (f'<tr><td style="text-align:center;font-weight:700;color:#784171">1</td>'
            f'<td>{esc(a0.get("action",""))}</td><td>{esc(a0.get("impact",""))}</td>'
            f'<td style="text-align:center">☐</td></tr>')
    for i in range(2, len(items) + 1):
        rows += (f'<tr style="opacity:.5"><td style="text-align:center;font-weight:700;color:#784171">{i}</td>'
                 f'<td>🔒 Action détaillée dans le Diagnostic Invisibilité™</td>'
                 f'<td>—</td><td style="text-align:center">☐</td></tr>')
    return ('<table class=plan><tr><th>#</th><th>Action prioritaire (30 jours)</th>'
            '<th>Impact attendu</th><th>Fait&nbsp;?</th></tr>' + rows + '</table>')


def aa_html_standalone(d: dict, radar: str, marche: dict, off_code: str, for_email: bool = False, email: str = "") -> str:
    """Document Analyse Augmentée™ complet et autonome (charte VBEP intégrée)."""
    # Côté VISITEUR, l'unique prochaine étape proposée est toujours le DI à 99 € :
    # produit d'entrée sans friction (l'aperçu gratuit EST déjà l'Analyse Augmentée™).
    # Les offres supérieures (RP/RTP) se vendent dans la conversation déclenchée par
    # le DI, jamais en self-service depuis un outil gratuit. (off_code reste, lui,
    # enregistré dans le CRM pour que Valérie voie le vrai potentiel du lead.)
    di_label, di_link = OFFRES["DI"]
    # Email : lien Stripe direct (pas de host). Web : passe par /go/di (clic traçable).
    # Lien CTA avec attribution : web → /go/di?e=email (clic tracé + pré-remplissage),
    # email → lien Stripe direct avec email pré-rempli (pas de host en email).
    import urllib.parse as _up
    _e = _up.quote(email) if (email and "@" in email) else ""
    if for_email:
        cta_href = di_link + (("?prefilled_email=" + _e) if _e else "")
    else:
        cta_href = "/go/di" + (("?e=" + _e) if _e else "")
    # Urgence HONNÊTE (MAYA) : fondée sur le marché réel France Travail, jamais inventée.
    urgence = ""
    if marche.get("sourced"):
        _nb = f"{marche['nb_offres']:,}".replace(",", " ")
        urgence = (f'<div style="margin-bottom:10px;font-size:14px">⏳ <b>{_nb}</b> offres pour votre métier '
                   'sont actives <b>en ce moment</b> — autant que votre CV soit prêt dès maintenant.</div>')
    # Garantie (reco MAYA, variante risk-reversal Hormozi). Porte sur la VALEUR du
    # diagnostic, jamais sur un emploi/entretien (ça, c'est la garantie du Reposition Pro).
    garantie = ('<div style="background:rgba(255,255,255,.14);border-radius:8px;padding:8px 12px;'
                'margin:0 0 10px;font-size:13px">🛡️ <b>Garantie :</b> si vous n\'y trouvez pas au moins '
                '3 leviers concrets à appliquer, je vous rembourse — et vous gardez le diagnostic.</div>')
    score = int(d.get("score_global", 0))
    radar_img = f'<img src="{radar}" style="width:100%;border-radius:12px">' if radar else ""
    pf = [x for x in d.get("points_forts", []) if x]
    forts = "".join(f'<span class=tag>✅ {esc(x)}</span>' for x in pf[:1])
    if len(pf) > 1:
        forts += f'<span class=tag style="opacity:.6">🔒 +{len(pf) - 1} autre(s) valorisé(s) dans le DI</span>'
    mk = [x for x in d.get("mots_cles_manquants", []) if x]
    manq = "".join(f'<span class=tag>🔑 {esc(x)}</span>' for x in mk[:2])
    if len(mk) > 2:
        manq += (f'<span class=tag style="opacity:.6">🔒 +{len(mk) - 2} autre(s) mot(s)-clé(s) dans le DI</span>')
    actions = _aa_actions_html(d.get("actions"))
    frein = esc((d.get("frein_principal", "") or "").strip())
    frein_li = (f'<div>✓ Le plan précis pour lever votre frein n°1 : « {frein} »</div>'
                if frein else '<div>✓ Le plan précis pour lever votre frein principal</div>')
    # Bloc CTA = création du « manque » (le CV ne montre qu'1/3) → DI à 99 €.
    # Claims réutilisés tels quels (LinkedIn SSI / réseau / stratégie / marché zone) :
    # aucune promesse inventée. La garantie 80 % appartient au Reposition Pro, PAS au
    # DI → on ne l'affiche pas ici. La déduction 99 €→RP vient du catalogue (DI→RP 391 €).
    cta_block = f"""<div class=cta>
    <div class=cs style="font-size:19px;margin-bottom:8px">Votre CV n'est qu'un tiers du problème</div>
    <p style="margin:0 0 12px;font-size:14px;opacity:.95">Cette analyse note votre <b>CV</b>. Mais votre invisibilité se
    joue surtout ailleurs — votre <b>visibilité LinkedIn</b>, votre <b>réseau</b> et votre <b>stratégie</b> sur le marché
    réel de votre zone — et cet aperçu gratuit ne peut pas le voir. C'est précisément ce que révèle le
    <b>Diagnostic Invisibilité™</b>.</p>
    <div style="background:rgba(255,255,255,.12);border-radius:10px;padding:12px 15px;text-align:left;font-size:14px;margin-bottom:12px">
      <b>Le Diagnostic Invisibilité™ vous apporte&nbsp;:</b>
      <div style="margin-top:6px">✓ Votre score de visibilité LinkedIn (SSI)</div>
      <div>✓ L'audit de votre réseau et de votre stratégie de candidature</div>
      <div>✓ Le marché réel de VOTRE zone (pas seulement le national)</div>
      {frein_li}
      <div>✓ Un regard <b>humain</b> : une vraie conseillère en insertion (CIP), pas un robot anonyme</div>
    </div>
    {urgence}
    {garantie}
    <div style="font-size:22px;font-weight:800">{di_label}</div>
    <div style="font-size:13px;opacity:.9;margin:4px 0 12px">…et les 99 € sont déduits si vous poursuivez ensuite vers le Reposition Pro™.</div>
    <a href="{cta_href}" style="display:inline-block;background:#fff;color:#784171;padding:13px 28px;border-radius:30px;font-weight:800;text-decoration:none">→ Révéler mes deux tiers invisibles</a>
  </div>""" if di_link != "#" else ""
    # Porte n°1 (PRINCIPALE) : appel découverte GRATUIT 30 min en visio (Calendly).
    # Proposé à tous, sans engagement, AVANT l'offre payante DI. Lien constant (web + email).
    rdv_block = (
        '<div class=cta style="background:linear-gradient(135deg,#784171,#4A2647)">'
        '<div class=cs style="font-size:19px;margin-bottom:6px">Envie d\'en parler de vive voix&nbsp;?</div>'
        '<p style="margin:0 0 14px;font-size:14px;opacity:.95">Réservez votre <b>appel découverte gratuit</b> avec '
        'Valérie — <b>30 minutes en visio</b>, sans engagement&nbsp;: on regarde ensemble ce qui bloque vraiment '
        'et vous repartez avec un cap clair.</p>'
        '<a href="https://calendly.com/valerie-blanchard-vb-evopro/30min" target="_blank" rel="noopener" '
        'style="display:inline-block;background:#fff;color:#784171;padding:14px 30px;border-radius:30px;'
        'font-weight:800;text-decoration:none;font-size:16px">📅 Réserver mon appel gratuit — 30 min en visio</a>'
        '</div>')
    intro = ('<p class=note style="text-align:left;margin-top:14px">Voici votre Analyse Augmentée™, '
             'comme demandé. Document personnel et confidentiel.</p>') if for_email else ""
    barre = "" if for_email else (
        '<div class=noprint style="text-align:center;margin-top:18px">'
        '<button class=btn onclick="window.print()">🖨 Imprimer / Enregistrer en PDF</button>'
        '<div class=note><a href="/" style="color:#7A6075">← Tester un autre CV</a></div></div>')
    plan_block = (f'<div class=h1band style="margin-top:22px">Votre plan d\'action — 30 jours</div>{actions}'
                  '<p class=note style="text-align:left">Voici votre première action prioritaire. '
                  'Le plan complet semaine par semaine (CV, titre LinkedIn, ciblage marché, activation réseau) '
                  'est détaillé et personnalisé dans le Diagnostic Invisibilité™.</p>') if actions else ""
    # Formule de politesse + remerciement, signée Valérie (standard VBEP : écran ET email).
    merci = (
        '<div style="background:#EDE5F2;border-left:4px solid #CAB6D2;border-radius:0 10px 10px 0;'
        'padding:15px 18px;margin-top:22px;font-size:14px;line-height:1.65">'
        '<b>Merci d\'avoir confié votre CV à VB Evolution Pro.</b><br>'
        'Quel que soit votre score, il ne dit rien de votre valeur — seulement de sa visibilité aujourd\'hui. '
        'Et cela, cela se travaille. Je serais ravie de vous accompagner pour rendre votre valeur à nouveau '
        'visible… et désirable.'
        '<div style="margin-top:10px;color:#784171;font-weight:700">Bien à vous,<br>Valérie Blanchard</div>'
        '<div style="color:#7A6075;font-size:12px;margin-top:2px">Conseillère en insertion professionnelle · '
        'VB Evolution Pro · valerie.blanchard@vb-evopro.fr · vb-evopro.fr</div></div>')
    # Logo : URL relative en web, URL absolue en email (sinon image cassée dans la boîte mail).
    logo_src = (PUBLIC_URL + "/logo.png") if for_email else "/logo.png"
    return f"""<!doctype html><html lang=fr><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Analyse Augmentée™ — VB Evolution Pro</title>{AA_STYLE}</head><body><div class=wrap>
<div class=head><img src="{logo_src}" alt="VB Evolution Pro" width="44" height="44" style="border-radius:8px;display:block">
<div><h1>Analyse Augmentée™</h1><div class=sub>VB Evolution Pro · Diagnostic d'employabilité par IA</div></div></div>
{intro}
<div class=card>
  <div class=h1band>Votre score de visibilité CV</div>
  <div class=grid>
    <div style="text-align:center"><div class=score>{score}</div><div class=sub>/100</div>
      <div style="margin-top:8px" class=cs>Poste détecté : <b>{esc(d.get('poste_detecte','—'))}</b></div></div>
    <div>{radar_img}</div>
  </div>
  <div style="margin-top:14px">{_aa_axes_html(d)}</div>
  {_aa_marche_html(marche)}
  {_aa_bloqueurs_html(d)}
  <p style="margin-top:18px"><b>Verdict CLARA :</b> {esc(d.get('verdict',''))}</p>
  <p>🔴 <b>Frein n°1 :</b> {esc(d.get('frein_principal',''))}</p>
  <div class=cs style="color:#784171;font-weight:700;margin-top:18px">Ce que votre CV a déjà pour lui</div>
  <div style="margin-top:8px">{forts}</div>
  <div class=cs style="color:#784171;font-weight:700;margin-top:16px">Mots-clés ATS qui vous manquent</div>
  <div style="margin-top:8px">{manq}</div>
  {plan_block}
  {rdv_block}
  {cta_block}
  <p class=note>Cet aperçu évalue votre CV. Le Diagnostic Invisibilité™ complet ajoute votre visibilité
  LinkedIn (score SSI), votre réseau et votre stratégie de candidature, sur le marché réel de votre zone.</p>
  {merci}
  {barre}
</div>
<div class=foot></div>
<div class=note>VB Evolution Pro · vb-evopro.fr · Valérie Blanchard, CIP · valerie.blanchard@vb-evopro.fr</div>
</div></body></html>"""


def envoyer_aa_par_email(email: str, nom: str, html_doc: str) -> None:
    """Envoie le document AA via le scénario Make/Outlook (best-effort, ne bloque jamais l'UX).
    L'appli POSTe simplement le HTML au webhook Make ; c'est Make qui expédie depuis
    l'Outlook de Valérie (fiable, et aucun secret email sur le serveur public)."""
    if not email or "@" not in email:
        return
    url = os.getenv("MAKE_AA_WEBHOOK_URL", "")
    if not url:
        log.warning("MAKE_AA_WEBHOOK_URL non configuré (.env) : email AA non envoyé.")
        return
    try:
        requests.post(url, json={
            "email": email,
            "nom": nom or "",
            "subject": "Votre Analyse Augmentée™ — VB Evolution Pro",
            "html": html_doc,
        }, timeout=8)
    except Exception:
        log.exception("Envoi email AA via Make échoué")


@app.get("/", response_class=HTMLResponse)
def home():
    return PAGE_HEAD + """
<div class=card>
  <div class=h1band>Bien plus qu'un scanner de CV</div>
  <p>Les outils classiques notent votre CV, point. Ici, on le <b>croise avec le marché réel de votre métier</b> —
  les offres actives <b>France Travail</b>, autour de chez vous <i>et</i> en France. Et le Diagnostic complet y ajoute
  votre <b>visibilité LinkedIn</b> et un <b>regard humain</b> de conseillère en insertion (CIP).</p>
  <p>Déposez votre CV en PDF — votre <b>Analyse Augmentée™</b> en 30 secondes : score de visibilité, radar,
  <b>marché réel de votre métier</b>, frein n°1 et votre première action.</p>
  <form action="/analyser" method=post enctype="multipart/form-data">
    <div class=drop>
      <p style="font-size:38px;margin:0">📄</p>
      <input type=file name=cv accept="application/pdf" required>
      <p class=note style="margin:8px 0 0">🔒 Votre CV et vos informations sont conservés <b>1 mois</b> (le temps de votre analyse et d'un éventuel suivi), puis automatiquement détruits. <a href="/confidentialite">En savoir plus</a></p>
    </div>
    <input type=email name=email required placeholder="Votre email (obligatoire — pour recevoir votre Analyse Augmentée)">
    <input type=url name=linkedin placeholder="Lien de votre profil LinkedIn (optionnel — analysé dans le diagnostic complet)">
    <input type=text name=ville placeholder="Votre ville (optionnel — pour voir les offres dans un rayon de 30 km)">
    <label class=consent><input type=checkbox name=consent value=oui>
      <span>J'accepte que mon email et mon profil LinkedIn soient enregistrés par VB Evolution Pro afin d'être recontacté(e)
      au sujet de mon employabilité. Sans cocher cette case, votre score s'affiche normalement mais aucune coordonnée
      n'est conservée. Voir la <a href="/confidentialite">politique de confidentialité</a>.</span>
    </label>
    <div style="text-align:center"><button class=btn type=submit>Tester mon CV →</button></div>
  </form>
  <p class=note>Aperçu fondé sur votre CV uniquement. Le Diagnostic Invisibilité™ complet ajoute votre
  visibilité LinkedIn, votre réseau et votre stratégie de candidature.</p>
</div>
<div id=loading>
  <div class=spin></div>
  <div class=cs style="color:#784171;font-size:20px">Analyse de votre CV en cours…</div>
  <div class=sub style="margin-top:6px">CLARA examine votre CV — cela prend environ 30 secondes. Merci de ne pas fermer cette page.</div>
</div>
<script>
(function(){
  var f=document.querySelector('form');
  if(!f) return;
  f.addEventListener('submit',function(){
    document.getElementById('loading').style.display='flex';
    var b=f.querySelector('button[type=submit]');
    if(b){b.disabled=true;b.textContent='Analyse en cours…';}
  });
})();
</script>""" + PAGE_FOOT


@app.get("/confidentialite", response_class=HTMLResponse)
def confidentialite():
    return PAGE_HEAD + """
<div class="card legal">
  <div class=h1band>Politique de confidentialité &amp; mentions légales</div>
  <p class=note>Dernière mise à jour : juin 2026. Conforme au RGPD (Règlement UE 2016/679) et à la loi Informatique et Libertés.</p>

  <h2>Responsable du traitement</h2>
  <p><b>VB Evolution Pro</b> — Valérie Blanchard, Conseillère en Insertion Professionnelle (CIP).<br>
  Salbris (41), France · Site : <a href="https://vb-evopro.fr">vb-evopro.fr</a><br>
  Contact / exercice de vos droits : <a href="mailto:valerie.blanchard@vb-evopro.fr">valerie.blanchard@vb-evopro.fr</a></p>

  <h2>Conservation de votre CV</h2>
  <p>Le fichier PDF que vous déposez est conservé de façon sécurisée pendant <b>1 mois</b> (analyse gratuite) —
  le temps de réaliser votre analyse et un éventuel suivi — puis <b>automatiquement détruit</b>. Si vous souscrivez
  un diagnostic payant, il est conservé <b>3 mois</b> afin d'établir ce diagnostic, puis détruit. Votre CV n'est ni
  revendu, ni réutilisé à d'autres fins.</p>

  <h2>Données traitées et finalités</h2>
  <ul>
    <li><b>Contenu du CV</b> (texte et mise en page) — pour calculer votre score et, si vous le souhaitez, votre diagnostic. Conservé 1 mois (analyse gratuite), 3 mois en cas de diagnostic payant, puis détruit.</li>
    <li><b>Email et profil LinkedIn</b> (facultatifs) — uniquement si vous cochez la case de consentement, pour vous
    recontacter au sujet de votre employabilité et vous adresser le rapport complet. Sans consentement, rien n'est enregistré.</li>
    <li><b>Poste détecté, code ROME, score, offre suggérée</b> — enregistrés avec vos coordonnées (si consentement) pour personnaliser le suivi.</li>
  </ul>

  <h2>Base légale</h2>
  <p>Le calcul de votre score repose sur votre demande (mesure préalable à votre initiative).
  L'enregistrement de vos coordonnées repose sur votre <b>consentement explicite</b> (case à cocher), que vous pouvez retirer à tout moment.</p>

  <h2>Destinataires et sous-traitants</h2>
  <p>Vos données ne sont jamais vendues. Elles sont traitées par des prestataires techniques agissant pour notre compte :</p>
  <ul>
    <li><b>Anthropic</b> (analyse du CV par IA) — le CV est transmis le temps de l'analyse ; Anthropic n'en conserve pas de copie.</li>
    <li><b>Airtable</b> (CRM) — hébergement de vos coordonnées en cas de consentement.</li>
    <li><b>France Travail</b> (statistiques du marché de l'emploi) — aucune donnée personnelle ne lui est transmise.</li>
  </ul>
  <p class=note>Certains prestataires peuvent être situés hors de l'Union européenne ; les transferts sont alors encadrés par les garanties prévues par le RGPD.</p>

  <h2>Durée de conservation</h2>
  <p>Analyse gratuite (CV et informations) : <b>1 mois</b>, puis destruction automatique.
  Diagnostic payant (vous devenez client) : <b>3 mois</b>, puis destruction.
  Dans tous les cas, vous pouvez demander la suppression de vos données à tout moment.</p>

  <h2>Vos droits</h2>
  <p>Vous disposez d'un droit d'accès, de rectification, d'effacement, d'opposition, de limitation et de portabilité,
  ainsi que du droit de retirer votre consentement à tout moment. Pour les exercer, écrivez à
  <a href="mailto:valerie.blanchard@vb-evopro.fr">valerie.blanchard@vb-evopro.fr</a>.
  Vous pouvez aussi introduire une réclamation auprès de la <a href="https://www.cnil.fr">CNIL</a>.</p>

  <p style="text-align:center;margin-top:18px"><a href="/">← Retour au test</a></p>
</div>""" + PAGE_FOOT


def _erreur(msg: str) -> HTMLResponse:
    return HTMLResponse(PAGE_HEAD + f'<div class=card><b>{msg}</b><br><a href="/">← Réessayer</a></div>' + PAGE_FOOT)


@app.post("/analyser", response_class=HTMLResponse)
async def analyser(request: Request, cv: UploadFile = File(...),
                   email: str = Form(""), linkedin: str = Form(""), consent: str = Form(""),
                   ville: str = Form("")):
    # 1) Anti-abus : limite de débit (protège votre budget API)
    refus = _rate_limited(_client_ip(request))
    if refus:
        return _erreur(refus)

    # 1bis) Email OBLIGATOIRE — refusé AVANT toute analyse coûteuse (le navigateur le
    #       bloque déjà via `required`, mais on revérifie côté serveur par sécurité).
    email = (email or "").strip()
    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        return _erreur("Merci d'indiquer une adresse email valide pour recevoir votre Analyse Augmentée.")

    # 2) Taille max : on lit au plus MAX_UPLOAD+1 octets pour détecter le dépassement
    data = await cv.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        return _erreur(f"Fichier trop volumineux (maximum {MAX_UPLOAD_MB} Mo).")
    if not data or not (cv.filename or "").lower().endswith(".pdf"):
        return _erreur("Merci de déposer un fichier PDF.")

    # 3) Erreurs internes masquées au visiteur, mais tracées côté serveur
    try:
        d = analyser_cv(data)
    except Exception:
        log.exception("Échec analyse CV")
        return _erreur("L'analyse a échoué, merci de réessayer dans un instant.")

    # Axe « Marché » sourcé France Travail (remplace l'estimation de l'IA si dispo).
    marche = analyser_marche_ft(d.get("code_rome", ""), d.get("poste_detecte", ""), ville)
    if marche.get("sourced"):
        d["marche"] = marche["score"]
        # Recalcule le score global pour rester cohérent avec les 5 axes (moyenne x10).
        axes_vals = [float(d.get(k, 0)) for k in
                     ("presentation", "mots_cles", "coherence", "marche", "positionnement")]
        d["score_global"] = round(sum(axes_vals) / len(axes_vals) * 10)

    # Offre recommandée par la grille METHODE.md (score × stade ADVP), pas par l'IA.
    off_code = _offre_methode(d.get("score_global", 0), d.get("stade_advp", ""))

    # Code ROME validé FT si dispo, sinon proposition de l'IA.
    code_rome_final = marche.get("code_rome") or d.get("code_rome", "")

    nom = (cv.filename.rsplit(".", 1)[0])[:40]
    radar = radar_data_uri(d, nom)
    # Mesure conversion (MAYA) : 1 analyse réussie, + 1 lead si consenti.
    _metrics["analyses"] += 1
    if bool(consent) and email and "@" in email:
        _metrics["leads"] += 1
    log.info("Analyse #%s · leads=%s · clics DI=%s", _metrics["analyses"], _metrics["leads"], _metrics["clics_di"])
    _kpi_bump(d_aa=1, d_leads=1 if (bool(consent) and email and "@" in email) else 0)  # persiste dans KPI & Pilotage

    # RGPD : capture CRM uniquement si consentement explicite coché (sinon rien n'est conservé).
    capture_lead(email, linkedin, off_code, code_rome_final, d, bool(consent), ville)  # non-bloquant
    stocker_cv(email, data, bool(consent))  # CV → Drive (conservation 1 mois AA, purge auto), non-bloquant

    # Document Analyse Augmentée™ généré pour tout le monde (sans RDV).
    doc_html = aa_html_standalone(d, radar, marche, off_code, for_email=False, email=email)

    # Livraison aussi par email si une adresse est fournie (best-effort, non bloquant).
    if email and "@" in email:
        try:
            envoyer_aa_par_email(email, nom, aa_html_standalone(d, radar, marche, off_code, for_email=True, email=email))
        except Exception:
            log.exception("Envoi email AA échoué (non bloquant)")

    return HTMLResponse(doc_html)


# ─────────────────────────────────────────────────────────────────────────────
# Capture lead → Airtable CRM (défensif : ne bloque jamais l'UX)
# ─────────────────────────────────────────────────────────────────────────────
def capture_lead(email: str, linkedin: str, offre: str, code_rome: str, d: dict, consent: bool, ville: str = ""):
    """Enregistre le lead dans la table CRM Clients, sur les champs EXISTANTS (pas de
    doublon). Best-effort : ne bloque jamais l'UX, et si l'envoi enrichi échoue
    (champ renommé, option select…), on retombe sur un envoi minimal (Email seul).
    RGPD : ne conserve RIEN sans consentement explicite (base légale = consentement)."""
    if not consent or not email or "@" not in email:
        return
    key = os.getenv("AIRTABLE_API_KEY")
    base = os.getenv("AIRTABLE_BASE_ID", "")
    url = f"https://api.airtable.com/v0/{base}/CRM%20Clients"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    now = datetime.now(timezone.utc)
    # Champs renseignés à CHAQUE passage (analyse + profil extrait du CV par l'IA).
    # Tous texte/nombre → aucun champ select ici (zéro risque d'option parasite).
    comp = d.get("competences_cles")
    if isinstance(comp, list):
        comp = ", ".join(str(x) for x in comp if x)
    profil = {
        "Nom": d.get("nom"), "Prénom": d.get("prenom"),
        "Téléphone": d.get("telephone"), "Code Postal": d.get("code_postal"),
        "Poste visé": d.get("poste_detecte"),
        "Secteur / Domaine": d.get("secteur"),
        "Niveau d'expérience": d.get("niveau_experience"),
        "Type de contrat souhaité": d.get("type_contrat"),
        "Compétences clés": comp,
        "Situation actuelle": d.get("situation_actuelle"),
        "Durée de recherche": d.get("duree_recherche"),
        "Candidatures sans réponse": d.get("candidatures_sans_reponse"),
        "Frein principal": d.get("frein_principal"),
        "🔥 Potentiel": d.get("potentiel"),   # champ passé en TEXTE par Valérie → écriture libre
        "Code Rome": code_rome,
    }
    fields = {"Email": email, "Source": "Teste ton CV",
              "Dernier contact": now.date().isoformat()}
    fields.update({k: v for k, v in profil.items() if v})
    if ville and ville.strip():
        fields["Ville"] = ville.strip()
    if linkedin and linkedin.startswith("http"):
        fields["lien LD"] = linkedin
    if d.get("score_global") is not None:
        fields["Score employabilité /100"] = int(d.get("score_global", 0))
    if offre:
        fields["Offre proposée"] = offre
        fields["Montant potentiel €"] = OFFRE_MONTANT.get(offre, 99)
    if d.get("stade_advp"):
        fields["Notes"] = f"Stade ADVP (interne) : {d['stade_advp']} — via Teste ton CV."
    # Amorce de relance : posée UNIQUEMENT à la création (ne pas réinitialiser une
    # relance déjà en cours quand on met à jour la fiche). Options EXACTES.
    amorce = {
        "Date d'entrée": now.date().isoformat(),
        "Statut pipeline": "Sophie - Nouveau Lead",
        # Le document AA envoyé par email = 1er contact (J0) → la séquence enchaîne sur J+2 (relance web).
        "Relance": "1",
        "Prochaine action": "🔄 Relancer",
        "Date prochaine action": (now + timedelta(days=RELANCE_J1)).isoformat(),
    }
    import urllib.parse

    def _send(method: str, u: str, payload: dict) -> bool:
        """Envoie un POST/PATCH Airtable et renvoie True SEULEMENT si HTTP < 300.
        ⚠️ requests ne lève PAS d'exception sur un 4xx → il FAUT tester le status,
        sinon un 422 (champ qui coince) passe inaperçu et le lead est perdu."""
        try:
            r = requests.request(method, u, headers=headers,
                                 json={"fields": payload, "typecast": True}, timeout=8)
        except Exception:
            log.exception("capture_lead : requête %s réseau échouée", method)
            return False
        if r.status_code < 300:
            return True
        log.warning("capture_lead : Airtable %s a renvoyé %s — %s",
                    method, r.status_code, r.text[:300])
        return False

    # 1) Retrouver une fiche existante (upsert par email) — non bloquant.
    rec_id = None
    try:
        formula = "{Email}='" + email.replace("'", "") + "'"
        q = f"{url}?maxRecords=1&filterByFormula=" + urllib.parse.quote(formula)
        rg = requests.get(q, headers=headers, timeout=8)
        if rg.status_code < 300:
            recs = rg.json().get("records", [])
            if recs:
                rec_id = recs[0]["id"]
    except Exception:
        log.exception("capture_lead : recherche par email échouée")

    # 2) Écriture en DÉGRADÉ : du plus riche au minimum increvable. On s'arrête
    #    au premier succès. La dernière tentative ({Email, Source}) ne contient
    #    aucun champ select/date → elle ne peut pas renvoyer 422.
    if rec_id:
        # MISE À JOUR (jamais d'amorce : ne pas réinitialiser une relance en cours).
        for payload in (fields, {"Dernier contact": now.date().isoformat()}):
            if _send("PATCH", f"{url}/{rec_id}", payload):
                return
    else:
        bare = {"Email": email, "Source": "Teste ton CV"}
        for payload in ({**fields, **amorce},
                        {**bare, "Date d'entrée": now.date().isoformat(), **amorce},
                        {**bare, "Date d'entrée": now.date().isoformat()},
                        bare,
                        {"Email": email}):
            if _send("POST", url, payload):
                return
    log.error("capture_lead : TOUTES les tentatives ont échoué pour %s (lead non enregistré)", email)


def stocker_cv(email: str, pdf_bytes: bytes, consent: bool, tag: str = "AA"):
    """Stocke le CV dans Drive via le webhook Make (conservation 1 mois AA / 3 mois DI,
    purge automatique côté Make). Consentement requis (donnée personnelle).
    Le nom de fichier encode le tag (AA/DI) + la date → sert à la purge auto.
    Best-effort : ne bloque jamais l'UX, aucun secret Google côté appli."""
    if not consent or not email or "@" not in email or not pdf_bytes:
        return
    url = os.getenv("MAKE_CV_WEBHOOK_URL", "")
    if not url:
        log.warning("MAKE_CV_WEBHOOK_URL non configuré : CV non archivé.")
        return
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", email)
    fname = f"{tag}_{datetime.now(timezone.utc).date().isoformat()}_{safe}.pdf"
    try:
        requests.post(url, json={"filename": fname,
                                 "cv_b64": base64.standard_b64encode(pdf_bytes).decode()},
                      timeout=10)
    except Exception:
        log.exception("Stockage CV via Make échoué")


if __name__ == "__main__":
    import uvicorn
    # En local : 127.0.0.1:8000. En ligne (Render/Railway…) : l'hébergeur fournit $PORT
    # et l'écoute doit se faire sur 0.0.0.0.
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
