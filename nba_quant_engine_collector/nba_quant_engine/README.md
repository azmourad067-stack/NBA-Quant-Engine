# NBA Quant Engine — Streamlit + DunkAPI collector

Application Streamlit et collecteur Python pour importer les équipes NBA, calendriers/résultats, box-scores et cotes DunkAPI dans le schéma PostgreSQL Supabase nba.

## Interface Streamlit

L'application propose deux vues :

- **Vue d'ensemble** : volumes d'équipes, matchs, joueurs, statistiques et snapshots de cotes ; derniers matchs ; qualité temporelle PIT ; historique des imports.
- **Collecteur** : lancement manuel des imports avec sélection de la saison, du type de saison, du statut des matchs, du nombre maximal de matchs et du plafond de crédits.

L'ouverture de l'application ne déclenche aucune collecte. Les imports utilisent le même NBACollector que la CLI, conservent les upserts idempotents, l'historique d'ingestion, les payloads bruts et les contrôles de provenance temporelle.

## Déploiement sur Streamlit Community Cloud

1. Ouvrir Streamlit Community Cloud et choisir **New app**.
2. Sélectionner ce dépôt GitHub : azmourad067-stack/NBA-Quant-Engine.
3. Choisir la branche feature/streamlit-dashboard.
4. Définir le chemin du fichier principal :
   streamlit_app.py (à la racine du dépôt)
5. Dans les options avancées, sélectionner Python 3.12.
6. Dans **App → Settings → Secrets**, ajouter les variables suivantes :

    APP_ACCESS_PASSWORD = "un-mot-de-passe-long-et-unique"
    DUNKAPI_API_KEY = "YOUR_DUNKAPI_API_KEY"
    SUPABASE_DB_URL = "postgresql://postgres...."
    NBA_DB_SCHEMA = "nba"
    DUNKAPI_BASE_URL = "https://mcp.dunkapi.com"
    MAX_CREDITS_PER_RUN = "350"
    REQUEST_TIMEOUT_SECONDS = "60"
    REQUEST_INTERVAL_SECONDS = "1.05"

SUPABASE_DB_URL doit être l'URI de connexion PostgreSQL obtenue dans Supabase → **Connect** → **Session Pooler**. Ce n'est pas la clé API service_role. APP_ACCESS_PASSWORD est obligatoire : l'application reste bloquée s'il n'est pas défini. Choisissez un mot de passe long et unique. Ne mettez jamais les secrets dans un fichier suivi par Git ou dans le code source.

Le fichier requirements.txt est situé dans le même dossier que streamlit_app.py, afin que Streamlit Cloud installe les dépendances de l'application.
Pour les connexions via le pooler transactionnel Supabase, l'application supprime le paramètre `pgbouncer=true` (spécifique à Prisma) et configure Psycopg avec `prepare_threshold=None`. Conservez les autres paramètres PostgreSQL de l'URI, notamment `sslmode=require`.


## Lancement local

Depuis le dossier nba_quant_engine_collector/nba_quant_engine :

    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    pip install -r requirements.txt
    Copy-Item .env.example .env
    streamlit run streamlit_app.py

Renseigner DUNKAPI_API_KEY et SUPABASE_DB_URL dans .env avant de lancer l'application. Sous macOS/Linux, activer l'environnement avec source .venv/bin/activate et copier le modèle avec cp .env.example .env.

La CLI reste disponible :

    python -m collector.cli --season-start 2027 --season-end 2027 --status final --max-games 10 --max-credits 120

## Import et intégrité des données

Tables alimentées :

- nba_teams, nba_games
- nba_team_game_stats, nba_players, nba_player_game_stats
- nba_odds_snapshots
- nba_ingestion_runs, nba_raw_payloads pour l'audit des imports

Le collecteur vérifie le schéma existant avant l'écriture. Il ne crée, ne supprime, ne tronque et ne modifie aucune table. Les upserts s'appuient sur les identifiants du fournisseur et préservent les valeurs non nulles déjà enregistrées.

Règles de données :

- Les statistiques non retournées par le fournisseur (par exemple pace, possessions ou ratings par 100 possessions) restent NULL.
- Les heures réelles de tip-off ne sont pas inventées.
- Une cote capturée avant le tip-off peut être verified_point_in_time.
- Une cote capturée après le tip-off est postgame_backfill.
- Une cote sans horodatage source fiable reste unknown.
- Les cotes historiques récupérées après le match ne doivent pas être utilisées comme prix pré-match dans les backtests.
- Les opening/closing moneyline sont importées lorsqu'elles sont fournies. Les lignes et prix spread/total non disponibles ne sont pas reconstruits artificiellement.
- Les réponses API sont conservées dans nba_raw_payloads et les appels sont consignés dans nba_ingestion_runs.

Le plafond de crédits de la page de collecte est une limite locale estimative : le fournisseur reste la référence pour la consommation réelle. Commencer par de petits lots (par défaut : 10 matchs et 120 crédits dans l'interface), puis augmenter progressivement après contrôle des premiers imports.

## Tests

    pytest -q

Les tests existants couvrent les mappings DunkAPI → Supabase, les conversions de cotes américaines, les box-scores et la classification PIT. Ils n'appellent ni DunkAPI ni Supabase.

## Couverture fournisseur

La documentation DunkAPI décrit des matchs et box-scores historiques depuis 2003–04, mais l'historique des cotes commence en 2013–14 avec des périodes et marchés incomplets. Les rapports de blessures ne peuvent être collectés qu'à partir du lancement de leur collecte. Vérifier le catalogue de données du fournisseur avant de planifier un import historique massif : https://dunkapi.com/data.
