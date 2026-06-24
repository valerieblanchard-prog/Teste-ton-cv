# Teste ton CV - VB Evolution Pro

Application web (FastAPI) qui analyse un CV (PDF) et renvoie un score
d'employabilite ATS + le marche reel France Travail (le diagnostic gratuit).

## Demarrage (hebergeur)
- Commande : voir le Procfile  ->  uvicorn app:app --host 0.0.0.0 --port $PORT
- Variables d'environnement : voir .env.example (les remplir dans l'hebergeur,
  ne JAMAIS commiter de vraies cles).

Autonome : ne depend d'aucun secret du systeme de bots (module moteur.py).
