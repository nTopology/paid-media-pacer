"""
Brian View — GA4 "2024 Website Dash" rebuild + Warmly tier/company filtering.

Two explicit modes:
  Mode 1 — All traffic (GA4): rebuilt dashboard from pre-aggregated weekly tables.
  Mode 2 — Identified companies (Warmly): tier + company filters, daily/weekly/monthly.
"""

from __future__ import annotations

import base64
import json
import math
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from google.cloud import bigquery
from google.oauth2 import service_account


# ── Brand assets ─────────────────────────────────────────────────────────────
_BRAND_DIR = Path(__file__).parent.parent / "static" / "brand"
_LOGO_DIR  = Path(__file__).parent.parent / "static" / "logos"

try:
    _RAW = json.loads((_BRAND_DIR / "colors.json").read_text())
    _C = {
        "blue":       _RAW["accent"]["ntop_blue"]["hex"],
        "black":      _RAW["primary"]["black"]["hex"],
        "white":      _RAW["primary"]["white"]["hex"],
        "green":      _RAW["signal_indicators"]["green"],
        "red":        _RAW["signal_indicators"]["red"],
        "gray_light": _RAW["neutrals_optional"]["gray_light"],
        "gray_mid":   _RAW["neutrals_optional"]["gray_mid"],
        "gray_dark":  _RAW["neutrals_optional"]["gray_dark"],
    }
except Exception:
    _C = {
        "blue": "#248AFF", "black": "#000000", "white": "#FFFFFF",
        "green": "#1FA34E", "red": "#D43F3F",
        "gray_light": "#E5E5E5", "gray_mid": "#999999", "gray_dark": "#333333",
    }

_SVG_FILE = _LOGO_DIR / "nTop-Logo_Light-theme.svg"
_PNG_FILE = _LOGO_DIR / "nTop-Logo_Light-theme_400w.png"

if _SVG_FILE.exists():
    _logo_b64 = base64.b64encode(_SVG_FILE.read_bytes()).decode()
    _LOGO_IMG_HTML = (
        f'<img src="data:image/svg+xml;base64,{_logo_b64}" '
        f'height="54" style="display:block;flex-shrink:0;">'
    )
elif _PNG_FILE.exists():
    _logo_b64 = base64.b64encode(_PNG_FILE.read_bytes()).decode()
    _LOGO_IMG_HTML = (
        f'<img src="data:image/png;base64,{_logo_b64}" '
        f'height="54" style="display:block;flex-shrink:0;">'
    )
else:
    _LOGO_IMG_HTML = (
        f'<span style="font-family:Oswald,sans-serif;font-weight:700;'
        f'font-size:22px;color:{_C["black"]};">nTop</span>'
    )


# ── Constants ────────────────────────────────────────────────────────────────
SERVICE_ACCOUNT_FILE = "service-account.json"
GCP_PROJECT = "bi-ntop"

GA4_START = date(2025, 4, 14)
WARMLY_START = date(2025, 6, 1)

TIER_ORDER = [
    "Tier 1A (Big 3)",
    "Tier 1 (Primes)",
    "Tier 2 (Growth OEMs)",
    "Tier 3 (Programs, Depots, Labs)",
    "Tier 4 (Watchlist)",
    "Untiered",
]

DEFAULT_TIERS_ON = {
    "Tier 1A (Big 3)",
    "Tier 1 (Primes)",
    "Tier 2 (Growth OEMs)",
    "Tier 3 (Programs, Depots, Labs)",
}

TIER_COLORS = {
    "Tier 1A (Big 3)":                 "#D43F3F",
    "Tier 1 (Primes)":                 "#0047FF",
    "Tier 2 (Growth OEMs)":            "#F5A623",
    "Tier 3 (Programs, Depots, Labs)": "#1FA34E",
    "Tier 4 (Watchlist)":              "#888888",
    "Untiered":                        "#CCCCCC",
}

COMPANY_COLORS = [
    "#0047FF", "#D43F3F", "#1FA34E", "#F5A623", "#9B59B6",
    "#E67E22", "#1ABC9C", "#E74C3C", "#3498DB", "#2ECC71",
]

CHANNEL_COLORS = [
    "#0047FF", "#D43F3F", "#1FA34E", "#F5A623", "#9B59B6",
    "#E67E22", "#1ABC9C", "#E74C3C", "#3498DB", "#2ECC71",
    "#888888", "#FF6384", "#36A2EB", "#FFCE56", "#4BC0C0",
]

ROWS_PER_PAGE = 25


# ── Brand CSS + density overrides ────────────────────────────────────────────
_BRAND_CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Oswald:wght@700&family=IBM+Plex+Sans:wght@400;600;700&family=IBM+Plex+Mono&display=swap');

html, body, .stApp, .stMarkdown, .stCaption, p {{
    font-family: 'IBM Plex Sans', sans-serif;
}}
h1, h2, h3 {{
    font-family: 'Oswald', sans-serif;
    font-weight: 700;
    color: {_C["black"]};
}}
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] p,
section[data-testid="stSidebar"] div {{
    font-family: 'IBM Plex Sans', sans-serif;
}}
.stAlert p, .stAlert div {{
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 14px;
}}

/* ── Tighten Streamlit default spacing ─────────────────────────────────── */
.block-container {{
    padding-top: 2.5rem;
    padding-bottom: 1rem;
}}
[data-testid="stVerticalBlock"] > [data-testid="stVerticalBlockBorderWrapper"] {{
    gap: 0.4rem;
}}
div[data-testid="stHorizontalBlock"] > div {{
    gap: 0.5rem;
}}
.stRadio > div {{
    gap: 0.25rem;
}}

/* ── Header ────────────────────────────────────────────────────────────── */
.ntop-header {{
    display: flex;
    align-items: center;
    gap: 20px;
    padding-bottom: 16px;
    margin-bottom: 16px;
    border-bottom: 2px solid {_C["black"]};
}}
.ntop-header-text {{
    border-left: 1px solid {_C["gray_light"]};
    padding-left: 20px;
}}
.ntop-page-title {{
    font-family: 'Oswald', sans-serif;
    font-weight: 700;
    font-size: 26px;
    color: {_C["black"]};
    line-height: 1.15;
    margin: 0 0 3px 0;
}}
.ntop-page-subtitle {{
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 13px;
    color: {_C["gray_dark"]};
    margin: 0;
    line-height: 1.4;
}}

/* ── Compact KPI cards ─────────────────────────────────────────────────── */
.kpi-card {{
    background: {_C["white"]};
    border: 1px solid {_C["gray_light"]};
    border-radius: 6px;
    padding: 14px 16px 12px;
    text-align: center;
}}
.kpi-label {{
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: {_C["gray_mid"]};
    margin: 0 0 2px 0;
}}
.kpi-value {{
    font-family: 'Oswald', sans-serif;
    font-weight: 700;
    font-size: 30px;
    color: {_C["black"]};
    line-height: 1.1;
    margin: 0 0 4px 0;
}}
.kpi-delta {{
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 12px;
    font-weight: 600;
    margin: 0;
}}
.kpi-delta-up {{ color: {_C["green"]}; }}
.kpi-delta-down {{ color: {_C["red"]}; }}
.kpi-delta-flat {{ color: {_C["gray_mid"]}; }}

/* ── Dense analytics table ─────────────────────────────────────────────── */
.dense-table {{
    width: 100%;
    border-collapse: collapse;
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 12px;
}}
.dense-table thead th {{
    background: #F7F7F7;
    border-bottom: 2px solid {_C["gray_light"]};
    padding: 6px 10px;
    text-align: left;
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.3px;
    color: {_C["gray_dark"]};
    white-space: nowrap;
}}
.dense-table thead th.num {{
    text-align: right;
}}
.dense-table tbody tr {{
    border-bottom: 1px solid #F0F0F0;
}}
.dense-table tbody tr:hover {{
    background: #FAFAFA;
}}
.dense-table tbody td {{
    padding: 5px 10px;
    color: {_C["black"]};
    vertical-align: middle;
}}
.dense-table tbody td.num {{
    text-align: right;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
}}
.dense-table tbody td.path {{
    max-width: 220px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 11px;
}}
.dense-table tbody td.title {{
    max-width: 280px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}}

/* ── Section divider ───────────────────────────────────────────────────── */
.section-rule {{
    border: none;
    border-top: 1px solid {_C["gray_light"]};
    margin: 12px 0;
}}
</style>
"""


# ── Page setup ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="Brian View", layout="wide")
st.markdown(_BRAND_CSS, unsafe_allow_html=True)

if _PNG_FILE.exists():
    st.logo(str(_PNG_FILE))

st.markdown(
    f'<div class="ntop-header">'
    f'{_LOGO_IMG_HTML}'
    f'<div class="ntop-header-text">'
    f'<div class="ntop-page-title">Brian View</div>'
    f'<div class="ntop-page-subtitle">'
    f'Website dashboard rebuilt from GA4 aggregated tables, '
    f'with Warmly-powered tier and company filtering.'
    f'</div></div></div>',
    unsafe_allow_html=True,
)


# ── About this data ──────────────────────────────────────────────────────────
with st.expander("About this data"):
    st.markdown(
        "**Two modes, two different data populations — they won't reconcile.**\n\n"
        "- **Mode 1 — All Traffic (GA4):** every visitor, weekly grain. "
        "Source: pre-aggregated GA4 tables. Data starts **April 14, 2025**. "
        "No tier or company filter (those dimensions don't exist in GA4).\n"
        "- **Mode 2 — Identified Companies (Warmly):** only the ~9% of traffic "
        "Warmly could tie to a company. Data starts **June 1, 2025**. "
        "Supports tier filter, company lookup, and daily/weekly/monthly grain, "
        "but limited to page views and distinct companies (no device/browser/"
        "channel/bounce data).\n\n"
        "Tiering uses the Salesforce aircraft-concept target type, with "
        "Boeing / Lockheed Martin / Northrop Grumman grouped as "
        "\"Tier 1A (Big 3)\" by name match."
    )


# ── Auth ─────────────────────────────────────────────────────────────────────
@st.cache_resource
def get_credentials():
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    try:
        if "gcp_service_account" in st.secrets:
            return service_account.Credentials.from_service_account_info(
                st.secrets["gcp_service_account"], scopes=scopes
            )
    except Exception:
        pass
    return service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=scopes
    )


@st.cache_resource
def get_bq_client():
    return bigquery.Client(credentials=get_credentials(), project=GCP_PROJECT)


def _run(query: str, params: list | None = None) -> pd.DataFrame:
    cfg = bigquery.QueryJobConfig(query_parameters=params or [])
    return (
        get_bq_client()
        .query(query, job_config=cfg)
        .to_dataframe(create_bqstorage_client=False)
    )


# ── SQL helpers ──────────────────────────────────────────────────────────────
def _trunc_expr(col: str, grain: str) -> str:
    if grain == "Daily":
        return f"DATE({col})"
    if grain == "Weekly":
        return f"DATE_TRUNC(DATE({col}), WEEK(MONDAY))"
    return f"DATE_TRUNC(DATE({col}), MONTH)"


_TIER_CASE = """
CASE
  WHEN LOWER(acct.name) LIKE '%boeing%'
    OR LOWER(acct.name) LIKE '%lockheed%'
    OR LOWER(acct.name) LIKE '%northrop%'
      THEN 'Tier 1A (Big 3)'
  WHEN acct.aircraft_concept_target_type_c LIKE 'Tier 1%' THEN 'Tier 1 (Primes)'
  WHEN acct.aircraft_concept_target_type_c LIKE 'Tier 2%' THEN 'Tier 2 (Growth OEMs)'
  WHEN acct.aircraft_concept_target_type_c LIKE 'Tier 3%' THEN 'Tier 3 (Programs, Depots, Labs)'
  WHEN acct.aircraft_concept_target_type_c LIKE 'Tier 4%' THEN 'Tier 4 (Watchlist)'
  ELSE 'Untiered'
END
"""

# %boeing% also catches "Aviation Partners Boeing" (a JV) — acceptable for v1


# ── Presentation helpers ─────────────────────────────────────────────────────
def _base_layout(fig: go.Figure, title: str, height: int = 400,
                 tick_fmt: str = "%b %d") -> None:
    fig.update_layout(
        title=dict(
            text=title,
            font=dict(family="Oswald", size=18, color=_C["black"]),
            y=0.97, yanchor="top",
        ),
        height=height,
        margin=dict(l=48, r=16, t=72, b=32),
        hovermode="x unified",
        plot_bgcolor="white",
        paper_bgcolor="white",
        legend=dict(
            orientation="h",
            yanchor="bottom", y=1.0,
            xanchor="left", x=0,
            font=dict(size=10, family="IBM Plex Sans"),
        ),
        xaxis=dict(
            tickformat=tick_fmt,
            showgrid=True, gridcolor="#F0F0F0",
            tickfont=dict(size=10),
        ),
        yaxis=dict(
            showgrid=True, gridcolor="#F0F0F0",
            tickformat=",d",
            rangemode="tozero",
            tickfont=dict(size=10),
        ),
    )


def _format_duration(seconds: float) -> str:
    if pd.isna(seconds) or seconds < 0:
        return "0:00"
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m}:{s:02d}"


def _snap_to_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _kpi_card_html(label: str, value: str, delta_pct: float | None) -> str:
    """Render a compact KPI card as raw HTML."""
    if delta_pct is None:
        arrow = ""
        d_class = "kpi-delta-flat"
        d_text = "N/A"
    elif delta_pct >= 0:
        arrow = "&#9650; "  # ▲
        d_class = "kpi-delta-up"
        d_text = f"+{delta_pct:.1f}%"
    else:
        arrow = "&#9660; "  # ▼
        d_class = "kpi-delta-down"
        d_text = f"{delta_pct:.1f}%"
    return (
        f'<div class="kpi-card">'
        f'<p class="kpi-label">{label}</p>'
        f'<p class="kpi-value">{value}</p>'
        f'<p class="kpi-delta {d_class}">{arrow}{d_text}</p>'
        f'</div>'
    )


def _delta_pct(cur_val: float, prev_val: float) -> float | None:
    if prev_val == 0:
        return None
    return (cur_val - prev_val) / prev_val * 100


def _safe_sum(df: pd.DataFrame, col: str) -> float:
    if df.empty or col not in df.columns:
        return 0
    return df[col].sum()


# ══════════════════════════════════════════════════════════════════════════════
#  MODE 1 — GA4 DATA LOADERS
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=3600)
def load_ga4_weekly_traffic() -> pd.DataFrame:
    query = """
    SELECT
      report_week,
      traffic_source_channel,
      users,
      new_users,
      sessions,
      pageviews,
      total_engagement_time_seconds,
      bounce_rate,
      bounced_sessions,
      demo_page_visits,
      contact_page_visits
    FROM `bi-ntop.aero_prod.ga4_weekly_traffic`
    WHERE report_week >= '2025-04-14'
    ORDER BY report_week, traffic_source_channel
    """
    df = _run(query)
    if not df.empty:
        df["report_week"] = pd.to_datetime(df["report_week"]).dt.date
    return df


@st.cache_data(ttl=3600)
def load_ga4_page_performance() -> pd.DataFrame:
    query = """
    SELECT
      report_week,
      page_group,
      page_path,
      page_title,
      pageviews,
      unique_pageviews,
      users,
      sessions,
      entrances,
      total_engagement_time_seconds,
      avg_time_on_page_seconds,
      entrance_rate,
      scroll_rate,
      bounce_rate,
      bounced_sessions,
      exits,
      exit_rate,
      demo_conversions,
      contact_conversions,
      form_interactions,
      conversion_rate
    FROM `bi-ntop.aero_prod.ga4_page_performance`
    WHERE report_week >= '2025-04-14'
    ORDER BY report_week, pageviews DESC
    """
    df = _run(query)
    if not df.empty:
        df["report_week"] = pd.to_datetime(df["report_week"]).dt.date
    return df


# ══════════════════════════════════════════════════════════════════════════════
#  MODE 2 — WARMLY DATA LOADERS
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=3600)
def load_company_lookup() -> pd.DataFrame:
    query = f"""
    SELECT
      w.account_id,
      COALESCE(
        acct.name,
        JSON_VALUE(w.features, '$.company_name'),
        JSON_VALUE(w.features, '$.name'),
        w.account_id
      ) AS company_name,
      {_TIER_CASE} AS tier
    FROM `bi-ntop.aero_prod.warmly_event_stream` w
    LEFT JOIN `bi-ntop.salesforce.account` acct
      ON w.account_id = acct.id
      AND acct._fivetran_deleted = FALSE
    WHERE DATE(w.ts) >= '2025-06-01'
    GROUP BY w.account_id, company_name, tier
    """
    return _run(query)


@st.cache_data(ttl=3600)
def load_warmly_by_tier(grain: str) -> pd.DataFrame:
    trunc = _trunc_expr("w.ts", grain)
    query = f"""
    SELECT
      {trunc} AS period,
      {_TIER_CASE} AS tier,
      COUNT(*) AS page_views,
      COUNT(DISTINCT w.account_id) AS distinct_companies
    FROM `bi-ntop.aero_prod.warmly_event_stream` w
    LEFT JOIN `bi-ntop.salesforce.account` acct
      ON w.account_id = acct.id
      AND acct._fivetran_deleted = FALSE
    WHERE DATE(w.ts) >= '2025-06-01'
    GROUP BY period, tier
    ORDER BY period, tier
    """
    df = _run(query)
    if not df.empty:
        df["period"] = pd.to_datetime(df["period"]).dt.date
    return df


@st.cache_data(ttl=3600)
def load_warmly_by_company(grain: str, account_ids: tuple[str, ...]) -> pd.DataFrame:
    if not account_ids:
        return pd.DataFrame(columns=["period", "account_id", "page_views"])
    placeholders = ", ".join(f"'{aid}'" for aid in account_ids)
    trunc = _trunc_expr("w.ts", grain)
    query = f"""
    SELECT
      {trunc} AS period,
      w.account_id,
      COUNT(*) AS page_views
    FROM `bi-ntop.aero_prod.warmly_event_stream` w
    WHERE DATE(w.ts) >= '2025-06-01'
      AND w.account_id IN ({placeholders})
    GROUP BY period, w.account_id
    ORDER BY period
    """
    df = _run(query)
    if not df.empty:
        df["period"] = pd.to_datetime(df["period"]).dt.date
    return df


@st.cache_data(ttl=3600)
def load_warmly_kpis_and_table(
    grain: str, tiers: tuple[str, ...], start: date, end: date
) -> pd.DataFrame:
    if not tiers:
        return pd.DataFrame()
    tier_list = ", ".join(f"'{t}'" for t in tiers)
    query = f"""
    SELECT
      w.account_id,
      COALESCE(
        acct.name,
        JSON_VALUE(w.features, '$.company_name'),
        JSON_VALUE(w.features, '$.name'),
        w.account_id
      ) AS company_name,
      {_TIER_CASE} AS tier,
      COUNT(*) AS total_page_views,
      COUNT(DISTINCT DATE(w.ts)) AS distinct_days_active,
      MAX(DATE(w.ts)) AS last_seen
    FROM `bi-ntop.aero_prod.warmly_event_stream` w
    LEFT JOIN `bi-ntop.salesforce.account` acct
      ON w.account_id = acct.id
      AND acct._fivetran_deleted = FALSE
    WHERE DATE(w.ts) >= '{start.isoformat()}'
      AND DATE(w.ts) <= '{end.isoformat()}'
    GROUP BY w.account_id, company_name, tier
    HAVING tier IN ({tier_list})
    ORDER BY total_page_views DESC
    """
    return _run(query)


# ══════════════════════════════════════════════════════════════════════════════
#  MODE TOGGLE
# ══════════════════════════════════════════════════════════════════════════════
mode = st.radio(
    "**Data mode**",
    ["All Traffic (GA4)", "Identified Companies (Warmly)"],
    horizontal=True,
    key="brian_mode",
)


# ══════════════════════════════════════════════════════════════════════════════
#  MODE 1 — ALL TRAFFIC (GA4)
# ══════════════════════════════════════════════════════════════════════════════
if mode == "All Traffic (GA4)":

    # ── Load data ────────────────────────────────────────────────────────────
    with st.spinner("Loading GA4 weekly traffic..."):
        ga4_df = load_ga4_weekly_traffic()
    with st.spinner("Loading GA4 page performance..."):
        page_df = load_ga4_page_performance()

    if ga4_df.empty:
        st.warning("No GA4 weekly traffic data found.")
        st.stop()

    # ── Date range + filters (compact row) ───────────────────────────────────
    all_weeks = sorted(ga4_df["report_week"].unique())
    default_end = all_weeks[-1]
    default_start = max(all_weeks[0], default_end - timedelta(days=90))
    default_start = _snap_to_monday(default_start)

    fc1, fc2, fc3, fc4 = st.columns(4)
    with fc1:
        range_start = st.date_input(
            "Start (snaps to Mon)",
            value=default_start,
            min_value=all_weeks[0],
            max_value=default_end,
            key="ga4_start",
        )
    with fc2:
        range_end = st.date_input(
            "End",
            value=default_end,
            min_value=all_weeks[0],
            max_value=all_weeks[-1],
            key="ga4_end",
        )

    range_start = _snap_to_monday(range_start)

    channel_options = sorted(
        ga4_df[ga4_df["traffic_source_channel"] != "All Sources"]["traffic_source_channel"]
        .dropna().unique().tolist()
    )
    page_type_options = []
    if not page_df.empty:
        page_type_options = sorted(page_df["page_group"].dropna().unique().tolist())

    with fc3:
        selected_channels = st.multiselect(
            "Channel",
            options=channel_options,
            default=[],
            key="ga4_channel_filter",
            placeholder="All channels",
        )
    with fc4:
        selected_page_types = st.multiselect(
            "Page Type",
            options=page_type_options,
            default=[],
            key="ga4_page_type_filter",
            placeholder="All page types",
        )

    # ── Compute current + prior period ───────────────────────────────────────
    all_src = ga4_df[ga4_df["traffic_source_channel"] == "All Sources"].copy()
    cur = all_src[
        (all_src["report_week"] >= range_start) & (all_src["report_week"] <= range_end)
    ]
    range_days = (range_end - range_start).days
    prior_end = range_start - timedelta(days=1)
    prior_start = _snap_to_monday(prior_end - timedelta(days=range_days))
    prev = all_src[
        (all_src["report_week"] >= prior_start) & (all_src["report_week"] <= prior_end)
    ]

    # ── KPI cards ────────────────────────────────────────────────────────────
    st.markdown('<hr class="section-rule">', unsafe_allow_html=True)

    cur_views = _safe_sum(cur, "pageviews")
    cur_sessions = _safe_sum(cur, "sessions")
    cur_users = _safe_sum(cur, "users")
    cur_eng = _safe_sum(cur, "total_engagement_time_seconds")
    cur_avg_dur = cur_eng / cur_sessions if cur_sessions else 0
    cur_key_events = _safe_sum(cur, "demo_page_visits") + _safe_sum(cur, "contact_page_visits")

    prev_views = _safe_sum(prev, "pageviews")
    prev_sessions = _safe_sum(prev, "sessions")
    prev_users = _safe_sum(prev, "users")
    prev_eng = _safe_sum(prev, "total_engagement_time_seconds")
    prev_avg_dur = prev_eng / prev_sessions if prev_sessions else 0
    prev_key_events = _safe_sum(prev, "demo_page_visits") + _safe_sum(prev, "contact_page_visits")

    kpi_data = [
        ("Views", f"{cur_views:,.0f}", _delta_pct(cur_views, prev_views)),
        ("Sessions", f"{cur_sessions:,.0f}", _delta_pct(cur_sessions, prev_sessions)),
        ("Active Users", f"{cur_users:,.0f}", _delta_pct(cur_users, prev_users)),
        ("Avg Duration", _format_duration(cur_avg_dur), _delta_pct(cur_avg_dur, prev_avg_dur)),
        ("Key Events", f"{cur_key_events:,.0f}", _delta_pct(cur_key_events, prev_key_events)),
    ]

    kpi_cols = st.columns(len(kpi_data))
    for col, (label, value, delta) in zip(kpi_cols, kpi_data):
        col.markdown(_kpi_card_html(label, value, delta), unsafe_allow_html=True)

    st.markdown('<hr class="section-rule">', unsafe_allow_html=True)

    # ── Donut + dual line charts (3-column layout) ───────────────────────────
    col_donut, col_views_chart, col_ke_chart = st.columns([1, 1, 1])

    # -- Channel donut --
    channels_df = ga4_df[
        (ga4_df["traffic_source_channel"] != "All Sources")
        & (ga4_df["report_week"] >= range_start)
        & (ga4_df["report_week"] <= range_end)
    ]
    if selected_channels:
        channels_df = channels_df[channels_df["traffic_source_channel"].isin(selected_channels)]

    with col_donut:
        if not channels_df.empty:
            ch_agg = (
                channels_df.groupby("traffic_source_channel")["sessions"]
                .sum()
                .sort_values(ascending=False)
                .reset_index()
            )
            ch_total = ch_agg["sessions"].sum()
            ch_agg["pct"] = (ch_agg["sessions"] / ch_total * 100).round(1)

            fig_donut = go.Figure(data=[go.Pie(
                labels=ch_agg["traffic_source_channel"],
                values=ch_agg["sessions"],
                hole=0.55,
                marker=dict(colors=CHANNEL_COLORS[:len(ch_agg)]),
                textinfo="none",
                hovertemplate="%{label}<br>%{value:,d} sessions (%{percent})<extra></extra>",
            )])
            fig_donut.update_layout(
                title=dict(
                    text="Sessions by Channel",
                    font=dict(family="Oswald", size=18, color=_C["black"]),
                    y=0.97, yanchor="top",
                ),
                height=400,
                margin=dict(l=8, r=8, t=56, b=8),
                paper_bgcolor="white",
                showlegend=True,
                legend=dict(
                    font=dict(size=10, family="IBM Plex Sans"),
                    orientation="v",
                    yanchor="middle", y=0.5,
                ),
            )
            st.plotly_chart(fig_donut, use_container_width=True)

    # -- Views over time --
    with col_views_chart:
        if not cur.empty:
            cur_sorted = cur.sort_values("report_week")
            prev_sorted = prev.sort_values("report_week")
            fig_views = go.Figure()
            if not prev_sorted.empty:
                offset = (range_start - prior_start).days
                prev_shifted = [d + timedelta(days=offset) for d in prev_sorted["report_week"]]
                fig_views.add_trace(go.Scatter(
                    x=prev_shifted, y=prev_sorted["pageviews"],
                    mode="lines", name="Prior period",
                    line=dict(color=_C["gray_mid"], width=1.5, dash="dot"),
                    hovertemplate="%{x}: %{y:,d} (prior)<extra></extra>",
                ))
            fig_views.add_trace(go.Scatter(
                x=cur_sorted["report_week"], y=cur_sorted["pageviews"],
                mode="lines+markers", name="Current",
                line=dict(color=_C["blue"], width=2.5),
                marker=dict(size=4),
                hovertemplate="%{x}: %{y:,d} views<extra></extra>",
            ))
            _base_layout(fig_views, "Views (weekly)")
            st.plotly_chart(fig_views, use_container_width=True)

    # -- Key events over time --
    with col_ke_chart:
        if not cur.empty:
            cur_ke = cur.sort_values("report_week").copy()
            cur_ke["key_events"] = cur_ke["demo_page_visits"].fillna(0) + cur_ke["contact_page_visits"].fillna(0)
            prev_ke = prev.sort_values("report_week").copy()
            if not prev_ke.empty:
                prev_ke["key_events"] = prev_ke["demo_page_visits"].fillna(0) + prev_ke["contact_page_visits"].fillna(0)

            fig_ke = go.Figure()
            if not prev_ke.empty:
                offset = (range_start - prior_start).days
                prev_shifted = [d + timedelta(days=offset) for d in prev_ke["report_week"]]
                fig_ke.add_trace(go.Scatter(
                    x=prev_shifted, y=prev_ke["key_events"],
                    mode="lines", name="Prior period",
                    line=dict(color=_C["gray_mid"], width=1.5, dash="dot"),
                    hovertemplate="%{x}: %{y:,d} (prior)<extra></extra>",
                ))
            fig_ke.add_trace(go.Scatter(
                x=cur_ke["report_week"], y=cur_ke["key_events"],
                mode="lines+markers", name="Current",
                line=dict(color=_C["green"], width=2.5),
                marker=dict(size=4),
                hovertemplate="%{x}: %{y:,d} key events<extra></extra>",
            ))
            _base_layout(fig_ke, "Key Events (weekly)")
            st.plotly_chart(fig_ke, use_container_width=True)

    # ── Page table (dense, analytics-style) ──────────────────────────────────
    st.markdown('<hr class="section-rule">', unsafe_allow_html=True)

    if not page_df.empty:
        pt = page_df[
            (~page_df["page_path"].isin(["ALL", ""]))
            & (page_df["report_week"] >= range_start)
            & (page_df["report_week"] <= range_end)
        ].copy()

        if selected_page_types:
            pt = pt[pt["page_group"].isin(selected_page_types)]
        if selected_channels:
            st.caption("Channel filter scopes the charts; the page table shows all channels (page data isn't broken out by channel).")

        if pt.empty:
            st.info("No page data for the selected filters and date range.")
        else:
            page_agg = (
                pt.groupby(["page_path", "page_title"])
                .agg(
                    views=("pageviews", "sum"),
                    users=("users", "sum"),
                    sessions=("sessions", "sum"),
                    avg_time=("avg_time_on_page_seconds", "mean"),
                    bounce_rate=("bounce_rate", "mean"),
                    demo_conv=("demo_conversions", "sum"),
                    contact_conv=("contact_conversions", "sum"),
                    conv_rate=("conversion_rate", "mean"),
                )
                .reset_index()
                .sort_values("views", ascending=False)
            )

            total_rows = len(page_agg)
            total_page_count = math.ceil(total_rows / ROWS_PER_PAGE)

            col_title, col_pager = st.columns([3, 1])
            with col_title:
                st.markdown(
                    f'<p style="font-family:Oswald;font-size:20px;font-weight:700;'
                    f'color:{_C["black"]};margin:0;">Pages</p>',
                    unsafe_allow_html=True,
                )
            with col_pager:
                page_num = st.number_input(
                    f"Page (of {total_page_count})",
                    min_value=1,
                    max_value=max(1, total_page_count),
                    value=1,
                    step=1,
                    key="page_table_page",
                    label_visibility="collapsed",
                )

            st.caption(f"Showing {min(ROWS_PER_PAGE, total_rows)} of {total_rows:,} pages  |  Page {page_num} of {total_page_count}")

            start_idx = (page_num - 1) * ROWS_PER_PAGE
            end_idx = start_idx + ROWS_PER_PAGE
            page_slice = page_agg.iloc[start_idx:end_idx]

            # Build HTML table
            header_cols = [
                ("Page Path", ""),
                ("Page Title", ""),
                ("Views", "num"),
                ("Users", "num"),
                ("Sessions", "num"),
                ("Avg Time", "num"),
                ("Bounce", "num"),
                ("Demo Conv", "num"),
                ("Contact Conv", "num"),
                ("Conv Rate", "num"),
            ]
            thead = "".join(
                f'<th class="{cls}">{name}</th>' for name, cls in header_cols
            )

            rows_html = []
            for _, r in page_slice.iterrows():
                avg_t = _format_duration(r["avg_time"])
                br = f"{r['bounce_rate']:.1f}%" if pd.notna(r["bounce_rate"]) else "—"
                cr = f"{r['conv_rate']:.2f}%" if pd.notna(r["conv_rate"]) else "—"
                rows_html.append(
                    f'<tr>'
                    f'<td class="path" title="{r["page_path"]}">{r["page_path"]}</td>'
                    f'<td class="title" title="{r["page_title"]}">{r["page_title"]}</td>'
                    f'<td class="num">{r["views"]:,.0f}</td>'
                    f'<td class="num">{r["users"]:,.0f}</td>'
                    f'<td class="num">{r["sessions"]:,.0f}</td>'
                    f'<td class="num">{avg_t}</td>'
                    f'<td class="num">{br}</td>'
                    f'<td class="num">{r["demo_conv"]:,.0f}</td>'
                    f'<td class="num">{r["contact_conv"]:,.0f}</td>'
                    f'<td class="num">{cr}</td>'
                    f'</tr>'
                )

            table_html = (
                f'<table class="dense-table">'
                f'<thead><tr>{thead}</tr></thead>'
                f'<tbody>{"".join(rows_html)}</tbody>'
                f'</table>'
            )
            st.markdown(table_html, unsafe_allow_html=True)
    else:
        st.warning("No GA4 page performance data found.")


# ══════════════════════════════════════════════════════════════════════════════
#  MODE 2 — IDENTIFIED COMPANIES (WARMLY)
# ══════════════════════════════════════════════════════════════════════════════
elif mode == "Identified Companies (Warmly)":

    # ── Controls (compact row) ───────────────────────────────────────────────
    fc1, fc2, fc3 = st.columns([1, 1, 1])
    with fc1:
        grain = st.radio(
            "Time grain",
            ["Daily", "Weekly", "Monthly"],
            index=2,
            horizontal=True,
            key="warmly_grain",
        )
    with fc2:
        warmly_start = st.date_input(
            "Start date",
            value=WARMLY_START,
            min_value=WARMLY_START,
            key="warmly_start",
        )
    with fc3:
        warmly_end = st.date_input(
            "End date",
            value=date.today(),
            min_value=WARMLY_START,
            key="warmly_end",
        )

    tier_selection = st.multiselect(
        "Tiers",
        options=TIER_ORDER,
        default=[t for t in TIER_ORDER if t in DEFAULT_TIERS_ON],
        key="warmly_tier_select",
    )

    # ── Company search ───────────────────────────────────────────────────────
    with st.spinner("Loading company list..."):
        company_lookup_df = load_company_lookup()

    selected_companies: list[str] = []
    name_to_id: dict[str, str] = {}
    if not company_lookup_df.empty:
        clean_lookup = company_lookup_df.dropna(subset=["company_name"])
        if tier_selection:
            clean_lookup = clean_lookup[clean_lookup["tier"].isin(tier_selection)]
        name_to_id = dict(zip(clean_lookup["company_name"], clean_lookup["account_id"]))
        sorted_names = sorted(name_to_id.keys(), key=lambda s: s.lower())

        selected_companies = st.multiselect(
            "Search and select companies",
            options=sorted_names,
            default=[],
            key="warmly_company_select",
            placeholder="Type to search companies...",
        )

    # ── KPI cards ────────────────────────────────────────────────────────────
    st.markdown('<hr class="section-rule">', unsafe_allow_html=True)

    with st.spinner("Loading Warmly data..."):
        tier_ts_df = load_warmly_by_tier(grain)
        company_table_df = load_warmly_kpis_and_table(
            grain, tuple(tier_selection) if tier_selection else (), warmly_start, warmly_end
        )

    if not tier_ts_df.empty and tier_selection:
        filt = tier_ts_df[
            (tier_ts_df["tier"].isin(tier_selection))
            & (tier_ts_df["period"] >= warmly_start)
            & (tier_ts_df["period"] <= warmly_end)
        ]
        total_views = filt["page_views"].sum() if not filt.empty else 0
        distinct_cos = company_table_df["account_id"].nunique() if not company_table_df.empty else 0
        distinct_days = company_table_df["distinct_days_active"].max() if not company_table_df.empty else 0

        k1, k2, k3 = st.columns(3)
        with k1:
            st.markdown(
                _kpi_card_html("Identified Page Views", f"{total_views:,.0f}", None),
                unsafe_allow_html=True,
            )
        with k2:
            st.markdown(
                _kpi_card_html("Distinct Companies", f"{distinct_cos:,.0f}", None),
                unsafe_allow_html=True,
            )
        with k3:
            st.markdown(
                _kpi_card_html("Max Days Active", f"{distinct_days:,.0f}" if pd.notna(distinct_days) else "0", None),
                unsafe_allow_html=True,
            )
        st.caption("Identified-traffic only — a small fraction of total site visitors.")

    # ── Traffic by Tier over time ────────────────────────────────────────────
    if not tier_ts_df.empty and tier_selection:
        filt = tier_ts_df[
            (tier_ts_df["tier"].isin(tier_selection))
            & (tier_ts_df["period"] >= warmly_start)
            & (tier_ts_df["period"] <= warmly_end)
        ]

        metric_toggle = st.radio(
            "Metric", ["Page views", "Distinct companies"],
            horizontal=True, key="warmly_metric",
        )
        y_col = "page_views" if metric_toggle == "Page views" else "distinct_companies"
        y_label = metric_toggle

        fig_tier = go.Figure()
        for tier in TIER_ORDER:
            if tier not in tier_selection:
                continue
            t_df = filt[filt["tier"] == tier]
            if t_df.empty:
                continue
            fig_tier.add_trace(go.Scatter(
                x=t_df["period"], y=t_df[y_col],
                mode="lines+markers",
                name=tier,
                line=dict(color=TIER_COLORS.get(tier, "#999"), width=2),
                marker=dict(size=4),
                hovertemplate=f"%{{x}}: %{{y:,d}} {y_label.lower()}<extra>{tier}</extra>",
            ))
        tick_fmt = "%b %Y" if grain == "Monthly" else "%b %d"
        _base_layout(fig_tier, f"{y_label} by Tier Over Time", height=420, tick_fmt=tick_fmt)
        st.plotly_chart(fig_tier, use_container_width=True)
    elif not tier_selection:
        st.info("Select at least one tier to display charts.")

    # ── Traffic by Company over time ─────────────────────────────────────────
    if selected_companies:
        selected_ids = tuple(name_to_id[n] for n in selected_companies)
        id_to_name = {v: k for k, v in name_to_id.items() if k in selected_companies}

        with st.spinner("Loading company traffic..."):
            company_ts_df = load_warmly_by_company(grain, selected_ids)

        if not company_ts_df.empty:
            co_filt = company_ts_df[
                (company_ts_df["period"] >= warmly_start)
                & (company_ts_df["period"] <= warmly_end)
            ]
            fig_co = go.Figure()
            for i, aid in enumerate(selected_ids):
                cname = id_to_name.get(aid, aid)
                c_df = co_filt[co_filt["account_id"] == aid]
                if c_df.empty:
                    continue
                fig_co.add_trace(go.Scatter(
                    x=c_df["period"], y=c_df["page_views"],
                    mode="lines+markers",
                    name=cname,
                    line=dict(color=COMPANY_COLORS[i % len(COMPANY_COLORS)], width=2),
                    marker=dict(size=5),
                    hovertemplate=f"%{{x}}: %{{y:,d}} page views<extra>{cname}</extra>",
                ))
            tick_fmt = "%b %Y" if grain == "Monthly" else "%b %d"
            _base_layout(fig_co, "Page Views by Company Over Time", height=420, tick_fmt=tick_fmt)
            st.plotly_chart(fig_co, use_container_width=True)
        else:
            st.info("No traffic data found for the selected companies in this range.")

    # ── Company table (dense, analytics-style) ───────────────────────────────
    st.markdown('<hr class="section-rule">', unsafe_allow_html=True)

    if not company_table_df.empty:
        st.markdown(
            f'<p style="font-family:Oswald;font-size:20px;font-weight:700;'
            f'color:{_C["black"]};margin:0 0 6px 0;">Company Table</p>',
            unsafe_allow_html=True,
        )
        st.caption(f"{len(company_table_df)} companies in selected tiers.")

        header_cols = [
            ("Company", ""),
            ("Tier", ""),
            ("Total Views", "num"),
            ("Days Active", "num"),
            ("Last Seen", "num"),
        ]
        thead = "".join(f'<th class="{cls}">{name}</th>' for name, cls in header_cols)

        rows_html = []
        for _, r in company_table_df.iterrows():
            ls = str(r["last_seen"]) if pd.notna(r["last_seen"]) else "—"
            rows_html.append(
                f'<tr>'
                f'<td>{r["company_name"]}</td>'
                f'<td>{r["tier"]}</td>'
                f'<td class="num">{r["total_page_views"]:,.0f}</td>'
                f'<td class="num">{r["distinct_days_active"]:,.0f}</td>'
                f'<td class="num">{ls}</td>'
                f'</tr>'
            )

        table_html = (
            f'<table class="dense-table">'
            f'<thead><tr>{thead}</tr></thead>'
            f'<tbody>{"".join(rows_html)}</tbody>'
            f'</table>'
        )
        st.markdown(table_html, unsafe_allow_html=True)
    else:
        st.info("No company data for the selected tiers and date range.")
