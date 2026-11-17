from __future__ import annotations

import os
import re
from typing import Any

import pandas as pd
import streamlit as st
from psycopg import sql

from collector.api import CreditBudget, DunkAPIClient
from collector.config import Settings
from collector.db import EXPECTED_COLUMNS, SupabaseDatabase
from collector.importer import NBACollector


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
            if value is not None and str(value).strip() and not os.getenv(name):
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
                       records_updated, credits_estimated, error_message
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
    try:
        db = SupabaseDatabase(settings.supabase_db_url, schema=settings.database_schema)
        db.verify_schema()
        collector = NBACollector(api, db, budget)
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
            db.close()


def render_header() -> None:
    st.markdown(
        """
        <div class="hero">
          <h1>🏀 NBA Quant Engine</h1>
          <p>Collecte, contrôle de la qualité des données et suivi des imports NBA.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


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
                value=2027, step=1,
                help="2027 correspond à la saison 2026–2027.",
            )
        with c2:
            season_end = st.number_input(
                "Dernière saison incluse", min_value=2004, max_value=2035,
                value=2027, step=1,
            )
        with c3:
            max_games = st.number_input(
                "Nombre maximal de matchs", min_value=1, max_value=100,
                value=10, step=1,
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
                format_func=lambda value: {
                    "final": "Terminés",
                    "scheduled": "À venir",
                    "all": "Tous",
                }[value],
            )
        with d3:
            max_credits = st.number_input(
                "Plafond de crédits", min_value=10, max_value=350,
                value=min(settings.max_credits_per_run, 120), step=10,
                help="Le collecteur s'arrête avant tout appel estimé à dépasser ce plafond.",
            )

        include_stats = st.checkbox(
            "Importer les box-scores (matchs terminés uniquement)", value=True
        )
        include_odds = st.checkbox("Importer les cotes disponibles", value=True)
        submitted = st.form_submit_button(
            "Lancer la collecte", type="primary", use_container_width=True
        )

    st.info(
        "Réglages prudents par défaut : une saison, 10 matchs, 120 crédits maximum. "
        "Augmentez les limites progressivement après avoir vérifié les premiers imports."
    )

    if submitted:
        if season_end < season_start:
            st.error("La dernière saison doit être supérieure ou égale à la première.")
            return
        try:
            with st.spinner("Collecte en cours. Ne fermez pas cette page…"):
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
                )
            st.success("Collecte terminée.")
            result_cols = st.columns(4)
            result_cols[0].metric("Matchs sélectionnés", report["games_selected"])
            result_cols[1].metric("Équipes traitées", report["teams"])
            result_cols[2].metric("Snapshots de cotes", report["odds"])
            result_cols[3].metric("Crédits estimés", report["credits_used_local_estimate"])
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


def main() -> None:
    render_header()
    with st.sidebar:
        st.markdown("## Navigation")
        page = st.radio(
            "Ouvrir",
            ["Vue d'ensemble", "Collecteur"],
            label_visibility="collapsed",
        )
        st.divider()
        st.caption("NBA Quant Engine · DunkAPI → Supabase")
        st.caption("Schéma cible : nba")

    try:
        settings = read_settings()
    except Exception:
        render_setup_help()
        return

    try:
        if page == "Vue d'ensemble":
            render_overview(settings)
        else:
            render_collector(settings)
    except Exception as exc:
        st.error(f"Connexion ou lecture impossible : {exc}")
        st.caption(
            "Vérifiez l'URI PostgreSQL Supabase, les secrets Streamlit, l'accès réseau "
            "au Session Pooler et la présence du schéma nba."
        )


if __name__ == "__main__":
    main()
