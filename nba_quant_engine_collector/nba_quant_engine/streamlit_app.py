from __future__ import annotations

import hmac
import os
import re
import sys
import time
from uuid import uuid4
from urllib.parse import parse_qsl, urlsplit
from pathlib import Path
from typing import Any

APP_BUILD_LABEL = "nba-historical-resumable-pagination-v0.4-2026-11-17"
APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import pandas as pd
import streamlit as st
from psycopg import sql

from collector.api import CreditBudget, DunkAPIClient
from collector.config import Settings
from collector.db import EXPECTED_COLUMNS, SupabaseDatabase
from collector.importer import NBACollector
from engine.service import build_engine_report


st.set_page_config(
    page_title="NBA Quant Engine",
    page_icon="🏀",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
      .stApp { background: #0b1220; color: #e8eef8; }
      [data-testid="stSidebar"] { background: #101b2d; border-right: 1px solid #24344c; }
      [data-testid="stHeader"] { background: rgba(11,18,32,.88); }
      .hero {
        padding: 1.3rem 1.5rem; border: 1px solid #263a56; border-radius: 16px;
        background: linear-gradient(120deg, #14243a 0%, #101b2d 60%, #1b2d45 100%);
        margin-bottom: 1rem;
      }
      .hero h1 { margin: 0; color: #f8fafc; font-size: 2rem; }
      .hero p { color: #a9bad0; margin: .35rem 0 0; }
      div[data-testid="stMetric"] {
        background: #111f33; border: 1px solid #253a55; border-radius: 12px;
        padding: 13px 15px;
      }
      div[data-testid="stMetricLabel"] { color: #9fb1c8; }
      div[data-testid="stMetricValue"] { color: #f8fafc; }
      .section-note { color: #9fb1c8; font-size: .92rem; }
      .stButton button[kind="primary"], .stFormSubmitButton button {
        background: #f59e0b; color: #101827; border: 0; font-weight: 700;
      }
      .stButton button[kind="primary"]:hover, .stFormSubmitButton button:hover {
        background: #fbbf24; color: #101827;
      }
      div[data-testid="stAlert"] { border-radius: 10px; }
      hr { border-color: #263a56; }
    </style>
    """,
    unsafe_allow_html=True,
)


def load_streamlit_secrets() -> None:
    """Expose Streamlit Cloud secrets to the existing environment-based Settings class."""
    secret_names = (
        "APP_ACCESS_PASSWORD",
        "DUNKAPI_API_KEY",
        "SUPABASE_DB_URL",
        "DUNKAPI_BASE_URL",
        "NBA_DB_SCHEMA",
        "REQUEST_TIMEOUT_SECONDS",
        "REQUEST_INTERVAL_SECONDS",
        "MAX_CREDITS_PER_RUN",
    )
    try:
        secrets = st.secrets
        for name in secret_names:
            value = secrets.get(name)
            if value is not None and str(value).strip():
                # Streamlit Cloud Secrets are the canonical source for the hosted app.
                # In particular, prefer SUPABASE_DB_URL over a stale process environment value.
                if name == "SUPABASE_DB_URL" or not os.getenv(name):
                    os.environ[name] = str(value).strip()
    except Exception:
        # Local .env remains supported; missing Streamlit secrets are handled below.
        return


def read_settings() -> Settings:
    load_streamlit_secrets()
    return Settings.from_env(env_file=None)


def table_identifier(schema: str, table: str):
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", schema):
        raise ValueError("Nom de schéma PostgreSQL invalide.")
    if table not in EXPECTED_COLUMNS and table not in {"nba_raw_payloads"}:
        raise ValueError("Table non autorisée.")
    return sql.SQL(".").join((sql.Identifier(schema), sql.Identifier(table)))


def fetch_dashboard_data(settings: Settings) -> dict[str, Any]:
    db = SupabaseDatabase(settings.supabase_db_url, schema=settings.database_schema)
    try:
        db.verify_schema()
        metrics: dict[str, int] = {}
        metric_tables = {
            "Équipes": "nba_teams",
            "Matchs": "nba_games",
            "Stats équipes": "nba_team_game_stats",
            "Joueurs": "nba_players",
            "Stats joueurs": "nba_player_game_stats",
            "Snapshots de cotes": "nba_odds_snapshots",
        }
        for label, table in metric_tables.items():
            row = db.conn.execute(
                sql.SQL("SELECT COUNT(*) AS total FROM {}").format(
                    table_identifier(settings.database_schema, table)
                )
            ).fetchone()
            metrics[label] = int(row["total"])

        runs = db.conn.execute(
            sql.SQL(
                """
                SELECT ingestion_run_id, endpoint_name, run_status, started_at,
                       completed_at, records_received, records_inserted,
                       records_updated, credits_estimated, error_message, metadata
                FROM {}.nba_ingestion_runs
                ORDER BY started_at DESC
                LIMIT 12
                """
            ).format(sql.Identifier(settings.database_schema))
        ).fetchall()

        games = db.conn.execute(
            sql.SQL(
                """
                SELECT g.game_date, g.game_status,
                       h.abbreviation AS home_team, g.home_score,
                       a.abbreviation AS away_team, g.away_score,
                       g.scheduled_tipoff_at, g.season_end_year
                FROM {}.nba_games AS g
                LEFT JOIN {}.nba_teams AS h
                  ON h.team_provider_id = g.home_team_provider_id
                LEFT JOIN {}.nba_teams AS a
                  ON a.team_provider_id = g.away_team_provider_id
                ORDER BY g.game_date DESC
                LIMIT 15
                """
            ).format(
                sql.Identifier(settings.database_schema),
                sql.Identifier(settings.database_schema),
                sql.Identifier(settings.database_schema),
            )
        ).fetchall()

        pit = db.conn.execute(
            sql.SQL(
                """
                SELECT snapshot_time_quality, COUNT(*) AS total
                FROM {}.nba_odds_snapshots
                GROUP BY snapshot_time_quality
                ORDER BY total DESC
                """
            ).format(sql.Identifier(settings.database_schema))
        ).fetchall()

        return {
            "connected": True,
            "metrics": metrics,
            "runs": [dict(row) for row in runs],
            "games": [dict(row) for row in games],
            "pit": [dict(row) for row in pit],
        }
    finally:
        db.close()


def run_collection(
    settings: Settings,
    season_start: int,
    season_end: int,
    season_type: str,
    status: str,
    max_games: int,
    max_credits: int,
    include_stats: bool,
    include_odds: bool,
    collection_id: str,
) -> dict[str, Any]:
    budget = CreditBudget(max_credits)
    api = DunkAPIClient(
        settings.dunkapi_key,
        base_url=settings.dunkapi_base_url,
        timeout=settings.request_timeout_seconds,
        interval_seconds=settings.request_interval_seconds,
        budget=budget,
    )
    db = None
    lock_acquired = False
    try:
        db = SupabaseDatabase(settings.supabase_db_url, schema=settings.database_schema)
        db.verify_schema()
        lock_acquired = db.try_acquire_collection_lock()
        if not lock_acquired:
            raise RuntimeError(
                "Une collecte NBA est déjà en cours. Aucune requête DunkAPI n'a été envoyée. "
                "Réessayez après sa fin."
            )
        collector = NBACollector(api, db, budget, collection_id=collection_id)
        return collector.collect(
            seasons=range(season_start, season_end + 1),
            season_type=season_type,
            max_games=max_games,
            include_stats=include_stats,
            include_odds=include_odds,
            status_filter=None if status == "all" else status,
        )
    finally:
        api.close()
        if db is not None:
            try:
                if lock_acquired:
                    db.release_collection_lock()
            finally:
                db.close()



def authenticate_app() -> None:
    """Require a private app password before exposing database or collection controls."""
    expected = os.getenv("APP_ACCESS_PASSWORD", "").strip()
    if not expected:
        st.error("Accès bloqué : APP_ACCESS_PASSWORD n'est pas configuré dans les secrets.")
        st.info(
            "Définissez un mot de passe long dans Streamlit Cloud → App → Settings → Secrets, "
            "puis rechargez l'application."
        )
        st.stop()

    if st.session_state.get("nba_quant_engine_authenticated", False):
        with st.sidebar:
            if st.button("Se déconnecter", use_container_width=True):
                st.session_state["nba_quant_engine_authenticated"] = False
                st.rerun()
        return

    st.subheader("Accès privé")
    st.caption("Connectez-vous pour consulter les données NBA et lancer une collecte.")
    with st.form("nba_quant_engine_login"):
        password = st.text_input("Mot de passe de l'application", type="password")
        submitted = st.form_submit_button("Se connecter", type="primary")
    if submitted:
        if hmac.compare_digest(password, expected):
            st.session_state["nba_quant_engine_authenticated"] = True
            st.rerun()
        else:
            st.error("Mot de passe incorrect.")
    st.stop()


def render_header() -> None:
    st.markdown(
        """
        <div class="hero">
          <h1>🏀 NBA Quant Engine</h1>
          <p>Données NBA, probabilités pré-match et suivi des imports.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )



def render_safe_connection_diagnostics() -> None:
    """Show safe connection metadata and isolate parsing failures without exposing secrets."""
    st.markdown("#### Diagnostic de connexion (sans identifiants)")
    # Always show the deployed build, even when the DSN is malformed.
    lines = [f"Version application : {APP_BUILD_LABEL}"]

    raw_dsn = os.getenv("SUPABASE_DB_URL", "").strip()
    lines.append(f"SUPABASE_DB_URL dans l'environnement : {'présente' if raw_dsn else 'absente'}")
    try:
        secrets = st.secrets
        secret_dsn = secrets.get("SUPABASE_DB_URL")
        secret_present = secret_dsn is not None and bool(str(secret_dsn).strip())
    except Exception:
        secret_present = False
    lines.append(f"SUPABASE_DB_URL dans Streamlit Secrets : {'présente' if secret_present else 'absente ou inaccessible'}")
    lines.append(f"Variable système PGHOSTADDR : {'présente' if os.getenv('PGHOSTADDR') else 'absente'}")
    lines.append(f"Variable système PGHOST : {'présente' if os.getenv('PGHOST') else 'absente'}")
    lines.append(f"Variable système PGSERVICE : {'présente' if os.getenv('PGSERVICE') else 'absente'}")

    if not raw_dsn:
        lines.append("Analyse URI : impossible, variable d'environnement absente")
        st.code("\n".join(lines), language="text")
        st.warning(
            "Aucune URI n'est disponible dans l'environnement utilisé par l'application. "
            "Vérifiez le nom SUPABASE_DB_URL dans App → Settings → Secrets."
        )
        return

    try:
        parts = urlsplit(raw_dsn)
        lines.append(f"Type URI PostgreSQL : {parts.scheme or 'non détecté'}")
        try:
            host = parts.hostname
            lines.append(f"Hôte : {host or 'non détecté'}")
        except ValueError as exc:
            lines.append(f"Lecture de l'hôte : erreur {type(exc).__name__}")
        try:
            port = parts.port
            lines.append(f"Port : {port if port is not None else 'défaut PostgreSQL'}")
        except ValueError as exc:
            lines.append(f"Lecture du port : erreur {type(exc).__name__}")
        try:
            query_keys = {key.lower() for key, _ in parse_qsl(parts.query, keep_blank_values=True)}
            lines.append(f"Paramètre hostaddr dans l'URI : {'oui' if 'hostaddr' in query_keys else 'non'}")
            lines.append(f"Paramètre pgbouncer dans l'URI : {'oui' if 'pgbouncer' in query_keys else 'non'}")
        except Exception as exc:
            lines.append(f"Lecture des paramètres URI : erreur {type(exc).__name__}")
        lines.append("Analyse URI : terminée (valeurs confidentielles masquées)")
    except Exception as exc:
        # Do not print exception text: parser errors can contain fragments of the DSN.
        lines.append(f"Analyse URI : échec ({type(exc).__name__})")
        lines.append("Hôte/port : non disponibles")

    st.code("\n".join(lines), language="text")


def render_setup_help() -> None:
    st.error("Configuration incomplète : les identifiants DunkAPI et Supabase ne sont pas disponibles.")
    st.markdown(
        """
        **Pour Streamlit Community Cloud**, ajoutez les secrets suivants dans
        **App → Settings → Secrets**. Pour un lancement local, renseignez les mêmes
        variables dans le fichier <code>.env</code> du dossier de l'application.
        """
    )
    st.code(
        'DUNKAPI_API_KEY = "votre_cle_dunkapi"\n'
        'SUPABASE_DB_URL = "postgresql://postgres...."\n'
        'NBA_DB_SCHEMA = "nba"\n'
        'DUNKAPI_BASE_URL = "https://mcp.dunkapi.com"\n'
        'MAX_CREDITS_PER_RUN = "350"',
        language="toml",
    )
    st.warning(
        "SUPABASE_DB_URL doit être l'URI PostgreSQL de Supabase (Connect → Session Pooler), "
        "pas la clé service_role. Ne partagez jamais ces valeurs dans le chat ou dans GitHub."
    )


def render_overview(settings: Settings) -> None:
    st.subheader("Vue d'ensemble")
    with st.spinner("Connexion à Supabase et lecture des tables NBA…"):
        data = fetch_dashboard_data(settings)

    metrics = data["metrics"]
    cols = st.columns(6)
    for col, (label, value) in zip(cols, metrics.items()):
        col.metric(label, f"{value:,}".replace(",", " "))

    st.divider()
    left, right = st.columns([1.15, 0.85])
    with left:
        st.markdown("#### Derniers matchs importés")
        if data["games"]:
            games_df = pd.DataFrame(data["games"])
            games_df = games_df.rename(
                columns={
                    "game_date": "Date",
                    "game_status": "Statut",
                    "home_team": "Domicile",
                    "home_score": "Pts dom.",
                    "away_team": "Extérieur",
                    "away_score": "Pts ext.",
                    "scheduled_tipoff_at": "Tip-off prévu",
                    "season_end_year": "Saison",
                }
            )
            st.dataframe(games_df, use_container_width=True, hide_index=True)
        else:
            st.info("Aucun match enregistré pour le moment.")

    with right:
        st.markdown("#### Qualité temporelle des cotes")
        if data["pit"]:
            pit_df = pd.DataFrame(data["pit"]).rename(
                columns={"snapshot_time_quality": "Qualité PIT", "total": "Snapshots"}
            ).set_index("Qualité PIT")
            st.bar_chart(pit_df, use_container_width=True)
            st.caption(
                "Seules les cotes horodatées avant le tip-off peuvent être vérifiées "
                "comme point-in-time. Les données post-match ou sans horodatage ne "
                "doivent pas être utilisées comme prix pré-match."
            )
        else:
            st.info("Aucun snapshot de cotes enregistré.")

    st.divider()
    st.markdown("#### Historique des imports")
    if data["runs"]:
        runs_df = pd.DataFrame(data["runs"]).rename(
            columns={
                "ingestion_run_id": "Run",
                "endpoint_name": "Endpoint",
                "run_status": "Statut",
                "started_at": "Début",
                "completed_at": "Fin",
                "records_received": "Reçus",
                "records_inserted": "Insérés",
                "records_updated": "Mis à jour",
                "credits_estimated": "Crédits",
                "error_message": "Erreur",
                "metadata": "Métadonnées / ID collecte",
            }
        )
        st.dataframe(runs_df, use_container_width=True, hide_index=True)
    else:
        st.info("L'historique des imports est vide.")

    if st.button("Actualiser les données affichées", use_container_width=False):
        st.rerun()


def render_collector(settings: Settings) -> None:
    st.subheader("Collecteur DunkAPI")
    st.markdown(
        '<p class="section-note">Chaque lancement consomme des crédits DunkAPI et écrit dans le schéma '
        '<code>nba</code>. L\'ouverture de cette page ne lance aucune collecte.</p>',
        unsafe_allow_html=True,
    )
    with st.form("collector_form", border=True):
        c1, c2, c3 = st.columns(3)
        with c1:
            season_start = st.number_input(
                "Première saison (année de fin)", min_value=2004, max_value=2035,
                value=2024, step=1,
                help="2024 correspond à la saison 2023–2024. La borne de fin est incluse.",
            )
        with c2:
            season_end = st.number_input(
                "Dernière saison incluse", min_value=2004, max_value=2035,
                value=2026, step=1,
            )
        with c3:
            max_games = st.number_input(
                "Nombre maximal de matchs à parcourir", min_value=1, max_value=5000,
                value=5000, step=100,
                help="Cap de sécurité par lancement. La pagination reprend automatiquement au dernier offset enregistré en base.",
            )

        d1, d2, d3 = st.columns(3)
        with d1:
            season_type = st.selectbox(
                "Type de saison",
                ["regular", "postseason", "play_in", "preseason"],
                format_func=lambda value: {
                    "regular": "Saison régulière",
                    "postseason": "Playoffs",
                    "play_in": "Play-in",
                    "preseason": "Pré-saison",
                }[value],
            )
        with d2:
            status = st.selectbox(
                "Statut des matchs",
                ["final", "scheduled", "all"],
                index=0,
                format_func=lambda value: {
                    "final": "Terminés",
                    "scheduled": "À venir",
                    "all": "Tous",
                }[value],
            )
        with d3:
            max_credits = st.number_input(
                "Plafond de crédits", min_value=10, max_value=350,
                value=max(10, min(settings.max_credits_per_run, 120)), step=10,
                help="Le collecteur s'arrête avant tout appel estimé à dépasser ce plafond.",
            )

        include_stats = st.checkbox(
            "Importer les box-scores (matchs terminés uniquement)", value=True
        )
        include_odds = st.checkbox("Importer les cotes disponibles", value=False)
        submitted = st.form_submit_button(
            "Lancer la collecte", type="primary", use_container_width=True
        )

    st.info(
        "Préconfiguration historique : saisons régulières 2023–2024 à 2025–2026 incluses, "
        "matchs terminés, jusqu'à 5 000 matchs parcourus par lancement, box-scores activés, "
        "cotes désactivées et plafond de 120 crédits. Les pages de calendrier reprennent au "
        "dernier offset sauvegardé dans nba_ingestion_runs. Le collecteur réserve 25 crédits "
        "aux box-scores, puis enrichit en priorité les matchs terminés les plus anciens qui "
        "n'ont pas encore leurs deux lignes équipes et leurs statistiques joueurs. Les matchs "
        "déjà complets sont ignorés. Relancez la collecte pour poursuivre jusqu'à la fin des "
        "trois saisons ; aucun doublon de match ou de box-score n'est attendu grâce aux clés "
        "uniques et aux upserts. Les cotes historiques nécessitent une étape distincte."
    )

    if submitted:
        if season_end < season_start:
            st.error("La dernière saison doit être supérieure ou égale à la première.")
            return
        # Streamlit reruns the script after every form submission. Keep a short
        # same-session idempotency window so a repeated click cannot replay the
        # exact same expensive request immediately after the first run completes.
        submission_signature = (
            int(season_start), int(season_end), season_type, status, int(max_games),
            int(max_credits), bool(include_stats), bool(include_odds),
        )
        now = time.monotonic()
        previous = st.session_state.get("nba_last_collection_submission")
        if (
            previous
            and previous.get("signature") == submission_signature
            and now - previous.get("at", 0.0) < 60
        ):
            st.warning(
                "Cette demande identique vient déjà d'être soumise. "
                "Attendez 60 secondes avant de la relancer. Aucun appel DunkAPI supplémentaire n'a été effectué."
            )
            return

        collection_id = str(uuid4())
        st.session_state["nba_last_collection_submission"] = {
            "signature": submission_signature,
            "at": now,
            "collection_id": collection_id,
        }
        try:
            with st.spinner(f"Collecte {collection_id} en cours. Ne fermez pas cette page…"):
                report = run_collection(
                    settings=settings,
                    season_start=int(season_start),
                    season_end=int(season_end),
                    season_type=season_type,
                    status=status,
                    max_games=int(max_games),
                    max_credits=int(max_credits),
                    include_stats=include_stats,
                    include_odds=include_odds,
                    collection_id=collection_id,
                )
            st.success("Collecte terminée.")
            st.caption(f"Identifiant unique de collecte : {collection_id}")
            result_cols = st.columns(4)
            result_cols[0].metric("Matchs parcourus", report["games_selected"])
            result_cols[1].metric("Pages calendrier", report["games_pages"])
            result_cols[2].metric("Box-scores enrichis", report["completed_games_for_stats"])
            result_cols[3].metric("Crédits estimés", report["credits_used_local_estimate"])
            if report["pagination_complete"]:
                st.success("Pagination terminée : toutes les saisons sélectionnées ont été parcourues.")
            else:
                pending = ", ".join(str(season) for season in report["seasons_pending_pagination"])
                st.warning(
                    f"Pagination incomplète pour les saisons {pending}. "
                    "Relancez la même collecte : elle reprendra aux offsets sauvegardés."
                )
            if report["final_games_pending_details"]:
                st.info(
                    f"{report['final_games_pending_details']} matchs terminés restent à enrichir "
                    "en box-scores. Ils seront repris lors des prochaines collectes."
                )
            with st.expander("Rapport détaillé", expanded=True):
                st.json(report)
            st.caption(
                "Les crédits affichés sont l'estimation locale du collecteur ; "
                "les en-têtes de facturation DunkAPI restent la référence fournisseur."
            )
        except Exception as exc:
            st.error(f"La collecte a été interrompue : {exc}")
            st.warning(
                "Consultez l'historique des imports dans la vue d'ensemble. "
                "Les appels déjà terminés peuvent avoir été enregistrés même si le run échoue ensuite."
            )



def render_engine(settings: Settings) -> None:
    """Read-only model diagnostics; no DunkAPI calls or database writes."""
    from collector.db import SupabaseDatabase

    st.subheader("Moteur prédictif pré-match · v0.1")
    st.caption("Calcul en lecture seule depuis Supabase. Aucun appel DunkAPI ni écriture. Le moteur refuse l'entraînement si l'historique est trop court.")
    min_train_games = st.number_input("Minimum de matchs avant entraînement", min_value=50, max_value=1000, value=100, step=50)
    if st.button("Construire le dataset et évaluer le moteur", type="primary"):
        db = None
        try:
            with st.spinner("Construction chronologique du dataset et évaluation…"):
                db = SupabaseDatabase(settings.supabase_db_url, schema=settings.database_schema)
                db.verify_schema()
                report = build_engine_report(db, min_train_games=int(min_train_games))
            dataset = report.get("dataset", {})
            cols = st.columns(4)
            cols[0].metric("Matchs terminés", dataset.get("games", report.get("games", 0)))
            cols[1].metric("Matchs à venir", dataset.get("upcoming_games", 0))
            cols[2].metric("Variables pré-match", dataset.get("feature_count", 0))
            cols[3].metric("Matchs avec cote PIT", dataset.get("market_feature_coverage", 0))
            if report["status"] == "insufficient_history":
                st.warning(report.get("message", "Historique insuffisant pour entraîner le modèle."))
                st.info("Le moteur ne fabrique pas de modèle avec un échantillon trop petit. Enrichissez d'abord l'historique des matchs terminés et des cotes PIT.")
            elif report["status"] == "evaluated":
                st.success(f"Évaluation chronologique : {report['train_games']} matchs d'entraînement, puis {report['test_games']} matchs de test.")
                st.json(report["metrics"])
                if report.get("predictions"):
                    st.dataframe(pd.DataFrame(report["predictions"]), use_container_width=True, hide_index=True)
            else:
                st.warning(f"Évaluation non disponible : {report['status']}")
            upcoming = report.get("upcoming", {})
            st.divider()
            st.markdown("#### Prochains matchs")
            if upcoming.get("status") == "predicted":
                st.dataframe(pd.DataFrame(upcoming["predictions"]), use_container_width=True, hide_index=True)
            elif upcoming.get("status") == "insufficient_history":
                st.info(f"Aucune probabilité future publiée : {upcoming.get('games', 0)} matchs terminés disponibles, historique minimum requis : {min_train_games}.")
            elif upcoming.get("status") == "no_upcoming_games":
                st.caption("Aucun match au statut scheduled à prédire.")
            else:
                st.caption("Prédictions futures non disponibles pour le moment.")
            st.caption("Anti-fuite : seules les cotes verified_point_in_time capturées strictement avant scheduled_tipoff_at sont admises. Une évaluation hors-échantillon n'est pas une preuve de rentabilité.")
        except Exception as exc:
            st.error(f"Construction du moteur impossible : {exc}")
        finally:
            if db is not None:
                db.close()

def main() -> None:
    load_streamlit_secrets()
    authenticate_app()
    render_header()
    with st.sidebar:
        st.markdown("## Navigation")
        page = st.radio(
            "Ouvrir",
            ["Vue d'ensemble", "Moteur prédictif", "Collecteur"],
            label_visibility="collapsed",
        )
        st.divider()
        st.caption("NBA Quant Engine · DunkAPI → Supabase")
        st.caption("Build : " + APP_BUILD_LABEL)
        st.caption("Schéma cible : nba")

    try:
        settings = read_settings()
    except Exception:
        render_setup_help()
        return

    try:
        if page == "Vue d'ensemble":
            render_overview(settings)
        elif page == "Moteur prédictif":
            render_engine(settings)
        else:
            render_collector(settings)
    except Exception as exc:
        st.error(f"Connexion ou lecture impossible : {exc}")
        st.caption(
            "Vérifiez que Streamlit déploie bien la branche feature/streamlit-dashboard "
            "et que l'URI provient de Supabase → Connect → Session Pooler."
        )
        render_safe_connection_diagnostics()


if __name__ == "__main__":
    main()
