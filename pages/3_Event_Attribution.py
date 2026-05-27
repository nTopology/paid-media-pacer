"""
Event & Webinar Attribution — v1.0
Net-new contacts attributed to HubSpot campaigns prefixed EV- (events) or WN- (webinars).
Source buckets show where contacts came from before registering.
"""

from __future__ import annotations

import base64
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

try:
    from google.cloud import bigquery as _bq
    from google.oauth2 import service_account as _gcp_sa
    _HAS_BQ = True
except ImportError:
    _HAS_BQ = False


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
.kpi-band {{
    display: flex;
    gap: 16px;
    margin: 0 0 24px 0;
}}
.kpi-tile {{
    flex: 1;
    background: #f5f5f4;
    border-radius: 8px;
    padding: 1rem 1.25rem;
    min-width: 0;
}}
.kpi-label {{
    font-family: 'IBM Plex Mono', monospace;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.10em;
    color: {_C["gray_mid"]};
    margin-bottom: 6px;
}}
.kpi-value {{
    font-family: 'Oswald', sans-serif;
    font-weight: 700;
    font-size: 28px;
    color: {_C["black"]};
    line-height: 1.1;
}}
</style>
"""


# ── Constants ──────────────────────────────────────────────────────────────────
HS_BASE       = "https://api.hubapi.com"
METRICS_START = "2024-06-01"

# Channel color palette (spec-defined)
CHANNEL_COLORS: dict[str, str] = {
    "Email marketing":   "#1D9E75",
    "Organic search":    "#378ADD",
    "Social media":      "#D4537E",
    "Other campaigns":   "#7F77DD",
    "Direct":            "#D85A30",
    "Referrals / other": "#888780",
}
CHANNEL_ORDER = list(CHANNEL_COLORS.keys())


# ── HubSpot API helpers ────────────────────────────────────────────────────────

def _hs_token() -> str:
    if "hubspot_api_token" in st.secrets:
        return str(st.secrets["hubspot_api_token"])
    import os
    return os.environ.get("HUBSPOT_API_TOKEN", "")


def _hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {_hs_token()}",
        "Content-Type":  "application/json",
    }


def _hs_get(path: str, params: dict | None = None) -> dict:
    resp = requests.get(
        f"{HS_BASE}{path}",
        headers=_hs_headers(),
        params=params or {},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _hs_post(path: str, payload: dict) -> dict:
    resp = requests.post(
        f"{HS_BASE}{path}",
        headers=_hs_headers(),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _bucket_source(src: str) -> str:
    s = (src or "").upper().strip()
    if s == "EMAIL_MARKETING":
        return "Email marketing"
    if s == "ORGANIC_SEARCH":
        return "Organic search"
    if s in ("SOCIAL_MEDIA", "PAID_SOCIAL"):
        return "Social media"
    if s in ("OTHER_CAMPAIGNS", "PAID_SEARCH"):
        return "Other campaigns"
    if s == "DIRECT_TRAFFIC":
        return "Direct"
    return "Referrals / other"


# ── Data fetchers ──────────────────────────────────────────────────────────────

@st.cache_data(ttl=3600)
def fetch_ev_wn_campaigns() -> list[dict]:
    """
    Paginate /marketing/v3/campaigns with properties=hs_name so names are populated.
    The `id` field on each result is the campaign GUID used in all subsequent calls.
    Returns list of {"id": guid, "name": str} filtered to EV- and WN- prefixes.
    """
    campaigns: list[dict] = []
    after: str | None = None
    while True:
        params: dict = {"limit": 100, "properties": "hs_name"}
        if after:
            params["after"] = after
        data = _hs_get("/marketing/v3/campaigns", params)
        for c in data.get("results", []):
            name = (c.get("properties") or {}).get("hs_name", "")
            if name.startswith("EV-") or name.startswith("WN-"):
                campaigns.append({"id": c["id"], "name": name})
        after = ((data.get("paging") or {}).get("next") or {}).get("after")
        if not after:
            break
    return campaigns


@st.cache_data(ttl=3600)
def fetch_campaign_metrics(campaign_guid: str, start_date: str, end_date: str) -> dict:
    """
    GET /marketing/v3/campaigns/{campaignGuid}/reports/metrics
    Uses GUID (the `id` from the list endpoint), not the numeric CRM object ID.
    start_date/end_date are cache-key args to avoid replaying stale zeros.
    """
    return _hs_get(
        f"/marketing/v3/campaigns/{campaign_guid}/reports/metrics",
        {"startDate": start_date, "endDate": end_date},
    )


@st.cache_data(ttl=3600)
def fetch_contact_ids(campaign_guid: str, attr_type: str) -> list[str]:
    """
    GET /marketing/v3/campaigns/{guid}/reports/contacts/{attr_type}
    attr_type must be contactFirstTouch or contactLastTouch.
    Returns list of contact ID strings. Cached 1 h.
    """
    today_str = date.today().isoformat()
    ids: list[str] = []
    after: str | None = None
    while True:
        params: dict = {
            "startDate": METRICS_START,
            "endDate":   today_str,
            "limit":     100,
        }
        if after:
            params["after"] = after
        # Let HTTP errors propagate — caller catches and records them.
        data = _hs_get(
            f"/marketing/v3/campaigns/{campaign_guid}/reports/contacts/{attr_type}",
            params,
        )
        results = data.get("results", [])
        for item in results:
            cid = item.get("id") or item.get("contactId")
            if cid:
                ids.append(str(cid))
        after = ((data.get("paging") or {}).get("next") or {}).get("after")
        if not after or not results:
            break
    return ids


@st.cache_data(ttl=86400)
def fetch_contact_sources_bulk(contact_ids: tuple[str, ...]) -> dict[str, str]:
    """
    POST /crm/v3/objects/contacts/batch/read for a deduplicated set of IDs.
    Returns {contact_id: channel_bucket}. Accepts tuple so it's hashable. Cached 24 h.
    """
    if not contact_ids:
        return {}
    result: dict[str, str] = {}
    ids = list(contact_ids)
    for i in range(0, len(ids), 100):
        chunk = ids[i : i + 100]
        # Let HTTP errors propagate — caller catches and surfaces them.
        resp = _hs_post(
            "/crm/v3/objects/contacts/batch/read",
            {
                "inputs":     [{"id": cid} for cid in chunk],
                "properties": ["hs_analytics_source", "hs_latest_source"],
            },
        )
        for contact in resp.get("results", []):
            props = contact.get("properties") or {}
            src   = props.get("hs_analytics_source") or ""
            result[str(contact["id"])] = _bucket_source(src)
    return result


# ── BigQuery client (reuses credentials pattern from page 2) ─────────────────
_GCP_PROJECT = "bi-ntop"


@st.cache_resource
def _get_bq_client():
    if not _HAS_BQ:
        return None
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    if "gcp_service_account" in st.secrets:
        creds = _gcp_sa.Credentials.from_service_account_info(
            st.secrets["gcp_service_account"], scopes=scopes,
        )
    else:
        creds = _gcp_sa.Credentials.from_service_account_file(
            "service-account.json", scopes=scopes,
        )
    return _bq.Client(credentials=creds, project=_GCP_PROJECT)


# ── Target Account helpers ────────────────────────────────────────────────────

def _clean_domain(raw: str) -> str:
    d = raw.strip().lower()
    for prefix in ("https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    if d.startswith("www."):
        d = d[4:]
    return d.split("/")[0].rstrip(".")


@st.cache_data(ttl=86400)
def fetch_target_domains() -> dict:
    """
    Fetch target-account domains from HubSpot companies (primary) and
    Salesforce via BigQuery (secondary). Cached 24 h.
    """
    aero: set[str] = set()
    turbo: set[str] = set()

    for prop, bucket in [
        ("aircraft_concept_target_type", aero),
        ("turbomachinery_target_type", turbo),
    ]:
        after: int = 0
        while True:
            payload: dict = {
                "filterGroups": [{"filters": [
                    {"propertyName": prop, "operator": "HAS_PROPERTY"},
                ]}],
                "properties": ["domain"],
                "limit": 100,
            }
            if after:
                payload["after"] = after
            data = _hs_post("/crm/v3/objects/companies/search", payload)
            for co in data.get("results", []):
                d = (co.get("properties") or {}).get("domain", "")
                if d:
                    bucket.add(_clean_domain(d))
            paging = (data.get("paging") or {}).get("next", {}).get("after")
            if not paging:
                break
            after = int(paging)

    hs_all = aero | turbo

    # Salesforce domains from BigQuery (secondary — for sync-diff logging)
    sf_all: set[str] = set()
    sf_error: str | None = None
    client = _get_bq_client() if _HAS_BQ else None
    if client is not None:
        try:
            col_df = client.query(
                "SELECT column_name "
                "FROM `bi-ntop.salesforce.INFORMATION_SCHEMA.COLUMNS` "
                "WHERE table_name = 'account' AND ("
                "LOWER(column_name) LIKE '%target_type%' "
                "OR LOWER(column_name) LIKE '%aircraft%target%' "
                "OR LOWER(column_name) LIKE '%turbomachinery%target%')"
            ).to_dataframe(create_bqstorage_client=False)
            target_cols = col_df["column_name"].tolist()
            if target_cols:
                where = " OR ".join(f"`{c}` IS NOT NULL" for c in target_cols)
                df = client.query(
                    f"SELECT DISTINCT website "
                    f"FROM `bi-ntop.salesforce.account` "
                    f"WHERE ({where}) AND website IS NOT NULL "
                    f"AND _fivetran_deleted = FALSE"
                ).to_dataframe(create_bqstorage_client=False)
                sf_all = {_clean_domain(str(w)) for w in df["website"] if w}
            else:
                sf_error = "No target-type columns found on Salesforce account table"
        except Exception as exc:
            sf_error = str(exc)
    else:
        sf_error = "BigQuery not available"

    combined = hs_all | sf_all
    return {
        "aero": sorted(aero), "turbo": sorted(turbo), "all": sorted(combined),
        "hs_count": len(hs_all), "sf_count": len(sf_all),
        "hs_only": sorted(hs_all - sf_all) if sf_all else [],
        "sf_only": sorted(sf_all - hs_all) if sf_all else [],
        "sf_error": sf_error,
    }


def _count_contacts_for_domains(
    domains: tuple[str, ...],
    start_ms: str | None = None,
    end_ms: str | None = None,
) -> int:
    """
    Count marketing contacts whose hs_email_domain is in the domain set.
    Chunks domains ≤100 per IN filter; batches ≤5 filter groups per API call.
    """
    if not domains:
        return 0
    domain_list = list(domains)
    chunks = [domain_list[i : i + 100] for i in range(0, len(domain_list), 100)]

    total = 0
    for batch_start in range(0, len(chunks), 5):
        batch = chunks[batch_start : batch_start + 5]
        filter_groups: list[dict] = []
        for chunk in batch:
            filters: list[dict] = [
                {"propertyName": "hs_marketable_status", "operator": "EQ", "value": "true"},
                {"propertyName": "hs_email_domain", "operator": "IN", "values": chunk},
            ]
            if start_ms is not None:
                filters.append({"propertyName": "createdate", "operator": "GTE", "value": start_ms})
            if end_ms is not None:
                filters.append({"propertyName": "createdate", "operator": "LT", "value": end_ms})
            filter_groups.append({"filters": filters})

        data = _hs_post("/crm/v3/objects/contacts/search", {
            "filterGroups": filter_groups,
            "limit": 1,
        })
        total += data.get("total", 0)
    return total


@st.cache_data(ttl=3600)
def _count_month(domains: tuple[str, ...], year: int, month: int) -> int:
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = datetime(year + (1 if month == 12 else 0), (month % 12) + 1, 1, tzinfo=timezone.utc)
    return _count_contacts_for_domains(
        domains,
        str(int(start.timestamp() * 1000)),
        str(int(end.timestamp() * 1000)),
    )


@st.cache_data(ttl=3600)
def fetch_total_target_contacts(domains: tuple[str, ...]) -> int:
    return _count_contacts_for_domains(domains, None, None)


def _add_target_annotations(fig: go.Figure) -> None:
    for ann_date, color, text in [
        (date(2025, 2, 1), "#888780",
         "Feb 2025: bulk list import<br>(~2,300 contacts, under Legal review)"),
        (date(2026, 5, 1), "#D85A30",
         "May 2026: explicit opt-in<br>checkboxes added (per Legal)"),
    ]:
        fig.add_vline(x=ann_date, line_dash="dash", line_color=color, line_width=1)
        fig.add_annotation(
            x=ann_date, y=1.05, yref="paper", text=text, showarrow=False,
            font=dict(size=10, color=color, family="IBM Plex Sans"), xanchor="left",
        )


# ── Page setup ─────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Event & Webinar Attribution", layout="wide")
st.markdown(_BRAND_CSS, unsafe_allow_html=True)

if _PNG_FILE.exists():
    st.logo(str(_PNG_FILE))

st.markdown(
    f'<div class="ntop-header">'
    f'{_LOGO_IMG_HTML}'
    f'<div class="ntop-header-text">'
    f'<div class="ntop-page-title">Event &amp; Webinar Attribution</div>'
    f'<div class="ntop-page-subtitle">'
    f'Net-new contacts attributed to EV- and WN- HubSpot campaigns. '
    f'Source channels from <code>hs_analytics_source</code>. '
    f'Date range: 2024-06-01 to today.'
    f'</div></div></div>',
    unsafe_allow_html=True,
)


# ── Controls ───────────────────────────────────────────────────────────────────
ctrl1, ctrl2 = st.columns(2)
with ctrl1:
    campaign_type = st.radio(
        "Campaign type",
        ["All", "Events", "Webinars"],
        horizontal=True,
        index=0,
    )
with ctrl2:
    attr_model = st.radio(
        "Attribution model",
        ["First touch", "Last touch"],
        horizontal=True,
        index=0,
    )

metric_key  = "newContactsFirstTouch" if attr_model == "First touch" else "newContactsLastTouch"


# ── Load campaigns ─────────────────────────────────────────────────────────────
if not _hs_token():
    st.error(
        "HubSpot API token is not set. "
        "Go to Streamlit Cloud → your app → ⋮ → Settings → Secrets and add:\n\n"
        "```\nhubspot_api_token = \"pat-na1-...\"\n```"
    )
    st.stop()

with st.spinner("Fetching campaign list from HubSpot…"):
    try:
        all_campaigns = fetch_ev_wn_campaigns()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        body   = exc.response.text[:400] if exc.response is not None else ""
        st.error(
            f"HubSpot API returned {status}.\n\n"
            f"**Details:** {body}\n\n"
            "If this says 'MISSING_SCOPES', the Service Key is missing `marketing.campaigns.read`. "
            "If it says 'INVALID_AUTHENTICATION', the token in Streamlit secrets is wrong."
        )
        st.stop()
    except Exception as exc:
        st.error(f"Could not fetch campaigns: {exc}")
        st.stop()

if campaign_type == "Events":
    campaigns = [c for c in all_campaigns if c.get("name", "").startswith("EV-")]
elif campaign_type == "Webinars":
    campaigns = [c for c in all_campaigns if c.get("name", "").startswith("WN-")]
else:
    campaigns = list(all_campaigns)

if not campaigns:
    st.info("No campaigns match the current filter.")
    st.stop()


# ── Fetch campaign metrics (fast, 1 h cache) ───────────────────────────────────
today_str = date.today().isoformat()
campaign_rows: list[dict] = []
metrics_bar = st.progress(0, text="Loading campaign metrics…")

for i, c in enumerate(campaigns):
    metrics_bar.progress(
        (i + 1) / len(campaigns),
        text=f"Metrics {i + 1}/{len(campaigns)}: {c.get('name', c['id'])}",
    )
    try:
        raw = fetch_campaign_metrics(c["id"], METRICS_START, today_str)
        # Response is either {"metrics": {...}} (nested) or the flat dict itself
        m = raw.get("metrics") if isinstance(raw.get("metrics"), dict) else raw
        if isinstance(m, list):
            m = {item.get("metric") or item.get("name"): item.get("value", 0) for item in m}
        ft = int(m.get("newContactsFirstTouch", 0) or 0)
        lt = int(m.get("newContactsLastTouch",  0) or 0)
        metrics_error = None
    except Exception as _exc:
        ft, lt, raw = 0, 0, {}
        metrics_error = str(_exc)

    name      = c.get("name", "")
    try:
        # Parse YYYY-MM-DD from name (e.g. EV-2025-06-24-...)
        date_part = "-".join(name.split("-")[1:4])
        sort_date = date.fromisoformat(date_part)
    except Exception:
        sort_date = date.min

    campaign_rows.append({
        "id":         c["id"],
        "name":       name,
        "type":       "Event" if name.startswith("EV-") else "Webinar",
        "sort_date":  sort_date,
        "ft_count":   ft,
        "lt_count":   lt,
        "_raw":       raw,
        "_error":     metrics_error,
    })

metrics_bar.empty()

# Apply current attribution model and filter out zero-registration campaigns
for row in campaign_rows:
    row["count"] = row["ft_count"] if attr_model == "First touch" else row["lt_count"]

campaign_rows_all = campaign_rows  # keep unfiltered copy for diagnostics
campaign_rows = [r for r in campaign_rows if r["count"] > 0]

if not campaign_rows:
    first = campaign_rows_all[0] if campaign_rows_all else {}
    if first.get("_error"):
        st.error(f"Metrics API error: {first['_error']}")
    else:
        st.warning("All campaigns returned 0 registrations. Raw API response for first campaign:")
        st.json(first.get("_raw", {}))
    st.stop()

# Sort descending by date for the table (most recent first)
campaign_rows.sort(key=lambda r: r["sort_date"], reverse=True)


# ── Fetch contact IDs for every campaign (both models, 1 h cache) ──────────────
# Fetch FT + LT for all campaigns upfront so switching the model toggle is instant.
ft_ids:     dict[str, list[str]] = {}
lt_ids:     dict[str, list[str]] = {}
first_error: str | None          = None

id_bar = st.progress(0, text="Fetching contact IDs…")
for i, row in enumerate(campaign_rows):
    id_bar.progress(
        (i + 1) / len(campaign_rows),
        text=f"Contact IDs {i + 1}/{len(campaign_rows)}: {row['name']}",
    )
    try:
        ft_ids[row["id"]] = fetch_contact_ids(row["id"], "contactFirstTouch")
    except Exception as exc:
        ft_ids[row["id"]] = []
        if first_error is None:
            first_error = str(exc)
    try:
        lt_ids[row["id"]] = fetch_contact_ids(row["id"], "contactLastTouch")
    except Exception as exc:
        lt_ids[row["id"]] = []
        if first_error is None:
            first_error = str(exc)
id_bar.empty()

# Deduplicate across both models and bulk-read sources in one shot (24 h cache).
all_ids: tuple[str, ...] = tuple(sorted({
    cid
    for mapping in (ft_ids, lt_ids)
    for ids in mapping.values()
    for cid in ids
}))
sources_lookup: dict[str, str] = {}
bulk_error: str | None = None
with st.spinner(f"Loading sources for {len(all_ids):,} unique contacts (cached 24 h)…"):
    try:
        sources_lookup = fetch_contact_sources_bulk(all_ids)
    except Exception as exc:
        bulk_error = str(exc)

# Aggregate per campaign using whichever ID list the selected model needs.
for row in campaign_rows:
    id_list = ft_ids.get(row["id"], []) if attr_model == "First touch" else lt_ids.get(row["id"], [])
    src: dict[str, int] = {}
    for cid in id_list:
        bucket = sources_lookup.get(cid, "Referrals / other")
        src[bucket] = src.get(bucket, 0) + 1
    row["sources"]    = src
    row["n_contacts"] = len(id_list)

# Determine whether source data actually loaded before rendering cards or chart.
sources_available = any(sum(r["sources"].values()) > 0 for r in campaign_rows)


# ── Metric cards ───────────────────────────────────────────────────────────────
total_registrations = sum(r["count"] for r in campaign_rows)
n_campaigns         = len(campaign_rows)

if sources_available:
    email_total = sum(r["sources"].get("Email marketing", 0) for r in campaign_rows)
    email_pct   = email_total / total_registrations if total_registrations else 0
    st.markdown(
        f'<div class="kpi-band">'
        f'<div class="kpi-tile">'
        f'<div class="kpi-label">Net-new contacts</div>'
        f'<div class="kpi-value">{total_registrations:,}</div>'
        f'</div>'
        f'<div class="kpi-tile">'
        f'<div class="kpi-label">Email-sourced</div>'
        f'<div class="kpi-value">{email_total:,}</div>'
        f'</div>'
        f'<div class="kpi-tile">'
        f'<div class="kpi-label">Email share</div>'
        f'<div class="kpi-value">{email_pct:.1%}</div>'
        f'</div>'
        f'<div class="kpi-tile">'
        f'<div class="kpi-label">Campaigns tracked</div>'
        f'<div class="kpi-value">{n_campaigns}</div>'
        f'</div>'
        f'</div>',
        unsafe_allow_html=True,
    )
else:
    st.markdown(
        f'<div class="kpi-band">'
        f'<div class="kpi-tile">'
        f'<div class="kpi-label">Net-new contacts</div>'
        f'<div class="kpi-value">{total_registrations:,}</div>'
        f'</div>'
        f'<div class="kpi-tile">'
        f'<div class="kpi-label">Campaigns tracked</div>'
        f'<div class="kpi-value">{n_campaigns}</div>'
        f'</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

st.divider()

# ── Horizontal stacked bar chart ───────────────────────────────────────────────
# Sort ascending by date so Plotly's bottom→top axis puts the newest campaign at the top.
chart_rows = sorted(campaign_rows, key=lambda r: r["sort_date"])

fig = go.Figure()
if sources_available:
    for channel in CHANNEL_ORDER:
        vals = [r["sources"].get(channel, 0) for r in chart_rows]
        if sum(vals) == 0:
            continue
        fig.add_trace(go.Bar(
            name=channel,
            x=vals,
            y=[r["name"] for r in chart_rows],
            orientation="h",
            marker_color=CHANNEL_COLORS[channel],
            hovertemplate="%{y}<br>" + channel + ": %{x:,d}<extra></extra>",
        ))
else:
    # Source data failed — show error and grey registration totals as placeholder.
    err_detail = bulk_error or first_error or "0 contact IDs returned from the contacts endpoint"
    st.error(
        f"Contact source breakdown unavailable. "
        f"Error: {err_detail}"
    )
    fig.add_trace(go.Bar(
        name="Registrations (source breakdown unavailable)",
        x=[r["count"] for r in chart_rows],
        y=[r["name"] for r in chart_rows],
        orientation="h",
        marker_color=_C["gray_mid"],
        hovertemplate="%{y}<br>Registrations: %{x:,d}<extra></extra>",
    ))

fig.update_layout(
    barmode="stack",
    height=max(350, 52 * len(chart_rows) + 100),
    margin=dict(l=20, r=40, t=10, b=40),
    xaxis=dict(title="Net-new contacts", showgrid=True, gridcolor="#F0F0F0"),
    yaxis=dict(title=None),
    legend=dict(
        orientation="h",
        yanchor="bottom", y=1.02,
        xanchor="left",   x=0,
        font=dict(size=12, family="IBM Plex Sans"),
    ),
    plot_bgcolor="white",
    paper_bgcolor="white",
)

st.plotly_chart(fig, use_container_width=True)


# ── Per-campaign table ─────────────────────────────────────────────────────────
st.divider()
with st.expander("Per-campaign breakdown", expanded=True):
    table_rows = []
    for r in campaign_rows:  # already sorted most-recent first
        src        = r["sources"]
        src_total  = sum(src.values())
        email_cnt  = src.get("Email marketing", 0)
        table_rows.append({
            "Campaign":        r["name"],
            "Type":            r["type"],
            "Date":            r["sort_date"].isoformat() if r["sort_date"] != date.min else "—",
            "Registrations":   r["count"],
            "Contacts fetched": r.get("n_contacts", 0),
            "Email-sourced":   email_cnt,
            "Email %":         f"{email_cnt / src_total:.0%}" if src_total else "—",
            **{ch: src.get(ch, 0) for ch in CHANNEL_ORDER},
        })
    st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)


# ═══════════════════════════════════════════════════════════════════════════════
# TARGET ACCOUNT ADDRESSABLE AUDIENCE
# ═══════════════════════════════════════════════════════════════════════════════

st.divider()
st.subheader("Target Account Addressable Audience")

ta_segment = st.radio(
    "Segment", ["Both", "Aerospace", "Turbomachinery"], horizontal=True, key="ta_seg",
)

with st.spinner("Loading target account domains (cached 24 h)…"):
    domain_data = fetch_target_domains()

if ta_segment == "Aerospace":
    ta_domains = tuple(domain_data["aero"])
elif ta_segment == "Turbomachinery":
    ta_domains = tuple(domain_data["turbo"])
else:
    ta_domains = tuple(domain_data["all"])

if not ta_domains:
    st.warning("No target account domains found for this segment.")
else:
    # ── Compliance callout ────────────────────────────────────────────────────
    st.markdown(
        '<div style="background:#FAEEDA;border-left:4px solid #D85A30;'
        'border-radius:0 8px 8px 0;padding:1rem 1.25rem;font-size:13px;'
        'line-height:1.6;color:#633806;margin-bottom:24px;">'
        "<strong>Compliance context for this data:</strong><br><br>"
        "A bulk import of ~2,300 contacts in Feb 2025 accounts for 86% of the current "
        "target-account marketing audience. The origin of this import is under review by Legal "
        "because contacts added via purchased or third-party lists may not have provided consent "
        "to receive marketing communications from nTop.<br><br>"
        "Regulatory exposure varies by jurisdiction:<br><br>"
        "&bull; <strong>EU/UK contacts (GDPR):</strong> generally require explicit consent or a "
        "defensible legitimate-interest basis. Penalties up to &euro;20M or 4% of global revenue.<br>"
        "&bull; <strong>Canadian contacts (CASL):</strong> require express or implied consent; "
        "implied consent expires after 2 years without an active business relationship.<br>"
        "&bull; <strong>US contacts (CAN-SPAM):</strong> more permissive; opt-in not required for "
        "B2B, but unsubscribe and accurate sender info must function.<br><br>"
        "<strong>Recommended actions while review is pending:</strong> do not delete records "
        "(some regulations require retention), do not send to the EU/UK subset of this audience "
        "until consent is verified or a re-consent campaign has been run, and confirm provenance "
        "with whoever executed the import."
        "</div>",
        unsafe_allow_html=True,
    )

    # ── Monthly trend data ────────────────────────────────────────────────────
    _today = date.today()
    _months: list[tuple[int, int]] = []
    for _i in range(23, -1, -1):
        _y, _m = _today.year, _today.month - _i
        while _m <= 0:
            _m += 12
            _y -= 1
        _months.append((_y, _m))

    monthly_counts: list[dict] = []
    _month_bar = st.progress(0, text="Loading monthly target account contacts…")
    for _idx, (_y, _m) in enumerate(_months):
        _month_bar.progress(
            (_idx + 1) / len(_months),
            text=f"Month {_idx + 1}/{len(_months)}: {date(_y, _m, 1).strftime('%b %Y')}",
        )
        _cnt = _count_month(ta_domains, _y, _m)
        monthly_counts.append({
            "year": _y, "month": _m,
            "label": date(_y, _m, 1).strftime("%b %Y"),
            "date": date(_y, _m, 1),
            "count": _cnt,
        })
    _month_bar.empty()

    with st.spinner("Loading current total…"):
        ta_current_total = fetch_total_target_contacts(ta_domains)

    # ── Metric cards ──────────────────────────────────────────────────────────
    ta_last_3 = sum(mc["count"] for mc in monthly_counts[-3:])
    ta_avg_6 = sum(mc["count"] for mc in monthly_counts[-6:]) / 6 if len(monthly_counts) >= 6 else 0

    st.markdown(
        f'<div class="kpi-band">'
        f'<div class="kpi-tile">'
        f'<div class="kpi-label">Marketing contacts today</div>'
        f'<div class="kpi-value">{ta_current_total:,}{"+" if ta_current_total >= 10001 else ""}</div>'
        f'</div>'
        f'<div class="kpi-tile">'
        f'<div class="kpi-label">Added last 3 months</div>'
        f'<div class="kpi-value">{ta_last_3:,}</div>'
        f'</div>'
        f'<div class="kpi-tile">'
        f'<div class="kpi-label">Avg monthly run rate (6 mo)</div>'
        f'<div class="kpi-value">{ta_avg_6:,.0f}</div>'
        f'</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ── Cumulative line chart ─────────────────────────────────────────────────
    _x_dates = [mc["date"] for mc in monthly_counts]
    _cumulative: list[int] = []
    _running = 0
    for mc in monthly_counts:
        _running += mc["count"]
        _cumulative.append(_running)

    fig_cum = go.Figure()
    fig_cum.add_trace(go.Scatter(
        x=_x_dates, y=_cumulative, mode="lines",
        line=dict(color="#1D9E75", width=2.5),
        fill="tozeroy", fillcolor="rgba(29,158,117,0.10)",
        hovertemplate="%{x|%b %Y}<br>Cumulative: %{y:,d}<extra></extra>",
    ))
    _add_target_annotations(fig_cum)
    fig_cum.update_layout(
        height=350,
        margin=dict(l=20, r=20, t=60, b=40),
        xaxis=dict(title=None, dtick="M3", tickformat="%b %Y"),
        yaxis=dict(title="Cumulative marketing contacts", showgrid=True, gridcolor="#F0F0F0"),
        plot_bgcolor="white", paper_bgcolor="white",
    )
    st.plotly_chart(fig_cum, use_container_width=True)

    # ── Monthly bar chart (log scale) ─────────────────────────────────────────
    _bar_colors = ["#7F77DD" if mc["count"] > 100 else "#1D9E75" for mc in monthly_counts]

    fig_bar_ta = go.Figure()
    fig_bar_ta.add_trace(go.Bar(
        x=_x_dates,
        y=[mc["count"] for mc in monthly_counts],
        marker_color=_bar_colors,
        hovertemplate="%{x|%b %Y}<br>Added: %{y:,d}<extra></extra>",
    ))
    _add_target_annotations(fig_bar_ta)
    fig_bar_ta.update_layout(
        height=350,
        margin=dict(l=20, r=20, t=60, b=40),
        xaxis=dict(title=None, dtick="M3", tickformat="%b %Y"),
        yaxis=dict(
            title="Net-new contacts (log scale)", type="log",
            showgrid=True, gridcolor="#F0F0F0",
        ),
        plot_bgcolor="white", paper_bgcolor="white",
    )
    st.plotly_chart(fig_bar_ta, use_container_width=True)

    # ── Data notes callout ────────────────────────────────────────────────────
    st.markdown(
        '<div style="background:#f5f5f4;border-radius:8px;padding:1rem 1.25rem;'
        'font-size:13px;line-height:1.6;margin-top:24px;">'
        "<strong>Data notes:</strong><br><br>"
        "&bull; <strong>May 2026 forward:</strong> All forms now include unticked opt-in "
        "checkboxes for marketing communications, per Legal. Contacts post-May 2026 represent "
        "active opt-ins only, not implicit consent. Expect lower per-month numbers going forward; "
        "this is a data quality improvement, not a performance regression.<br><br>"
        "&bull; <strong>What this view shows:</strong> Current marketing contacts grouped by "
        "creation date. Contacts whose marketing-status was toggled off later are excluded. "
        "HubSpot does not store historical snapshots, so true list-size-over-time would require "
        "ongoing snapshot tracking."
        "</div>",
        unsafe_allow_html=True,
    )

    # ── HubSpot / Salesforce sync diff ────────────────────────────────────────
    if domain_data.get("sf_error"):
        st.caption(f"Salesforce comparison: {domain_data['sf_error']}")
    elif domain_data.get("hs_only") or domain_data.get("sf_only"):
        with st.expander(
            f"HubSpot / Salesforce domain diff "
            f"(HS-only: {len(domain_data['hs_only'])}, SF-only: {len(domain_data['sf_only'])})"
        ):
            if domain_data["hs_only"]:
                st.write(
                    f"**HubSpot only ({len(domain_data['hs_only'])}):** "
                    + ", ".join(domain_data["hs_only"][:30])
                    + ("…" if len(domain_data["hs_only"]) > 30 else "")
                )
            if domain_data["sf_only"]:
                st.write(
                    f"**Salesforce only ({len(domain_data['sf_only'])}):** "
                    + ", ".join(domain_data["sf_only"][:30])
                    + ("…" if len(domain_data["sf_only"]) > 30 else "")
                )


# ── Footer ─────────────────────────────────────────────────────────────────────
st.caption(
    f"Attribution window: {METRICS_START} to {date.today().isoformat()}. "
    "Source buckets from `hs_analytics_source` (HubSpot original source). "
    "Campaign metrics cached 1 h; contact source lookup cached 24 h."
)
with st.expander("Methodology"):
    st.markdown("""
**Attribution model** controls which set of contacts is counted per campaign:
- **First touch** — contacts for whom this campaign was the first known marketing touchpoint.
- **Last touch** — contacts for whom this campaign was the most recent touchpoint before conversion.

**Source bucketing** maps `hs_analytics_source` values as follows:

| HubSpot value | Chart channel |
|---|---|
| `EMAIL_MARKETING` | Email marketing |
| `ORGANIC_SEARCH` | Organic search |
| `SOCIAL_MEDIA`, `PAID_SOCIAL` | Social media |
| `OTHER_CAMPAIGNS`, `PAID_SEARCH` | Other campaigns |
| `DIRECT_TRAFFIC` | Direct |
| Everything else | Referrals / other |

Campaigns with zero registrations in the selected attribution model are hidden.
Invite-driven roadshows (Bristol, DC, FORMNEXT, SciTech, El Segundo) appear
with low counts — this is expected.
""")
