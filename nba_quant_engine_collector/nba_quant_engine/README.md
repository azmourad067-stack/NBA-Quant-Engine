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

Pour les URI Supabase utilisant le pooler transactionnel, l'application retire le paramètre Prisma `pgbouncer=true`. Elle écarte également `hostaddr` lorsqu'il contient un nom DNS (ce paramètre doit contenir une IP littérale) et laisse PostgreSQL résoudre le nom porté par `host`. Les autres options, notamment `sslmode=require`, sont conservées. Psycopg utilise `prepare_threshold=None` pour éviter les requêtes préparées sur le pooler.

Pour les connexions via le pooler transactionnel Supabase, l'application supprime le paramètre `pgbouncer=true` (spécifique à Prisma) et configure Psycopg avec `prepare_threshold=None`. Conservez les autres paramètres PostgreSQL de l'URI, notamment `sslmode=require`.


## Lancement local

Depuis le dossier nba_quant_engine_collector/nba_quant_engine :

    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    pip install -r requirements.txt
    Copy-Item .env.example .env
    streamlit run streamlit_app.py

Renseigner DUNKAPI_API_KEY et SUPABASE_DB_URL dans .env avant de lancer l'application. Sous macOS/Linux, activer l'environnement avec source .venv/bin/activate et copier le modèle avec cp .env.example .env.

La CLI reste disponible. Pour lancer ou reprendre le backfill des saisons régulières 2023–2024 à 2025–2026 avec le plafond de 120 crédits :

    python -m collector.cli --season-start 2024 --season-end 2026 --season-type regular --status final --max-games 5000 --max-credits 120 --skip-odds

Relancer exactement la même commande jusqu'à ce que le rapport indique que toutes les saisons sont parcourues et qu'il ne reste plus de matchs terminés à enrichir. Le collecteur reprend les offsets de pagination dans `nba_ingestion_runs`, et interroge le backlog en base pour détailler les matchs les plus anciens incomplets. Les box-scores complets ne sont pas redemandés. Le plafond est appliqué à chaque lancement, pas à l'ensemble des lancements.

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

Le plafond de crédits de la page de collecte est une limite locale estimative : le fournisseur reste la référence pour la consommation réelle. Pour le backfill historique, le plafond est de 120 crédits par lancement et la pagination peut parcourir jusqu'à 5 000 matchs par lancement ; la limite de crédits, et non ce plafond de sécurité, arrête généralement le lot.

## Tests

    pytest -q

Les tests existants couvrent les mappings DunkAPI → Supabase, les conversions de cotes américaines, les box-scores et la classification PIT. Ils n'appellent ni DunkAPI ni Supabase.

## Couverture fournisseur

La documentation DunkAPI décrit des matchs et box-scores historiques depuis 2003–04, mais l'historique des cotes commence en 2013–14 avec des périodes et marchés incomplets. Les rapports de blessures ne peuvent être collectés qu'à partir du lancement de leur collecte. Vérifier le catalogue de données du fournisseur avant de planifier un import historique massif : https://dunkapi.com/data.


## Phase B — enrichissement des matchs terminés

La préconfiguration du collecteur passe en phase B :

- saison régulière 2026–2027 ;
- statut `final` ;
- jusqu'à 25 matchs ;
- box-scores activés ;
- cotes désactivées pour ne pas consommer de crédits sur des dates où DunkAPI ne renvoie aucune ligne ;
- plafond local de 120 crédits.

L'enrichissement est incrémental : avant chaque collecte de box-scores, le collecteur vérifie les matchs qui n'ont pas encore de lignes dans les deux tables `nba_team_game_stats` et `nba_player_game_stats`. Les matchs déjà complets sont ignorés. Un match partiellement enrichi est repris. Les upserts restent idempotents.

Lors de la collecte du 17 novembre 2026, DunkAPI a renvoyé les calendriers des 21–24 novembre mais des tableaux `books` vides pour chaque rencontre, avec la note fournisseur `No sportsbook lines for this day`. Ce n'est pas une erreur de parsing : aucune cote n'est disponible dans la réponse. Les cotes restent donc désactivées dans la phase B ; elles pourront être réactivées lors d'une collecte ultérieure si le fournisseur publie des lignes.

## Phase C — backfill historique paginé et reprenable

La préconfiguration du collecteur cible maintenant les saisons régulières 2023–2024 à 2025–2026, statut `final`, jusqu'à 5 000 matchs parcourus par lancement et un plafond de 120 crédits.

- La pagination `list_games` reprend au dernier `next_offset` réussi, stocké dans les métadonnées de `nba_ingestion_runs` pour chaque saison, type de saison et filtre de statut.
- Les offsets ne sont avancés qu'après une page réussie. Une relance ne recommence donc pas inutilement à la première page.
- Le collecteur réserve 25 crédits avant de poursuivre la pagination quand les box-scores sont activés.
- Les matchs terminés nécessitant encore des statistiques sont recherchés dans toute la plage de saisons en base, et enrichis par ordre chronologique avec les crédits restants.
- L'enrichissement est considéré complet lorsqu'il existe au moins deux lignes de statistiques équipes et au moins une ligne de statistiques joueurs.
- Les cotes sont désactivées dans cette étape : elles feront l'objet d'une collecte distincte, avec leur propre suivi de progression et leur coût. Les lignes horodatées après le tip-off ou sans heure source fiable ne sont jamais qualifiées de PIT.

Le fournisseur documente `limit`, `offset` et `next_offset` pour `list_games`. Le paramètre saison utilise l'année de fin (2024 = 2023–2024). La reprise s'appuie sur ces curseurs et sur les upserts idempotents.

## Moteur prédictif pré-match v0.1

Une première chaîne de modélisation est disponible dans `engine/` :

- dataset chronologique par match, sans utiliser les statistiques du match à prédire ;
- variables équipe : Elo pré-match, forme des cinq derniers matchs, points pour/contre, adresse, rebonds, passes, pertes de balle et jours de repos ;
- probabilité moneyline sans marge, calculée à partir de paires de cotes par bookmaker ;
- filtre strict : `snapshot_time_quality = 'verified_point_in_time'` et `source_captured_at < scheduled_tipoff_at` ;
- baseline Logistic Regression avec imputation et standardisation ajustées sur l'entraînement ;
- séparation chronologique 80/20 et comparaison Brier Score avec Elo et marché lorsque disponibles.

### Exécution

Depuis `nba_quant_engine_collector/nba_quant_engine` :

    python -m engine.cli --min-train-games 100

Cette commande lit uniquement Supabase. Elle ne nécessite pas de clé DunkAPI et ne lance aucune collecte. La page **Moteur prédictif** du dashboard effectue le même calcul en lecture seule.

Le seuil par défaut est de 100 matchs d'entraînement et au moins 25 matchs de test chronologique (125 matchs terminés au total). En dessous, le moteur renvoie `insufficient_history` et ne prétend pas avoir entraîné un modèle. La v0.1 est une baseline de recherche : pas encore de calibration, de modèle joueur, de backtest de mises ni de preuve de rentabilité. Les métriques hors-échantillon doivent être accumulées sur un historique plus large avant toute utilisation opérationnelle.

La v0.1 inclut aussi l'inférence sur les rencontres au statut `scheduled` : le modèle est réentraîné sur tous les matchs terminés disponibles et produit les probabilités domicile/extérieur uniquement si le seuil d'historique est atteint. Les rencontres à venir ne sont jamais ajoutées à l'historique ni au calcul des labels.
