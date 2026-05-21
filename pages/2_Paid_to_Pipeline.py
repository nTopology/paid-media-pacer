"""
Paid to Pipeline — v1.3

Occurrence-count funnel: six time-series rows on a shared x-axis.

  Row 1:  Paid Spend
  Row 2:  Engagement (paid media events)
  Row 3:  Inbound Demo Requests (Unmatched left y; Strategic/HV right y)
  Row 4:  Opportunities (generated left y; qualified right y)
  Row 5:  Pipeline ARR Created
  Row 6:  Sales Velocity ($/day, monthly only)
"""

from __future__ import annotations

import base64
import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import toml
from google.cloud import bigquery
from google.oauth2 import service_account
from plotly.subplots import make_subplots


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


# ── Constants ──────────────────────────────────────────────────────────────────
SERVICE_ACCOUNT_FILE = "service-account.json"
GCP_PROJECT          = "bi-ntop"

STRATEGIC_RT = frozenset({"012Qo00000AyuhVIAR", "012Qo00000Avzc5IAB"})
HV_RT        = frozenset({"0124R000001UuQlQAK", "0124R000001UuQgQAK"})
EXPANSION_RT = "0124R000001JIhxQAG"
RENEWAL_RT   = "0124R000001UuQqQAK"
_ALL_NB_EXP  = ", ".join(f"'{r}'" for r in [*STRATEGIC_RT, *HV_RT, EXPANSION_RT])

COLORS = {
    # Functional chart colors — do not rebrand; earned meaning over time
    "Strategic": "#0047FF",
    "HV":        "#F5A623",
    "Total":     "#1a1a1a",
    "Generated": "#1a1a1a",
    "Qualified": "#F5A623",
    "LinkedIn":  "#0A66C2",
    "Google":    "#34A853",
    "YouTube":   "#FF4444",
    "OOH":       "#888888",
    "Velocity":  "#FF6B35",
    "Unmatched": "#999999",
    # Pipeline ARR bars use exact nTop Blue — the one place brand and data color align
    "Pipeline":  _C["blue"],
}

NROWS       = 6
ROW_HEIGHTS = [0.18, 0.17, 0.14, 0.20, 0.17, 0.14]
V_SPACING   = 0.065
_ROW_Y = {1: 0.94, 2: 0.76, 3: 0.59, 4: 0.41, 5: 0.22, 6: 0.05}


# ── Brand CSS ──────────────────────────────────────────────────────────────────
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
/* Page header */
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
/* KPI band */
.kpi-band {{
    display: flex;
    gap: 16px;
    margin: 0 0 6px 0;
}}
.kpi-tile {{
    flex: 1;
    background: {_C["white"]};
    border: 1px solid {_C["gray_light"]};
    border-top: 3px solid {_C["black"]};
    padding: 16px 20px 14px;
    min-width: 0;
}}
.kpi-label {{
    font-family: 'IBM Plex Mono', monospace;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.10em;
    color: {_C["gray_mid"]};
    margin-bottom: 4px;
}}
.kpi-value {{
    font-family: 'Oswald', sans-serif;
    font-weight: 700;
    font-size: 26px;
    color: {_C["black"]};
    line-height: 1.1;
    margin-bottom: 4px;
}}
.kpi-delta-pos {{
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    color: {_C["green"]};
}}
.kpi-delta-neg {{
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    color: {_C["red"]};
}}
.kpi-delta-neutral {{
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    color: {_C["gray_mid"]};
}}
.kpi-note {{
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 11px;
    color: {_C["gray_mid"]};
    text-align: right;
    margin-bottom: 20px;
}}
</style>
"""


# ── Page setup ─────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Paid to Pipeline", layout="wide")
st.markdown(_BRAND_CSS, unsafe_allow_html=True)

if _PNG_FILE.exists():
    st.logo(str(_PNG_FILE))

st.markdown(
    f'<div class="ntop-header">'
    f'{_LOGO_IMG_HTML}'
    f'<div class="ntop-header-text">'
    f'<div class="ntop-page-title">Paid to Pipeline</div>'
    f'<div class="ntop-page-subtitle">'
    f'Top-to-bottom: where the money goes in, where it comes out. '
    f'Read left-to-right for trends; click legend items to isolate.'
    f'</div></div></div>',
    unsafe_allow_html=True,
)


# ── Auth ───────────────────────────────────────────────────────────────────────
@st.cache_resource
def get_credentials():
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    if "gcp_service_account" in st.secrets:
        return service_account.Credentials.from_service_account_info(
            st.secrets["gcp_service_account"], scopes=scopes
        )
    return service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=scopes
    )


@st.cache_resource
def get_bq_client():
    return bigquery.Client(credentials=get_credentials(), project=GCP_PROJECT)


# ── Events config ──────────────────────────────────────────────────────────────
def load_events() -> list[dict]:
    p = Path(__file__).parent.parent / "events.toml"
    if not p.exists():
        return []
    return toml.loads(p.read_text()).get("events", [])


# ── SQL helpers ────────────────────────────────────────────────────────────────
def _trunc(col: str, grain: str) -> str:
    return f"DATE_TRUNC({col}, {'MONTH' if grain == 'Monthly' else 'WEEK(MONDAY)'})"


def _pcol(grain: str) -> str:
    return "month_start" if grain == "Monthly" else "week_start"


def _opp_tier_case(o: str = "o", af: str = "af") -> str:
    return f"""CASE
          WHEN {o}.record_type_id IN ('012Qo00000AyuhVIAR', '012Qo00000Avzc5IAB') THEN 'Strategic'
          WHEN {o}.record_type_id IN ('0124R000001UuQlQAK', '0124R000001UuQgQAK') THEN 'HV'
          WHEN {o}.record_type_id = '{EXPANSION_RT}' THEN
            CASE WHEN {af}.account_segment IN ('a. Strategic', 'b. Enterprise')
                 THEN 'Strategic' ELSE 'HV' END
          ELSE NULL
        END"""


def _opp_tier_where(o: str = "o") -> str:
    """No is_closed/is_won gate — lets closed-lost opps count as 'generated'."""
    return f"""{o}._fivetran_deleted = FALSE
      AND {o}.record_type_id != '{RENEWAL_RT}'
      AND {o}.record_type_id IN ({_ALL_NB_EXP})
      AND {o}.stage_name != 'Rejected'"""


def _opp_base_where(o: str = "o") -> str:
    """Full filter with is_closed/is_won — use for pipeline value (don't sum lost ARR)."""
    return _opp_tier_where(o) + f"\n      AND ({o}.is_closed = FALSE OR {o}.is_won = TRUE)"


def _run(query: str, params: list) -> pd.DataFrame:
    job_config = bigquery.QueryJobConfig(query_parameters=params)
    return (
        get_bq_client()
        .query(query, job_config=job_config)
        .to_dataframe(create_bqstorage_client=False)
    )


def _date_params(start: date, end: date) -> list:
    return [
        bigquery.ScalarQueryParameter("start_date", "DATE", start),
        bigquery.ScalarQueryParameter("end_date",   "DATE", end),
    ]


def _to_date(df: pd.DataFrame, col: str) -> pd.DataFrame:
    if not df.empty:
        df[col] = pd.to_datetime(df[col]).dt.date
    return df


# ── KPI Summary (trailing 30 days) ────────────────────────────────────────────
@st.cache_data(ttl=3600)
def load_kpi_summary(as_of: date) -> dict:
    """Returns {metric: (current_30d_value, prior_30d_value)} for the four headline KPIs."""
    t30_end   = as_of
    t30_start = as_of - timedelta(days=29)
    p30_end   = as_of - timedelta(days=30)
    p30_start = as_of - timedelta(days=59)
    params = [
        bigquery.ScalarQueryParameter("t30_start", "DATE", t30_start),
        bigquery.ScalarQueryParameter("t30_end",   "DATE", t30_end),
        bigquery.ScalarQueryParameter("p30_start", "DATE", p30_start),
        bigquery.ScalarQueryParameter("p30_end",   "DATE", p30_end),
    ]
    out: dict = {}

    try:
        df = _run("""
        SELECT
          ROUND(SUM(CASE WHEN date_day BETWEEN @t30_start AND @t30_end THEN spend ELSE 0 END), 2) AS current_val,
          ROUND(SUM(CASE WHEN date_day BETWEEN @p30_start AND @p30_end THEN spend ELSE 0 END), 2) AS prior_val
        FROM `bi-ntop.aero_prod_ad_reporting.ad_reporting__campaign_report`
        WHERE date_day BETWEEN @p30_start AND @t30_end AND spend > 0
        """, params)
        out["spend"] = (float(df["current_val"].iloc[0] or 0), float(df["prior_val"].iloc[0] or 0))
    except Exception:
        out["spend"] = (0.0, 0.0)

    try:
        li = _run("""
        SELECT
          CAST(SUM(CASE WHEN DATE(a.day) BETWEEN @t30_start AND @t30_end
            THEN COALESCE(a.likes,0)+COALESCE(a.comments,0)+COALESCE(a.shares,0)+
                 COALESCE(a.follows,0)+COALESCE(a.reactions,0)+COALESCE(a.video_views,0)+COALESCE(a.clicks,0)
            ELSE 0 END) AS INT64) AS current_val,
          CAST(SUM(CASE WHEN DATE(a.day) BETWEEN @p30_start AND @p30_end
            THEN COALESCE(a.likes,0)+COALESCE(a.comments,0)+COALESCE(a.shares,0)+
                 COALESCE(a.follows,0)+COALESCE(a.reactions,0)+COALESCE(a.video_views,0)+COALESCE(a.clicks,0)
            ELSE 0 END) AS INT64) AS prior_val
        FROM `bi-ntop.linkedin_ads.ad_analytics_by_campaign` a
        WHERE DATE(a.day) BETWEEN @p30_start AND @t30_end
        """, params)
        goog = _run("""
        SELECT
          CAST(SUM(CASE WHEN date_day BETWEEN @t30_start AND @t30_end THEN COALESCE(clicks,0) ELSE 0 END) AS INT64) AS current_val,
          CAST(SUM(CASE WHEN date_day BETWEEN @p30_start AND @p30_end THEN COALESCE(clicks,0) ELSE 0 END) AS INT64) AS prior_val
        FROM `bi-ntop.aero_prod_ad_reporting.ad_reporting__campaign_report`
        WHERE date_day BETWEEN @p30_start AND @t30_end
          AND platform = 'google_ads' AND spend > 0
          AND campaign_name NOT LIKE 'PMAX%'
        """, params)
        c = int(li["current_val"].iloc[0] or 0) + int(goog["current_val"].iloc[0] or 0)
        p = int(li["prior_val"].iloc[0]  or 0) + int(goog["prior_val"].iloc[0]  or 0)
        out["engagement"] = (c, p)
    except Exception:
        out["engagement"] = (0, 0)

    try:
        df = _run("""
        SELECT
          COUNT(DISTINCT CASE WHEN DATE(cfs.timestamp) BETWEEN @t30_start AND @t30_end
            THEN cfs.contact_id END) AS current_val,
          COUNT(DISTINCT CASE WHEN DATE(cfs.timestamp) BETWEEN @p30_start AND @p30_end
            THEN cfs.contact_id END) AS prior_val
        FROM `bi-ntop.hubspot.contact_form_submission` cfs
        JOIN `bi-ntop.hubspot.form` f ON cfs.form_id = f.guid
        WHERE f._fivetran_deleted = FALSE
          AND LOWER(f.name) LIKE '%demo%'
          AND f.name NOT LIKE '%(FOR TESTING ONLY)%'
          AND DATE(cfs.timestamp) BETWEEN @p30_start AND @t30_end
        """, params)
        out["demos"] = (int(df["current_val"].iloc[0] or 0), int(df["prior_val"].iloc[0] or 0))
    except Exception:
        out["demos"] = (0, 0)

    base_where = _opp_base_where()
    try:
        df = _run(f"""
        SELECT
          ROUND(SUM(CASE WHEN DATE(o.created_date) BETWEEN @t30_start AND @t30_end
            THEN COALESCE(opf.arr, 0) ELSE 0 END), 2) AS current_val,
          ROUND(SUM(CASE WHEN DATE(o.created_date) BETWEEN @p30_start AND @p30_end
            THEN COALESCE(opf.arr, 0) ELSE 0 END), 2) AS prior_val
        FROM `bi-ntop.salesforce.opportunity` o
        LEFT JOIN `bi-ntop.google_sheets.opportunity_fields` opf ON opf.opportunity_id = o.id
        LEFT JOIN `bi-ntop.google_sheets.account_fields` af      ON af._18_digit_account_id = o.account_id
        WHERE {base_where}
          AND DATE(o.created_date) BETWEEN @p30_start AND @t30_end
        """, params)
        out["pipeline_arr"] = (float(df["current_val"].iloc[0] or 0), float(df["prior_val"].iloc[0] or 0))
    except Exception:
        out["pipeline_arr"] = (0.0, 0.0)

    return out


# ── KPI tile helpers ───────────────────────────────────────────────────────────
def _fmt_currency(v: float) -> str:
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}k"
    return f"${v:.0f}"


def _fmt_count(v: float) -> str:
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.0f}k"
    return f"{int(v):,}"


def _delta_html(current: float, prior: float) -> str:
    if prior == 0:
        return '<span class="kpi-delta-neutral">— no prior data</span>'
    pct   = (current - prior) / prior * 100
    arrow = "↑" if pct >= 0 else "↓"
    cls   = "kpi-delta-pos" if pct >= 0 else "kpi-delta-neg"
    return f'<span class="{cls}">{arrow}&nbsp;{abs(pct):.1f}% vs prior 30d</span>'


def _kpi_tile_html(label: str, value: str, delta: str) -> str:
    return (
        f'<div class="kpi-tile">'
        f'<div class="kpi-label">{label}</div>'
        f'<div class="kpi-value">{value}</div>'
        f'{delta}'
        f'</div>'
    )


# ── Layer 1: Paid Spend ────────────────────────────────────────────────────────
@st.cache_data(ttl=3600)
def load_spend(start: date, end: date, grain: str) -> pd.DataFrame:
    pcol  = _pcol(grain)
    trunc = _trunc("date_day", grain)
    query = f"""
    SELECT
        {trunc} AS {pcol},
        CASE
            WHEN platform = 'linkedin_ads'                            THEN 'LinkedIn'
            WHEN platform = 'google_ads'
             AND LOWER(campaign_name) LIKE '%video%'                  THEN 'YouTube'
            WHEN platform = 'google_ads'                              THEN 'Google'
            ELSE 'Other'
        END AS channel,
        ROUND(SUM(spend), 2) AS spend
    FROM `bi-ntop.aero_prod_ad_reporting.ad_reporting__campaign_report`
    WHERE date_day BETWEEN @start_date AND @end_date
      AND spend > 0
    GROUP BY {pcol}, channel
    ORDER BY {pcol}, channel
    """
    df = _to_date(_run(query, _date_params(start, end)), pcol)
    try:
        ooh_trunc = _trunc("month", grain)
        ooh = _to_date(_run(
            f"SELECT {ooh_trunc} AS {pcol}, 'OOH' AS channel, "
            f"ROUND(SUM(spend_usd), 2) AS spend "
            f"FROM `bi-ntop.google_sheets.ooh_spend` "
            f"WHERE month BETWEEN @start_date AND @end_date GROUP BY {pcol}",
            _date_params(start, end),
        ), pcol)
        if not ooh.empty:
            df = pd.concat([df, ooh], ignore_index=True)
    except Exception:
        pass
    return df


# ── Layer 2: Engagement ────────────────────────────────────────────────────────
@st.cache_data(ttl=3600)
def load_engagement(start: date, end: date, grain: str) -> pd.DataFrame:
    """
    LinkedIn: ad_analytics_by_campaign joined to campaign_history (Fivetran has no
    plain linkedin_ads.campaign table). Dedup to latest row per campaign id.
    LEFT JOIN keeps orphan campaign_ids as Unmapped rather than dropping them.
    Tier patterns cover both 'Strategic' and the Q3+ typo 'Stategic', plus 'HV-Full_'.
    Google/YouTube: clicks from aero_prod report. PMAX_ prefix excluded —
    those are display placements, not a comparable intent signal.
    """
    pcol    = _pcol(grain)
    results = []

    li_trunc  = _trunc("DATE(a.day)", grain)
    li_query  = f"""
    WITH latest_campaign AS (
      SELECT id, name
      FROM (
        SELECT id, name,
               ROW_NUMBER() OVER (PARTITION BY id ORDER BY last_modified_time DESC) AS rn
        FROM `bi-ntop.linkedin_ads.campaign_history`
      )
      WHERE rn = 1
    )
    SELECT
        {li_trunc} AS {pcol},
        CASE
          WHEN LOWER(COALESCE(c.name, '')) LIKE '%strategic%'
            OR LOWER(COALESCE(c.name, '')) LIKE '%stategic%'
            OR LOWER(COALESCE(c.name, '')) LIKE 'strat-%'           THEN 'Strategic'
          WHEN LOWER(COALESCE(c.name, '')) LIKE '%high-velo%'
            OR LOWER(COALESCE(c.name, '')) LIKE '%high velo%'
            OR LOWER(COALESCE(c.name, '')) LIKE '%-hv-%'
            OR LOWER(COALESCE(c.name, '')) LIKE 'hv-%'              THEN 'HV'
          ELSE 'Unmapped'
        END AS tier,
        'LinkedIn' AS channel,
        CAST(SUM(
          COALESCE(a.likes,      0) + COALESCE(a.comments,    0) +
          COALESCE(a.shares,     0) + COALESCE(a.follows,     0) +
          COALESCE(a.reactions,  0) + COALESCE(a.video_views, 0) +
          COALESCE(a.clicks,     0)
        ) AS INT64) AS engagement_events
    FROM `bi-ntop.linkedin_ads.ad_analytics_by_campaign` a
    LEFT JOIN latest_campaign c ON a.campaign_id = c.id
    WHERE DATE(a.day) BETWEEN @start_date AND @end_date
    GROUP BY {pcol}, tier, channel
    ORDER BY {pcol}
    """
    try:
        li = _to_date(_run(li_query, _date_params(start, end)), pcol)
        if not li.empty:
            results.append(li)
    except Exception:
        pass

    goog_trunc = _trunc("date_day", grain)
    goog_query = f"""
    SELECT
        {goog_trunc} AS {pcol},
        'All' AS tier,
        CASE WHEN LOWER(campaign_name) LIKE '%video%' THEN 'YouTube' ELSE 'Google' END AS channel,
        CAST(SUM(COALESCE(clicks, 0)) AS INT64) AS engagement_events
    FROM `bi-ntop.aero_prod_ad_reporting.ad_reporting__campaign_report`
    WHERE date_day BETWEEN @start_date AND @end_date
      AND platform = 'google_ads' AND spend > 0
      AND campaign_name NOT LIKE 'PMAX%'
    GROUP BY {pcol}, channel
    ORDER BY {pcol}
    """
    try:
        goog = _to_date(_run(goog_query, _date_params(start, end)), pcol)
        if not goog.empty:
            results.append(goog)
    except Exception:
        pass

    if results:
        return pd.concat(results, ignore_index=True)
    return pd.DataFrame(columns=[pcol, "tier", "channel", "engagement_events"])


# ── Layer 3: Demo Requests ─────────────────────────────────────────────────────
@st.cache_data(ttl=3600)
def load_demo_requests(start: date, end: date, grain: str) -> pd.DataFrame:
    """
    Timestamp column is 'timestamp' (not 'submitted_at').
    contact_form_submission has no _fivetran_deleted. Data starts Feb 2025.
    Mar 2025 spike (~306) is a Fivetran backfill artifact on HubSpot integration launch.
    Form join uses f.guid (not f.id). Forms matched via LOWER(name) LIKE '%demo%'.
    """
    pcol  = _pcol(grain)
    trunc = _trunc("DATE(cfs.timestamp)", grain)
    query = f"""
    SELECT
        {trunc} AS {pcol},
        CASE
          WHEN af.account_segment IN ('a. Strategic', 'b. Enterprise')     THEN 'Strategic'
          WHEN af.account_segment IN ('c. Emerging', 'd. Commercial',
                                      'e. Projects', 'f. Research',
                                      'g. Reseller/Partner')               THEN 'HV'
          ELSE 'Unmatched'
        END AS tier,
        COUNT(DISTINCT cfs.contact_id) AS demo_requests
    FROM `bi-ntop.hubspot.contact_form_submission` cfs
    JOIN `bi-ntop.hubspot.form` f
        ON cfs.form_id = f.guid
    JOIN `bi-ntop.hubspot.contact` hc
        ON cfs.contact_id = hc.id
       AND hc._fivetran_deleted = FALSE
    LEFT JOIN `bi-ntop.salesforce.account` sa
        ON hc.property_salesforceaccountid = sa.id
       AND sa._fivetran_deleted = FALSE
    LEFT JOIN `bi-ntop.google_sheets.account_fields` af
        ON af._18_digit_account_id = sa.id
    WHERE f._fivetran_deleted = FALSE
      AND LOWER(f.name) LIKE '%demo%'
      AND f.name NOT LIKE '%(FOR TESTING ONLY)%'
      AND DATE(cfs.timestamp) BETWEEN @start_date AND @end_date
    GROUP BY {pcol}, tier
    ORDER BY {pcol}, tier
    """
    try:
        return _to_date(_run(query, _date_params(start, end)), pcol)
    except Exception:
        return pd.DataFrame(columns=[pcol, "tier", "demo_requests"])


# ── Layer 4: Opportunities ─────────────────────────────────────────────────────
@st.cache_data(ttl=3600)
def load_opportunities(start: date, end: date, grain: str) -> pd.DataFrame:
    """
    tier_map uses _opp_tier_where (no is_closed/is_won gate) so that opps
    created in a period but since closed-lost still appear in 'generated' counts.
    A periods×tiers spine guarantees every (period, tier) has a row — NULLs
    COALESCE to 0 — so the chart line never breaks across quiet months.
    """
    pcol          = _pcol(grain)
    trunc_created = _trunc("DATE(o.created_date)", grain)
    tier_case     = _opp_tier_case()
    tier_where    = _opp_tier_where()
    if grain == "Monthly":
        gen_arr = ("GENERATE_DATE_ARRAY("
                   "DATE_TRUNC(@start_date, MONTH), "
                   "DATE_TRUNC(@end_date, MONTH), "
                   "INTERVAL 1 MONTH)")
    else:
        gen_arr = ("GENERATE_DATE_ARRAY("
                   "DATE_TRUNC(@start_date, WEEK(MONDAY)), "
                   "DATE_TRUNC(@end_date, WEEK(MONDAY)), "
                   "INTERVAL 1 WEEK)")

    query = f"""
    WITH tier_map AS (
      SELECT o.id AS opp_id,
             {tier_case} AS tier
      FROM `bi-ntop.salesforce.opportunity` o
      LEFT JOIN `bi-ntop.google_sheets.account_fields` af
        ON af._18_digit_account_id = o.account_id
      WHERE {tier_where}
    ),
    tiers AS (
      SELECT DISTINCT tier FROM tier_map WHERE tier IS NOT NULL
    ),
    periods AS (
      SELECT d AS {pcol} FROM UNNEST({gen_arr}) AS d
    ),
    spine AS (
      SELECT p.{pcol}, t.tier
      FROM periods p CROSS JOIN tiers t
    ),
    generated AS (
      SELECT {trunc_created} AS {pcol}, tm.tier, COUNT(*) AS opps_generated
      FROM `bi-ntop.salesforce.opportunity` o
      JOIN tier_map tm ON tm.opp_id = o.id
      WHERE DATE(o.created_date) BETWEEN @start_date AND @end_date
      GROUP BY {pcol}, tm.tier
    ),
    first_stage3 AS (
      SELECT oh.opportunity_id,
             {_trunc("MIN(DATE(oh.created_date))", grain)} AS stage3_at
      FROM `bi-ntop.salesforce.opportunity_history` oh
      WHERE oh._fivetran_deleted = FALSE
        AND oh.stage_name LIKE '3%'
      GROUP BY oh.opportunity_id
    ),
    qualified AS (
      SELECT fs.stage3_at AS {pcol}, tm.tier, COUNT(*) AS opps_qualified
      FROM first_stage3 fs
      JOIN tier_map tm ON tm.opp_id = fs.opportunity_id
      WHERE fs.stage3_at BETWEEN @start_date AND @end_date
      GROUP BY {pcol}, tm.tier
    )
    SELECT
      s.{pcol},
      s.tier,
      COALESCE(g.opps_generated, 0) AS opps_generated,
      COALESCE(q.opps_qualified, 0) AS opps_qualified
    FROM spine s
    LEFT JOIN generated g ON g.{pcol} = s.{pcol} AND g.tier = s.tier
    LEFT JOIN qualified q ON q.{pcol} = s.{pcol} AND q.tier = s.tier
    ORDER BY s.{pcol}, s.tier
    """
    try:
        return _to_date(_run(query, _date_params(start, end)), pcol)
    except Exception as e:
        st.warning(f"Layer 4 unavailable: {e}")
        return pd.DataFrame(columns=[pcol, "tier", "opps_generated", "opps_qualified"])


# ── Layer 5: Pipeline ARR ──────────────────────────────────────────────────────
@st.cache_data(ttl=3600)
def load_pipeline_value(start: date, end: date, grain: str) -> pd.DataFrame:
    """Uses _opp_base_where (with is_closed/is_won) — don't sum ARR from lost deals."""
    pcol       = _pcol(grain)
    trunc      = _trunc("DATE(o.created_date)", grain)
    tier_case  = _opp_tier_case()
    base_where = _opp_base_where()
    query = f"""
    SELECT
        {trunc} AS {pcol},
        {tier_case} AS tier,
        ROUND(SUM(COALESCE(opf.arr, 0)), 2) AS pipeline_arr,
        COUNTIF(opf.arr IS NOT NULL AND opf.arr > 0) AS opps_with_arr,
        COUNT(*) AS opp_count
    FROM `bi-ntop.salesforce.opportunity` o
    LEFT JOIN `bi-ntop.google_sheets.opportunity_fields` opf ON opf.opportunity_id = o.id
    LEFT JOIN `bi-ntop.google_sheets.account_fields` af      ON af._18_digit_account_id = o.account_id
    WHERE {base_where}
      AND DATE(o.created_date) BETWEEN @start_date AND @end_date
    GROUP BY {pcol}, tier
    ORDER BY {pcol}, tier
    """
    try:
        return _to_date(_run(query, _date_params(start, end)), pcol)
    except Exception:
        return pd.DataFrame(columns=[pcol, "tier", "pipeline_arr", "opps_with_arr", "opp_count"])


# ── Layer 6: Sales Velocity (monthly only) ────────────────────────────────────
@st.cache_data(ttl=3600)
def load_velocity_inputs(start: date, end: date) -> tuple[pd.DataFrame, pd.DataFrame]:
    tier_where = _opp_tier_where()
    m = start.month - 6
    y = start.year + (m - 1) // 12
    ext_start = date(y, ((m - 1) % 12) + 1, 1)
    params = [
        bigquery.ScalarQueryParameter("start_date", "DATE", ext_start),
        bigquery.ScalarQueryParameter("end_date",   "DATE", end),
    ]

    qual_q = f"""
    SELECT DATE_TRUNC(fs.stage3_at, MONTH) AS month_start, COUNT(*) AS num_qualified
    FROM (
      SELECT oh.opportunity_id, MIN(DATE(oh.created_date)) AS stage3_at
      FROM `bi-ntop.salesforce.opportunity_history` oh
      WHERE oh._fivetran_deleted = FALSE AND oh.stage_name LIKE '3%'
      GROUP BY 1
    ) fs
    JOIN `bi-ntop.salesforce.opportunity` o ON o.id = fs.opportunity_id
    LEFT JOIN `bi-ntop.google_sheets.account_fields` af ON af._18_digit_account_id = o.account_id
    WHERE {tier_where}
      AND fs.stage3_at BETWEEN @start_date AND @end_date
    GROUP BY 1 ORDER BY 1
    """

    trail_q = f"""
    SELECT
      DATE_TRUNC(DATE(o.close_date), MONTH) AS month_start,
      COUNTIF(o.is_won = TRUE)               AS won,
      COUNTIF(o.is_closed = TRUE)            AS closed,
      AVG(CASE WHEN o.is_won THEN opf.arr END)              AS avg_arr,
      AVG(CASE WHEN o.is_won THEN opf.qualify_to_close END) AS avg_cycle_days
    FROM `bi-ntop.salesforce.opportunity` o
    LEFT JOIN `bi-ntop.google_sheets.opportunity_fields` opf ON opf.opportunity_id = o.id
    LEFT JOIN `bi-ntop.google_sheets.account_fields` af      ON af._18_digit_account_id = o.account_id
    WHERE {tier_where}
      AND o.is_closed = TRUE
      AND DATE(o.close_date) BETWEEN @start_date AND @end_date
    GROUP BY 1 ORDER BY 1
    """
    try:
        qual_df = _to_date(_run(qual_q, params), "month_start")
    except Exception:
        qual_df = pd.DataFrame(columns=["month_start", "num_qualified"])
    try:
        trail_df = _to_date(_run(trail_q, params), "month_start")
    except Exception:
        trail_df = pd.DataFrame(columns=["month_start", "won", "closed", "avg_arr", "avg_cycle_days"])
    return qual_df, trail_df


def compute_velocity(qual_df: pd.DataFrame, trail_df: pd.DataFrame,
                     start: date, end: date) -> pd.DataFrame:
    if qual_df.empty or trail_df.empty:
        return pd.DataFrame(columns=["month_start", "sales_velocity"])
    anchor = min(trail_df["month_start"].min(), qual_df["month_start"].min())
    spine  = pd.DataFrame({"month_start": pd.date_range(
        pd.Timestamp(anchor), pd.Timestamp(end), freq="MS",
    ).date})
    t = spine.merge(trail_df, on="month_start", how="left").fillna(0)
    t["win_rate"]    = t["won"] / t["closed"].replace(0, float("nan"))
    t["win_rate_6m"] = t["win_rate"].rolling(6, min_periods=1).mean()
    t["arr_6m"]      = t["avg_arr"].rolling(6, min_periods=1).mean()
    t["cycle_6m"]    = t["avg_cycle_days"].rolling(6, min_periods=1).mean()
    out = spine.merge(qual_df, on="month_start", how="left").fillna(0)
    out = out.merge(t[["month_start", "win_rate_6m", "arr_6m", "cycle_6m"]], on="month_start", how="left")
    out["sales_velocity"] = (
        out["num_qualified"] * out["win_rate_6m"] * out["arr_6m"]
    ) / out["cycle_6m"].replace(0, float("nan"))
    return out[
        (out["month_start"] >= start) & (out["month_start"] <= end)
    ][["month_start", "sales_velocity"]].copy()


# ── Chart helpers ──────────────────────────────────────────────────────────────
def _add_event_lines(fig: go.Figure, events: list[dict]) -> None:
    for ev in events:
        d = str(ev["date"]) if not hasattr(ev["date"], "isoformat") else ev["date"].isoformat()
        fig.add_shape(
            type="line", xref="x", yref="paper",
            x0=d, x1=d, y0=0, y1=1,
            line=dict(color="rgba(80,80,80,0.28)", width=1.2, dash="dot"),
        )
        fig.add_trace(
            go.Scatter(
                x=[d, d], y=[0, 1], mode="lines",
                line=dict(color="rgba(0,0,0,0)", width=0),
                hovertext=f"<b>{ev['label']}</b><br>{ev['description']}",
                hoverinfo="text",
                showlegend=False,
            ),
            row=1, col=1,
        )


def _shade_regions(fig: go.Figure, today: date, grain: str) -> None:
    ip = _inprogress_cutoff(today, grain)
    mt = _maturing_cutoff(today, grain)
    for row in range(1, NROWS + 1):
        fig.add_vrect(
            x0=str(mt), x1=str(ip),
            fillcolor="rgba(200,200,200,0.10)", layer="below", line_width=0,
            row=row, col=1,
        )
        fig.add_vrect(
            x0=str(ip), x1=str(today + timedelta(days=35)),
            fillcolor="rgba(200,200,200,0.20)", layer="below", line_width=0,
            row=row, col=1,
        )


def _stub(fig: go.Figure, row: int, message: str) -> None:
    fig.add_trace(go.Scatter(x=[], y=[], showlegend=False), row=row, col=1)
    fig.add_annotation(
        text=f"⚠ {message}",
        xref="paper", yref="paper",
        x=0.5, y=_ROW_Y.get(row, 0.5),
        showarrow=False,
        font=dict(size=11, color="#999999"),
    )


def _inprogress_cutoff(today: date, grain: str) -> date:
    """Start of the current (incomplete) period."""
    if grain == "Monthly":
        return today.replace(day=1)
    return today - timedelta(days=today.weekday())


def _maturing_cutoff(today: date, grain: str) -> date:
    """3 complete periods before the in-progress cutoff — for background shading."""
    from dateutil.relativedelta import relativedelta
    ip = _inprogress_cutoff(today, grain)
    if grain == "Monthly":
        return ip - relativedelta(months=3)
    return ip - timedelta(weeks=12)


def _split2(df: pd.DataFrame, pcol: str, today: date, grain: str):
    """(solid, inprogress) — creation-stamped series: pipeline ARR bars."""
    ip = _inprogress_cutoff(today, grain)
    return df[df[pcol] < ip], df[df[pcol] >= ip]


# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Filters")
    today = date.today()

    start_date = st.date_input("From", value=date(2025, 1, 1), max_value=today)
    end_date   = st.date_input("To",   value=today,             max_value=today)
    grain      = st.radio("Time grain", ["Monthly", "Weekly"], horizontal=True)


pcol = _pcol(grain)


# ── KPI tiles ─────────────────────────────────────────────────────────────────
with st.spinner("Loading summary…"):
    kpi = load_kpi_summary(today)

tiles_html = "".join([
    _kpi_tile_html("Paid Spend",        _fmt_currency(kpi["spend"][0]),        _delta_html(*kpi["spend"])),
    _kpi_tile_html("Engagement Events", _fmt_count(kpi["engagement"][0]),      _delta_html(*kpi["engagement"])),
    _kpi_tile_html("Demo Requests",     _fmt_count(kpi["demos"][0]),           _delta_html(*kpi["demos"])),
    _kpi_tile_html("Pipeline ARR",      _fmt_currency(kpi["pipeline_arr"][0]), _delta_html(*kpi["pipeline_arr"])),
])
st.markdown(f'<div class="kpi-band">{tiles_html}</div>', unsafe_allow_html=True)

st.markdown(
    '<div class="kpi-note">Trailing 30 days &middot; vs prior 30 days'
    ' &middot; independent of date range filter</div>',
    unsafe_allow_html=True,
)


# ── In-progress callout ────────────────────────────────────────────────────────
_ip = _inprogress_cutoff(today, grain)
if grain == "Monthly":
    _period_label = _ip.strftime("%B %Y")
else:
    _period_label = f"Week of {_ip.strftime('%b %-d, %Y')}"

st.info(
    f"**{_period_label} is in progress** — darker shaded band on right. "
    "The lighter band to its left covers the last 3 complete periods — "
    "qualified opps and velocity there haven't had time to fully mature yet. "
    "Qualified (Stage 3+) lags generated by ~6 months; the Oct 2025 cohort "
    "is expected to appear as qualified in Q1–Q2 2026.",
    icon="ℹ️",
)


# ── Load data ──────────────────────────────────────────────────────────────────
with st.spinner("Loading…"):
    events   = load_events()
    spend_df = load_spend(start_date, end_date, grain)
    eng_df   = load_engagement(start_date, end_date, grain)
    demo_df  = load_demo_requests(start_date, end_date, grain)
    opp_df   = load_opportunities(start_date, end_date, grain)
    pip_df   = load_pipeline_value(start_date, end_date, grain)
    if grain == "Monthly":
        qual_df, trail_df = load_velocity_inputs(start_date, end_date)
        vel_df = compute_velocity(qual_df, trail_df, start_date, end_date)
    else:
        vel_df = pd.DataFrame(columns=["month_start", "sales_velocity"])


# ── Filter (pass-through — tier and channel filters removed) ──────────────────
spend_f = spend_df
eng_f   = eng_df
demo_f  = demo_df
opp_f   = opp_df
pip_f   = pip_df


# ── Build figure ───────────────────────────────────────────────────────────────
fig = make_subplots(
    rows=NROWS, cols=1,
    shared_xaxes=True,
    vertical_spacing=V_SPACING,
    row_heights=ROW_HEIGHTS,
    specs=[
        [{}],                     # row 1 — spend
        [{}],                     # row 2 — engagement
        [{"secondary_y": True}],  # row 3 — demo requests (Unmatched left, Strat/HV right)
        [{"secondary_y": True}],  # row 4 — opps (generated left, qualified right)
        [{}],                     # row 5 — pipeline ARR
        [{}],                     # row 6 — sales velocity
    ],
    subplot_titles=[
        "1 · Paid Spend",
        "2 · Engagement Events",
        "3 · Inbound Demo Requests",
        "4 · Opportunities",
        "5a · Pipeline ARR Created",
        "5b · Sales Velocity ($/day)",
    ],
)


# ── Row 1: Paid Spend ──────────────────────────────────────────────────────────
LG1 = "Paid Spend"
if spend_f.empty:
    _stub(fig, 1, "No data — spend source unavailable")
else:
    tot = spend_f.groupby(pcol, as_index=False)["spend"].sum()
    fig.add_trace(go.Scatter(
        x=tot[pcol], y=tot["spend"], name="Total",
        mode="lines+markers", line=dict(color=COLORS["Total"], width=2),
        legendgroup=LG1, legendgrouptitle_text=LG1,
        hovertemplate="%{x}: $%{y:,.0f}<extra>Total Spend</extra>",
    ), row=1, col=1)
    for ch in spend_f["channel"].unique():
        ch_df = spend_f[spend_f["channel"] == ch]
        fig.add_trace(go.Bar(
            x=ch_df[pcol], y=ch_df["spend"], name=ch,
            marker_color=COLORS.get(ch, "#aaaaaa"),
            visible="legendonly",
            legendgroup=LG1,
            hovertemplate="%{x}: $%{y:,.0f}<extra>" + ch + "</extra>",
        ), row=1, col=1)
fig.update_yaxes(title_text="Spend ($)", tickprefix="$", tickformat=",.0f", row=1, col=1)


# ── Row 2: Engagement ──────────────────────────────────────────────────────────
LG2 = "Engagement"
if eng_f.empty:
    _stub(fig, 2, "No data — engagement source unavailable")
else:
    tot2 = eng_f.groupby(pcol, as_index=False)["engagement_events"].sum()
    fig.add_trace(go.Scatter(
        x=tot2[pcol], y=tot2["engagement_events"], name="Total",
        mode="lines+markers", line=dict(color=COLORS["Total"], width=2),
        legendgroup=LG2, legendgrouptitle_text=LG2,
        hovertemplate="%{x}: %{y:,.0f}<extra>Total Engagement</extra>",
    ), row=2, col=1)
    for ch in eng_f["channel"].unique():
        ch_sub = eng_f[eng_f["channel"] == ch].groupby(pcol, as_index=False)["engagement_events"].sum()
        fig.add_trace(go.Bar(
            x=ch_sub[pcol], y=ch_sub["engagement_events"], name=ch,
            marker_color=COLORS.get(ch, "#aaaaaa"),
            visible="legendonly",
            legendgroup=LG2,
            hovertemplate="%{x}: %{y:,.0f}<extra>" + ch + "</extra>",
        ), row=2, col=1)
fig.update_yaxes(title_text="Events", tickformat=",.0f", row=2, col=1)


# ── Row 3: Demo Requests — dual y-axis ────────────────────────────────────────
LG3     = "Demo Requests"
TIER_CL = {"Strategic": COLORS["Strategic"], "HV": COLORS["HV"], "Unmatched": COLORS["Unmatched"]}

if demo_f.empty:
    _stub(fig, 3, "No data — HubSpot demo forms unavailable (data starts Feb 2025)")
else:
    tiers_present = [t for t in ["Strategic", "HV", "Unmatched"] if t in demo_f["tier"].values]
    for i, tier in enumerate(tiers_present):
        t_df = demo_f[demo_f["tier"] == tier].groupby(pcol, as_index=False)["demo_requests"].sum()
        on_right = tier in ("Strategic", "HV")
        fig.add_trace(go.Scatter(
            x=t_df[pcol], y=t_df["demo_requests"], name=tier,
            mode="lines+markers",
            line=dict(color=TIER_CL.get(tier, "#aaaaaa"), width=2),
            legendgroup=LG3,
            legendgrouptitle_text=LG3 if i == 0 else None,
            hovertemplate="%{x}: %{y:,d}<extra>" + tier + " Demos</extra>",
        ), row=3, col=1, secondary_y=on_right)
    tot3 = demo_f.groupby(pcol, as_index=False)["demo_requests"].sum()
    fig.add_trace(go.Scatter(
        x=tot3[pcol], y=tot3["demo_requests"], name="Total",
        mode="lines", line=dict(color=COLORS["Total"], width=1.5, dash="dot"),
        visible="legendonly",
        legendgroup=LG3,
        hovertemplate="%{x}: %{y:,d}<extra>Total Demos</extra>",
    ), row=3, col=1, secondary_y=False)

fig.update_yaxes(title_text="Unmatched", tickformat=",d", row=3, col=1, secondary_y=False)
fig.update_yaxes(title_text="Strat / HV", tickformat=",d", row=3, col=1, secondary_y=True,
                 showgrid=False, range=[0, 40])


# ── Row 4: Opportunities — dual y-axis ────────────────────────────────────────
LG4 = "Opportunities"
if opp_f.empty:
    _stub(fig, 4, "No data — Salesforce opportunity source unavailable")
else:
    gen_tot  = opp_f.groupby(pcol, as_index=False)["opps_generated"].sum()
    qual_tot = opp_f.groupby(pcol, as_index=False)["opps_qualified"].sum()

    if not gen_tot.empty:
        fig.add_trace(go.Scatter(
            x=gen_tot[pcol], y=gen_tot["opps_generated"],
            name="Generated", mode="lines+markers",
            line=dict(color=COLORS["Generated"], width=2.5),
            legendgroup=LG4, legendgrouptitle_text=LG4,
            hovertemplate="%{x}: %{y:,d}<extra>Opps Generated</extra>",
        ), row=4, col=1, secondary_y=False)

    if not qual_tot.empty:
        fig.add_trace(go.Scatter(
            x=qual_tot[pcol], y=qual_tot["opps_qualified"],
            name="Qualified (Stage 3+)", mode="lines+markers",
            line=dict(color=COLORS["Qualified"], width=2.5),
            legendgroup=LG4,
            hovertemplate="%{x}: %{y:,d}<extra>Opps Qualified</extra>",
        ), row=4, col=1, secondary_y=True)

    for tier in [t for t in ["Strategic", "HV"] if t in opp_f["tier"].values]:
        for metric, sec_y, sym in [("opps_generated", False, "circle"),
                                    ("opps_qualified", True,  "diamond")]:
            t_df  = opp_f[opp_f["tier"] == tier].groupby(pcol, as_index=False)[metric].sum()
            label = f"{tier} Gen" if metric == "opps_generated" else f"{tier} Qual"
            fig.add_trace(go.Scatter(
                x=t_df[pcol], y=t_df[metric], name=label, mode="markers",
                marker=dict(color=COLORS[tier], symbol=sym, size=7),
                visible="legendonly",
                legendgroup=LG4,
                hovertemplate="%{x}: %{y:,d}<extra>" + label + "</extra>",
            ), row=4, col=1, secondary_y=sec_y)

fig.update_yaxes(title_text="Generated", tickformat=",d", row=4, col=1, secondary_y=False)
fig.update_yaxes(title_text="Qualified", tickformat=",d", row=4, col=1, secondary_y=True,
                 showgrid=False, range=[0, 30])


# ── Row 5: Pipeline ARR ────────────────────────────────────────────────────────
LG5 = "Pipeline ARR"
if pip_f.empty:
    _stub(fig, 5, "No data — pipeline source unavailable")
else:
    pip_total = pip_f.groupby(pcol, as_index=False)["pipeline_arr"].sum()
    pip_solid, pip_ip = _split2(pip_total, pcol, today, grain)

    for df_part, opacity, show in [(pip_solid, 1.0, True), (pip_ip, 0.4, False)]:
        if not df_part.empty:
            fig.add_trace(go.Bar(
                x=df_part[pcol], y=df_part["pipeline_arr"], name="Pipeline ARR",
                marker_color=COLORS["Pipeline"], opacity=opacity,
                legendgroup=LG5, legendgrouptitle_text=LG5,
                showlegend=show,
                hovertemplate="%{x}: $%{y:,.0f}<extra>Pipeline ARR</extra>",
            ), row=5, col=1)

    for tier in [t for t in ["Strategic", "HV"] if t in pip_f["tier"].values]:
        t_pip = pip_f[pip_f["tier"] == tier].groupby(pcol, as_index=False)["pipeline_arr"].sum()
        fig.add_trace(go.Bar(
            x=t_pip[pcol], y=t_pip["pipeline_arr"], name=tier,
            marker_color=COLORS[tier],
            visible="legendonly",
            legendgroup=LG5,
            hovertemplate="%{x}: $%{y:,.0f}<extra>" + tier + " ARR</extra>",
        ), row=5, col=1)
fig.update_yaxes(title_text="ARR ($)", tickprefix="$", tickformat=",.0f", row=5, col=1)


# ── Row 6: Sales Velocity ──────────────────────────────────────────────────────
LG6 = "Sales Velocity"
if grain != "Monthly":
    _stub(fig, 6, "Sales Velocity shown at Monthly grain only")
elif vel_df.empty or vel_df["sales_velocity"].isna().all():
    _stub(fig, 6, "No data — requires closed-won opps with ARR + cycle data")
else:
    vel_clean = vel_df.dropna(subset=["sales_velocity"])
    fig.add_trace(go.Scatter(
        x=vel_clean["month_start"], y=vel_clean["sales_velocity"],
        name="$/day", mode="lines+markers",
        line=dict(color=COLORS["Velocity"], width=2),
        legendgroup=LG6, legendgrouptitle_text=LG6,
        hovertemplate="%{x}: $%{y:,.0f}/day<extra>Sales Velocity</extra>",
    ), row=6, col=1)
fig.update_yaxes(title_text="$/day", tickprefix="$", tickformat=",.0f", row=6, col=1)


# ── Annotations & shading ──────────────────────────────────────────────────────
_add_event_lines(fig, events)
_shade_regions(fig, today, grain)
fig.update_traces(connectgaps=True, selector=dict(type="scatter"))


# ── Global layout ──────────────────────────────────────────────────────────────
tick_fmt = "%b %Y" if grain == "Monthly" else "%b %d"

fig.update_layout(
    height=2400,
    margin=dict(l=70, r=200, t=60, b=20),
    hovermode="x unified",
    barmode="group",
    plot_bgcolor="white",
    paper_bgcolor="white",
    legend=dict(
        orientation="v",
        x=1.02, y=1.0,
        xanchor="left", yanchor="top",
        tracegroupgap=18,
        font=dict(size=11, family="IBM Plex Sans"),
        bgcolor="rgba(255,255,255,0.9)",
        bordercolor=_C["gray_light"],
        borderwidth=1,
        itemclick="toggle",
        itemdoubleclick="toggleothers",
        groupclick="toggleitem",
    ),
)
fig.update_xaxes(
    showticklabels=True,
    tickformat=tick_fmt,
    showgrid=True, gridcolor="#F0F0F0",
    tickangle=-30,
)
fig.update_yaxes(showgrid=True, gridcolor="#F0F0F0")


# ── Render ─────────────────────────────────────────────────────────────────────
st.plotly_chart(fig, use_container_width=True)


# ── Footer notes ───────────────────────────────────────────────────────────────
st.caption("Engagement (row 2): LinkedIn shows Strategic / HV / Unmapped tier split. Google and YouTube are aggregate totals.")
if grain != "Monthly":
    st.caption("Sales Velocity (row 6) is only computed at Monthly grain.")
if not demo_df.empty and demo_df["demo_requests"].sum() == 0:
    st.caption("Demo Requests: HubSpot form data starts Feb 2025 — no submissions exist before that.")

with st.expander("Methodology"):
    st.markdown("""
**Occurrence-count funnel** — each layer counts events in their own time period. No cohort linking between layers.

This is pattern-matching, not attribution. Spend in a given month bought the engagement that happened that month; the pipeline that resulted will appear 3–9 months later.

| Layer | What it counts |
|-------|----------------|
| 1 · Paid Spend | Total media cost by channel |
| 2 · Engagement | Clicks + social actions (LinkedIn); paid clicks (Google/YouTube). PMax excluded. |
| 3 · Demo Requests | HubSpot demo form submissions, distinct contacts. Data starts Feb 2025. |
| 4 · Opportunities | Generated = Salesforce opp created date. Qualified = first date opp reached Stage 3+. |
| 5a · Pipeline ARR | ARR on open/won opps at creation date. Excludes closed-lost. |
| 5b · Sales Velocity | (Qualified × Win Rate × Avg ARR) ÷ Avg Cycle Days, using 6-month trailing inputs. Monthly only. |

Shading: lighter band = last 3 complete periods (qualified opps and velocity still accruing). Darker band = current in-progress period.
""")
