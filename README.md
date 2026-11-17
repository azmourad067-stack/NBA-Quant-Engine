# NBA Quant Engine

Collecteur NBA DunkAPI → Supabase et tableau de bord Streamlit.

## Démarrer l'application

L'application Streamlit se lance depuis la racine du dépôt :

    streamlit run streamlit_app.py

Pour déployer sur Streamlit Community Cloud :

- Repository : azmourad067-stack/NBA-Quant-Engine
- Branch : feature/streamlit-dashboard
- Main file path : streamlit_app.py
- Python : 3.12

Les dépendances principales sont déclarées dans le requirements.txt de la racine, qui inclut celui du collecteur.

## Secrets à configurer dans Streamlit Cloud

Dans App → Settings → Secrets, ajouter :

    APP_ACCESS_PASSWORD = "un-mot-de-passe-long-et-unique"
    DUNKAPI_API_KEY = "YOUR_DUNKAPI_API_KEY"
    SUPABASE_DB_URL = "postgresql://postgres...."
    NBA_DB_SCHEMA = "nba"
    DUNKAPI_BASE_URL = "https://mcp.dunkapi.com"
    MAX_CREDITS_PER_RUN = "350"

Utiliser l'URI PostgreSQL obtenue dans Supabase → Connect → Session Pooler. Ne jamais utiliser ni publier la clé service_role à la place de cette URI. Le mot de passe APP_ACCESS_PASSWORD est obligatoire : l'application refuse de s'ouvrir tant qu'il n'est pas défini. Ne jamais committer les secrets.

## Fonctions actuelles

- Vue d'ensemble : comptages des tables, derniers matchs, qualité PIT des cotes et historique des imports.
- Collecteur : lancement manuel par saison, statut, taille de lot et plafond de crédits.
- Conservation du collecteur CLI et des fonctions d'import existantes.
- Validation du schéma avant écriture ; upserts idempotents ; audit des appels et conservation des payloads bruts.

L'ouverture du tableau de bord ne déclenche aucune collecte. Commencer par un petit lot et vérifier les données et les crédits consommés avant d'augmenter la charge.

Voir nba_quant_engine_collector/nba_quant_engine/README.md pour les instructions locales détaillées et les règles d'intégrité des données.


## Moteur prédictif

La branche `feature/streamlit-dashboard` inclut une première baseline pré-match (features chronologiques, Elo, forme équipe, cotes moneyline PIT, validation temporelle 80/20). Elle est accessible depuis la page **Moteur prédictif** ou via `python -m engine.cli --min-train-games 100` depuis `nba_quant_engine_collector/nba_quant_engine`. Le moteur lit Supabase uniquement et ne lance aucune collecte DunkAPI. Voir le README du sous-projet pour les limites et règles anti-fuite.
