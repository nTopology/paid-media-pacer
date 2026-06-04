"""
Web Traffic Explorer — v1.0

Three sections:
  1. Total site traffic (GA4 sessions)
  2. Identified-company traffic by Tier (Warmly)
  3. Company lookup (Warmly + Salesforce)
"""

from __future__ import annotations

import base64
import json
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from google.cloud import bigquery
from google.oauth2 import service_account


# ── Brand assets ───────────────────────────────────────────────────────────────
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
_PNG_FILE  = _LOGO_DIR / "nTop-Logo_Light-theme_400w.png"

if _SVG_FILE.exists():
    _logo_b64      = base64.b64encode(_SVG_FILE.read_bytes()).decode()
    _LOGO_IMG_HTML = (
        f'<img src="data:image/svg+xml;base64,{_logo_b64}" '
        f'height="54" style="display:block;flex-shrink:0;">'
    )
elif _PNG_FILE.exists():
    _logo_b64      = base64.b64encode(_PNG_FILE.read_bytes()).decode()
    _LOGO_IMG_HTML = (
        f'<img src="data:image/png;base64,{_logo_b64}" '
        f'height="54" style="display:block;flex-shrink:0;">'
    )
else:
    _LOGO_IMG_HTML = (
        f'<span style="font-family:Oswald,sans-serif;font-weight:700;'
        f'font-size:22px;color:{_C["black"]};">nTop</span>'
    )


# ── Constants ─────────────────────────────────────────────────────────────────
SERVICE_ACCOUNT_FILE = "service-account.json"
GCP_PROJECT          = "bi-ntop"

GA4_START   = date(2025, 4, 16)
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
    "Tier 1A (Big 3)":                   "#D43F3F",
    "Tier 1 (Primes)":                   "#0047FF",
    "Tier 2 (Growth OEMs)":              "#F5A623",
    "Tier 3 (Programs, Depots, Labs)":   "#1FA34E",
    "Tier 4 (Watchlist)":                "#888888",
    "Untiered":                          "#CCCCCC",
}

COMPANY_COLORS = [
    "#0047FF", "#D43F3F", "#1FA34E", "#F5A623", "#9B59B6",
    "#E67E22", "#1ABC9C", "#E74C3C", "#3498DB", "#2ECC71",
]


# ── Brand CSS ─────────────────────────────────────────────────────────────────
_BRAND_CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Oswald:wght@700&family=IBM+Plex+Sans:wght@400;700&family=IBM+Plex+Mono&display=swap');

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
.ntop-header {{
    display: flex;
    align-items: center;
    gap: 20px;
    padding-bottom: 20px;
    margin-bottom: 24px;
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
    margin: 0 0 5px 0;
}}
.ntop-page-subtitle {{
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 13px;
    color: {_C["gray_dark"]};
    margin: 0;
    line-height: 1.4;
}}
</style>
"""


# ── Page setup ────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Web Traffic", layout="wide")
st.markdown(_BRAND_CSS, unsafe_allow_html=True)

if _PNG_FILE.exists():
    st.logo(str(_PNG_FILE))

st.markdown(
    f'<div class="ntop-header">'
    f'{_LOGO_IMG_HTML}'
    f'<div class="ntop-header-text">'
    f'<div class="ntop-page-title">Web Traffic Explorer</div>'
    f'<div class="ntop-page-subtitle">'
    f'Website traffic from GA4 (all visitors) and Warmly (identified companies). '
    f'Filter by A&amp;D tier and look up individual companies.'
    f'</div></div></div>',
    unsafe_allow_html=True,
)


# ── About this data ───────────────────────────────────────────────────────────
with st.expander("About this data"):
    st.markdown(
        "- **Total traffic** is from GA4 and counts all visitors; only ~9% can "
        "be tied to a company.\n"
        "- **Tier and Company** views are from Warmly, which only covers visitors "
        "it could identify by company — a small fraction of total traffic. The two "
        "views are different populations and won't add up to the same totals.\n"
        "- **Coverage start dates:** total traffic from mid-April 2025; "
        "company/tier data from June 2025. There is no website data before these "
        "dates in the warehouse.\n"
        "- **Tiering:** based on the Salesforce aircraft-concept target type, with "
        "Boeing / Lockheed Martin / Northrop Grumman grouped as \"Tier 1A (Big 3)\". "
        "Most identified companies are Untiered (no A&D target tier assigned)."
    )


# ── Auth ──────────────────────────────────────────────────────────────────────
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


# ── SQL helpers ───────────────────────────────────────────────────────────────
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

# -- loose %boeing% also catches "Aviation Partners Boeing" (JV, not Boeing proper) — acceptable for v1


# ── Data loaders ──────────────────────────────────────────────────────────────

@st.cache_data(ttl=3600)
def load_ga4_sessions(grain: str) -> pd.DataFrame:
    trunc = _trunc_expr("ts", grain)
    query = f"""
    SELECT
      {trunc} AS period,
      COUNT(*) AS sessions
    FROM `bi-ntop.aero_prod.ga4_events`
    WHERE activity = 'session_start'
      AND DATE(ts) >= '2025-04-16'
    GROUP BY period
    ORDER BY period
    """
    df = _run(query)
    if not df.empty:
        df["period"] = pd.to_datetime(df["period"]).dt.date
    return df


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
def load_company_summary(account_ids: tuple[str, ...]) -> pd.DataFrame:
    if not account_ids:
        return pd.DataFrame()
    placeholders = ", ".join(f"'{aid}'" for aid in account_ids)
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
    WHERE DATE(w.ts) >= '2025-06-01'
      AND w.account_id IN ({placeholders})
    GROUP BY w.account_id, company_name, tier
    ORDER BY total_page_views DESC
    """
    return _run(query)


# ── Chart helpers ─────────────────────────────────────────────────────────────
def _base_layout(fig: go.Figure, title: str, grain: str, height: int = 450) -> None:
    tick_fmt = "%b %Y" if grain == "Monthly" else "%b %d" if grain == "Weekly" else "%b %d"
    fig.update_layout(
        title=dict(text=title, font=dict(family="Oswald", size=20, color=_C["black"])),
        height=height,
        margin=dict(l=60, r=40, t=60, b=40),
        hovermode="x unified",
        plot_bgcolor="white",
        paper_bgcolor="white",
        legend=dict(
            orientation="h",
            yanchor="bottom", y=1.02,
            xanchor="left", x=0,
            font=dict(size=12, family="IBM Plex Sans"),
        ),
        xaxis=dict(
            tickformat=tick_fmt,
            showgrid=True, gridcolor="#F0F0F0",
        ),
        yaxis=dict(
            showgrid=True, gridcolor="#F0F0F0",
            tickformat=",d",
            dtick=None,  # let Plotly auto-pick, but force integers
            rangemode="tozero",
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — Total Site Traffic (GA4)
# ══════════════════════════════════════════════════════════════════════════════
st.markdown("---")
st.subheader("Total Site Traffic (GA4, all visitors)")

grain_ga4 = st.radio(
    "Time grain", ["Daily", "Weekly", "Monthly"],
    index=1, horizontal=True, key="grain_ga4",
)

with st.spinner("Loading GA4 sessions…"):
    ga4_df = load_ga4_sessions(grain_ga4)

if ga4_df.empty:
    st.warning("No GA4 session data found.")
else:
    fig1 = go.Figure()
    fig1.add_trace(go.Scatter(
        x=ga4_df["period"], y=ga4_df["sessions"],
        mode="lines+markers",
        name="Sessions",
        line=dict(color=_C["blue"], width=2),
        marker=dict(size=4),
        hovertemplate="%{x}: %{y:,d} sessions<extra></extra>",
    ))
    _base_layout(fig1, "Sessions per period", grain_ga4)
    st.plotly_chart(fig1, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — Traffic by Tier (Warmly)
# ══════════════════════════════════════════════════════════════════════════════
st.markdown("---")
st.subheader("Identified-Company Traffic by Tier (Warmly)")

col_grain2, col_metric2 = st.columns([1, 1])
with col_grain2:
    grain_warmly = st.radio(
        "Time grain", ["Daily", "Weekly", "Monthly"],
        index=2, horizontal=True, key="grain_warmly",
    )
with col_metric2:
    metric_warmly = st.radio(
        "Metric", ["Page views", "Distinct companies"],
        index=0, horizontal=True, key="metric_warmly",
    )

tier_selection = st.multiselect(
    "Tiers",
    options=TIER_ORDER,
    default=[t for t in TIER_ORDER if t in DEFAULT_TIERS_ON],
    key="tier_select",
)

with st.spinner("Loading Warmly tier data…"):
    tier_df = load_warmly_by_tier(grain_warmly)

if tier_df.empty:
    st.warning("No Warmly tier data found.")
elif not tier_selection:
    st.info("Select at least one tier to display the chart.")
else:
    y_col = "page_views" if metric_warmly == "Page views" else "distinct_companies"
    y_label = "Page views" if metric_warmly == "Page views" else "Distinct companies"

    fig2 = go.Figure()
    for tier in TIER_ORDER:
        if tier not in tier_selection:
            continue
        t_df = tier_df[tier_df["tier"] == tier]
        if t_df.empty:
            continue
        fig2.add_trace(go.Scatter(
            x=t_df["period"], y=t_df[y_col],
            mode="lines+markers",
            name=tier,
            line=dict(color=TIER_COLORS.get(tier, "#999"), width=2),
            marker=dict(size=4),
            hovertemplate=f"%{{x}}: %{{y:,d}} {y_label.lower()}<extra>{tier}</extra>",
        ))
    _base_layout(fig2, f"{y_label} per period by tier", grain_warmly, height=500)
    st.plotly_chart(fig2, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — Company Lookup
# ══════════════════════════════════════════════════════════════════════════════
st.markdown("---")
st.subheader("Traffic by Company (Warmly)")

with st.spinner("Loading company list…"):
    company_lookup_df = load_company_lookup()

if company_lookup_df.empty:
    st.warning("No Warmly company data found.")
else:
    # Drop rows where company_name is None/NaN (unresolvable accounts)
    clean_lookup = company_lookup_df.dropna(subset=["company_name"])
    name_to_id = dict(zip(
        clean_lookup["company_name"],
        clean_lookup["account_id"],
    ))
    sorted_names = sorted(name_to_id.keys(), key=lambda s: s.lower())

    selected_companies = st.multiselect(
        "Search and select companies",
        options=sorted_names,
        default=[],
        key="company_select",
        placeholder="Type to search companies…",
    )

    grain_company = st.radio(
        "Time grain", ["Daily", "Weekly", "Monthly"],
        index=2, horizontal=True, key="grain_company",
    )

    if selected_companies:
        selected_ids = tuple(name_to_id[n] for n in selected_companies)
        id_to_name = {v: k for k, v in name_to_id.items() if k in selected_companies}

        with st.spinner("Loading company traffic…"):
            company_ts_df = load_warmly_by_company(grain_company, selected_ids)
            company_summary_df = load_company_summary(selected_ids)

        if not company_ts_df.empty:
            fig3 = go.Figure()
            for i, aid in enumerate(selected_ids):
                cname = id_to_name.get(aid, aid)
                c_df = company_ts_df[company_ts_df["account_id"] == aid]
                if c_df.empty:
                    continue
                fig3.add_trace(go.Scatter(
                    x=c_df["period"], y=c_df["page_views"],
                    mode="lines+markers",
                    name=cname,
                    line=dict(color=COMPANY_COLORS[i % len(COMPANY_COLORS)], width=2),
                    marker=dict(size=5),
                    hovertemplate=f"%{{x}}: %{{y:,d}} page views<extra>{cname}</extra>",
                ))
            _base_layout(fig3, "Page views per period by company", grain_company, height=450)
            st.plotly_chart(fig3, use_container_width=True)
        else:
            st.info("No traffic data found for the selected companies.")

        if not company_summary_df.empty:
            display_df = company_summary_df[[
                "company_name", "tier", "total_page_views",
                "distinct_days_active", "last_seen",
            ]].rename(columns={
                "company_name": "Company",
                "tier": "Tier",
                "total_page_views": "Total Page Views",
                "distinct_days_active": "Days Active",
                "last_seen": "Last Seen",
            })
            st.dataframe(
                display_df,
                use_container_width=True,
                hide_index=True,
            )
    else:
        st.info("Select one or more companies above to see their traffic.")
