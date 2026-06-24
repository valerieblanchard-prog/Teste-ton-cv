# -*- coding: utf-8 -*-
"""
Moteur autonome de « Teste ton CV » — extrait minimal de clara_tools pour que
l'appli web NE DÉPENDE PLUS du système de bots (et puisse être hébergée seule,
sans embarquer les secrets Telegram/Google/LinkedIn).

Contient uniquement ce dont app.py a besoin :
  - _ft_token()              : token OAuth France Travail (cache 55 min)
  - rechercher_code_rome()   : intitulé métier → code(s) ROME
  - rechercher_commune_ft()  : ville → code commune INSEE
  - _generer_radar_png()     : radar 5 axes en PNG (matplotlib)

Variables d'env requises (uniquement) : FT_CLIENT_ID, FT_CLIENT_SECRET.
"""
import os
import time
import unicodedata
import requests

FT_TOKEN_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token"
FT_METIERS_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/referentiel/metiers"
FT_COMMUNES_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/referentiel/communes"

_ft_token_cache = {"token": "", "expires_at": 0.0}


def _ft_token() -> str:
    """Token OAuth2 France Travail (client_credentials), mis en cache 55 min."""
    if _ft_token_cache["token"] and time.time() < _ft_token_cache["expires_at"]:
        return _ft_token_cache["token"]
    resp = requests.post(
        FT_TOKEN_URL,
        params={"realm": "/partenaire"},
        data={
            "grant_type": "client_credentials",
            "client_id": os.getenv("FT_CLIENT_ID", ""),
            "client_secret": os.getenv("FT_CLIENT_SECRET", ""),
            "scope": "api_offresdemploiv2 o2dsoffre",
        },
        timeout=15,
    )
    if resp.status_code == 200:
        data = resp.json()
        _ft_token_cache["token"] = data.get("access_token", "")
        _ft_token_cache["expires_at"] = time.time() + data.get("expires_in", 3600) - 300
        return _ft_token_cache["token"]
    raise RuntimeError(f"FT token {resp.status_code}: {resp.text[:200]}")


def rechercher_code_rome(metier: str) -> dict:
    """Intitulé métier / mot-clé → code(s) ROME les plus proches (référentiel FT)."""
    try:
        token = _ft_token()
    except RuntimeError as e:
        return {"erreur": str(e)}
    try:
        r = requests.get(FT_METIERS_URL,
                         headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                         timeout=15)
    except Exception as e:
        return {"erreur": f"Requête référentiel métiers : {e}"}
    if r.status_code != 200:
        return {"erreur": f"Référentiel métiers {r.status_code}: {r.text[:200]}"}
    metiers_list = r.json()
    mot = metier.strip().lower()

    def _score(m):
        lib = m.get("libelle", "").lower()
        if lib == mot:
            return 0
        if lib.startswith(mot):
            return 1
        if mot in lib:
            return 2
        mots = [w for w in mot.split() if len(w) > 2]
        if mots and all(w in lib for w in mots):
            return 3
        if mots and any(w in lib for w in mots):
            return 4
        return 99

    matches = [(m, _score(m)) for m in metiers_list if _score(m) < 99]
    matches.sort(key=lambda x: x[1])
    if not matches:
        return {"erreur": f"Aucun code ROME trouvé pour '{metier}'."}
    return {"metier_recherche": metier, "nb_resultats": len(matches),
            "resultats": [{"code_rome": m.get("code", ""), "libelle": m.get("libelle", "")}
                          for m, _ in matches[:10]]}


def rechercher_commune_ft(ville: str) -> dict:
    """Ville → code commune INSEE (référentiel communes FT). Normalise accents/tirets."""
    try:
        token = _ft_token()
    except RuntimeError as e:
        return {"erreur": str(e)}
    resp = requests.get(FT_COMMUNES_URL,
                        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                        params={"libelle": ville}, timeout=10)
    if resp.status_code != 200:
        return {"erreur": f"FT référentiel {resp.status_code}: {resp.text[:200]}"}
    communes = resp.json()
    if not communes:
        return {"erreur": f"Aucune commune trouvée pour '{ville}'."}

    def _norm(s):
        s = unicodedata.normalize("NFD", s or "")
        s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
        return " ".join(s.lower().replace("-", " ").replace("'", " ").split())

    cible = _norm(ville)

    def _rank(c):
        lib = _norm(c.get("libelle", ""))
        if lib == cible:
            return 0
        if lib.startswith(cible):
            return 1
        if cible in lib:
            return 2
        return 3

    filtered = [c for c in communes if cible and cible in _norm(c.get("libelle", ""))]
    if not filtered:
        filtered = communes
    filtered.sort(key=_rank)
    return {"total": len(filtered),
            "communes": [{"code": c.get("code", ""), "libelle": c.get("libelle", ""),
                          "codePostal": c.get("codePostal", "")} for c in filtered[:10]]}


def _generer_radar_png(s1: float, s2: float, s3: float, s4: float, s5: float,
                       score_global: int, nom_candidat: str, labels: list = None):
    """Radar /10 × 5 axes en PNG (bytes). None si matplotlib absent."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
        import io

        vals = [float(s1), float(s2), float(s3), float(s4), float(s5)]
        if labels is None:
            labels = ["ATS", "Lisibilité", "Cohérence", "Positionnement", "Impact CV"]
        N = 5
        angles = np.linspace(0, 2 * np.pi, N, endpoint=False).tolist()
        vplot = vals + [vals[0]]
        aplot = angles + angles[:1]

        fig, ax = plt.subplots(figsize=(5, 5), subplot_kw=dict(polar=True))
        fig.patch.set_facecolor("white")
        ax.set_facecolor("#F2EBF5")
        for r in [2.5, 5, 7.5, 10]:
            ax.plot(aplot, [r] * 6, color="#D5CCD9", lw=0.7, ls="-")
        for a in angles:
            ax.plot([a, a], [0, 10], color="#D5CCD9", lw=0.7)
        ax.fill(aplot, vplot, color="#CAB6D2", alpha=0.75)
        ax.plot(aplot, vplot, color="#5C2D6E", lw=2.5, solid_capstyle="round")
        ax.scatter(angles, vals, color="#5C2D6E", s=55, zorder=5)
        ax.set_xticks(angles)
        ax.set_xticklabels(labels, size=9, color="#3A2535", fontweight="bold")
        ax.set_ylim(0, 10)
        ax.set_yticks([2.5, 5, 7.5, 10])
        ax.set_yticklabels(["2.5", "5", "7.5", "10"], size=7.5, color="#7A6075")
        ax.grid(False)
        ax.spines["polar"].set_color("#D5CCD9")
        plt.title(f"{nom_candidat}  —  {score_global}/100", size=10, color="#5C2D6E",
                  pad=14, fontweight="bold")
        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                    facecolor="white", edgecolor="none")
        plt.close()
        buf.seek(0)
        return buf.read()
    except Exception:
        return None
