"""
Funnel Analysis page.
Spend, engagement, and lifecycle funnel by channel + Strategic/HV/Expansion opp outcomes.
"""

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from google.cloud import bigquery
from google.oauth2 import service_account


# Configuration
SERVICE_ACCOUNT_FILE = "service-account.json"
GCP_PROJECT = "bi-ntop"
AD_TABLE = "bi-ntop.aero_prod_ad_reporting.ad_reporting__account_report"
FUNNEL_TABLE = "bi-ntop.aero_prod.marketing_lifecycle_funnel"
OPP_TABLE = "bi-ntop.salesforce.opportunity"

# Record type IDs from project doc, verified against real data
RECORD_TYPE_LABELS = {
    "0124R000001UuQlQAK": "HV",
    "0124R000001UuQgQAK": "HV",
    "012Qo00000AyuhVIAR": "Strategic",
    "012Qo00000Avzc5IAB": "Strategic",
    "0124R000001JIhxQAG": "Expansion",
}

# Region normalization — maps country names, city entries, and sub-region variants
# back to the canonical region buckets used in account_fields.
# All values not in this map pass through unchanged.
REGION_NORMALIZE: dict[str, str] = {
    # North America catch-alls
    "Americas":                         "North America",
    "USA - East":                       "North America",
    "USA":                              "North America",
    "United States":                    "North America",
    "United states":                    "North America",
    "New York, New York":               "North America",
    "New York":                         "North America",
    "Virginia, USA":                    "North America",
    "Huntsville, Alabama":              "North America",
    "Atlanta Georgia":                  "North America",
    # Central America (Mexico entries)
    "Monterrey, Nuevo Leon, Mexico":    "Central America",
    "Chihuahua, Mexico":                "Central America",
    "Queretaro Mexio":                  "Central America",
    # South America
    "Colombia":                         "South America",
    "Chile":                            "South America",
    "Argentina":                        "South America",
    # Europe
    "Germany":                          "Western Europe",
    "France":                           "Western Europe",
    "Europe":                           "Western Europe",
    "EMEA":                             "Western Europe",
    "United Kingdom":                   "Northern Europe",
    "Norway":                           "Northern Europe",
    "Central Europe":                   "Eastern Europe",
    "EMEA East":                        "Eastern Europe",
    "Russia":                           "Eastern Europe",
    "Southern Europe - Bosnia & Herzegovina": "Southern Europe",
    # Middle East
    "Middle East - Turkey":             "Middle East",
    # Asia-Pacific
    "Japan":                            "East Asia",
    "China":                            "East Asia",
    "Hong Kong":                        "East Asia",
    "East Asia/US":                     "East Asia",
    "East Asia - South Korea":          "East Asia",
    "India":                            "South Asia",
    "South Asia - India":               "South Asia",
    "New Zealand":                      "Australia",
}

# Channels we care about in the funnel view (rest get lumped as "Other")
PAID_CHANNEL_MAP = {
    ("Paid Social", "linkedin_ads"): "LinkedIn",
    ("Warm Outbound", "linkedin_ads"): "LinkedIn",
    ("Paid Search", "google_ads"): "Google",
    ("Paid Search", "microsoft_ads"): "Microsoft",
    ("Paid Social", "facebook_ads"): "Other Paid",
    ("Paid Social", "twitter_ads"): "Other Paid",
    ("Paid Display", None): "Other Paid",
    ("Other Advertising", None): "Other Paid",
}


# Page setup
st.set_page_config(page_title="Funnel Analysis", layout="wide")
st.title("Funnel Analysis")

with st.expander("What each stage means", expanded=False):
    st.markdown("""
| Stage | Source | Definition |
|---|---|---|
| **Aware** | Deepline `marketing_lifecycle_funnel` | Accounts Deepline's model flagged as aware of nTop. Exact scoring criteria TBC — see open questions in `docs/DEEPLINE_FEEDBACK.md`. |
| **Engaged** | Deepline `marketing_lifecycle_funnel` | Accounts that reached the engaged stage per Deepline's model. Definition TBC. |
| **Contact Created** | `hubspot.contact` | Distinct accounts (any source, not just paid media) that had ≥1 HubSpot contact created in the month, linked via Salesforce account ID. **This is a full-funnel month total — it includes contacts from inbound, outbound, events, and all channels, not only paid media.** Expect it to be much larger than Aware. |
| **Lead Routed** | `salesforce.lead` | Distinct accounts with ≥1 lead routed via LeanData in the month. Only counts leads after conversion to an account — pre-conversion leads aren't attached to an account yet. |
| **Opp Created** | `salesforce.opportunity` | Distinct accounts with ≥1 qualifying new opp created in the month. Qualifying = New Business or Expansion, not Rejected, known record type (Strategic / HV / Expansion). |
| **Opp Qualified** | `salesforce.opportunity` | Distinct accounts with ≥1 opp that has moved past nTop's `1 - Qualification` stage (current stage used as a proxy). "Qualified" means the opp was accepted into active pipeline — includes opps at any stage from `2 - Discovery` onward, and also Closed Lost (they were previously accepted). This count grows over time as pipeline matures — that's expected, not a bug. Full accuracy requires `opportunity_history` and is part of the cohort model rebuild. |
| **Closed Won** | `salesforce.opportunity` | Distinct accounts with ≥1 closed won opp, grouped by close date month. |

**Notes:**
- All counts are distinct accounts, not opp or contact counts, so stages are comparable.
- Aware and Engaged come from Deepline's pre-aggregated model and can't currently be filtered by account segment — that requires the cohort model rebuild.
- Contact Created through Closed Won can be filtered by segment, region, and industry using the filters below.
- **These are calendar-month totals** (what happened across the full business in a given month), not paid-media cohort tracking (following the specific accounts from the Aware stage forward). Cohort tracking is the next rebuild — it requires account-level aware data from Deepline. Until then, Contact Created will be larger than Aware because it counts all new contacts across all channels.
    """)
    st.caption("Stage definitions are from the Funnel Analysis spec, May 2026.")


# Authentication (reuses same service account as the budget pacer)
@st.cache_resource
def get_credentials():
    return service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=[
            "https://www.googleapis.com/auth/cloud-platform",
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
        ],
    )


@st.cache_resource
def get_bq_client():
    return bigquery.Client(credentials=get_credentials(), project=GCP_PROJECT)


# Data loaders
@st.cache_data(ttl=3600)
def load_spend_by_channel_month(months_back: int = 12) -> pd.DataFrame:
    """Monthly paid media spend by platform for the last N months."""
    query = f"""
    SELECT
        DATE_TRUNC(date_day, MONTH) AS month_start,
        platform,
        ROUND(SUM(spend), 2) AS spend
    FROM `{AD_TABLE}`
    WHERE date_day >= DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL @months_back MONTH), MONTH)
      AND spend > 0
    GROUP BY month_start, platform
    ORDER BY month_start, platform
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("months_back", "INT64", months_back),
        ]
    )
    client = get_bq_client()
    df = client.query(query, job_config=job_config).to_dataframe(create_bqstorage_client=False)
    df["month_start"] = pd.to_datetime(df["month_start"]).dt.date
    return df


@st.cache_data(ttl=3600)
def load_lifecycle_funnel_by_channel_month(months_back: int = 12) -> pd.DataFrame:
    """
    Monthly lifecycle funnel counts by channel/platform from Deepline.
    Aggregates weekly report_week rows into months.
    Excludes the opp/customer stages — those come from Salesforce direct.
    """
    query = f"""
    SELECT
        DATE_TRUNC(report_week, MONTH) AS month_start,
        channel,
        platform,
        SUM(accounts_aware) AS accounts_aware,
        SUM(accounts_engaged) AS accounts_engaged,
        SUM(accounts_mqa) AS accounts_mqa,
        SUM(accounts_sqa) AS accounts_sqa
    FROM `{FUNNEL_TABLE}`
    WHERE report_week >= DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL @months_back MONTH), MONTH)
    GROUP BY month_start, channel, platform
    ORDER BY month_start, channel, platform
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("months_back", "INT64", months_back),
        ]
    )
    client = get_bq_client()
    df = client.query(query, job_config=job_config).to_dataframe(create_bqstorage_client=False)
    df["month_start"] = pd.to_datetime(df["month_start"]).dt.date

    # Group raw channel/platform into the buckets we display
    def bucket(row):
        return PAID_CHANNEL_MAP.get(
            (row["channel"], row["platform"]),
            "Non-Paid" if row["channel"] not in ("Paid Social", "Paid Search", "Paid Display", "Other Advertising")
            else "Other Paid"
        )
    df["channel_group"] = df.apply(bucket, axis=1)
    return df


@st.cache_data(ttl=3600)
def load_opp_outcomes_by_month(months_back: int = 12) -> pd.DataFrame:
    """
    Monthly opp counts and ARR from Salesforce, split by Strategic / HV / Expansion.
    Filters: NB+Expansion+Renewal/Expansion only, exclude Rejected.
    """
    record_type_list = "', '".join(RECORD_TYPE_LABELS.keys())
    query = f"""
    SELECT
        DATE_TRUNC(DATE(opp.created_date), MONTH) AS month_start,
        opp.record_type_id,
        opp.type,
        COALESCE(af.account_segment, 'Unknown') AS account_segment,
        COALESCE(af.region, 'Unknown') AS region,
        COALESCE(af.industry_vertical, 'Unknown') AS industry_vertical,
        COUNT(*) AS opp_count,
        COUNTIF(opp.is_closed = TRUE) AS opps_closed,
        COUNTIF(opp.is_won = TRUE) AS opps_won,
        ROUND(SUM(COALESCE(opf.arr, 0)), 2) AS total_arr,
        ROUND(SUM(COALESCE(opf.new_expansion_arr, 0)), 2) AS new_expansion_arr,
        COUNTIF(COALESCE(opf.arr, 0) > 0) AS opps_with_arr
    FROM `{OPP_TABLE}` opp
    LEFT JOIN `bi-ntop.google_sheets.opportunity_fields` opf
        ON opp.id = opf.opportunity_id
    LEFT JOIN `bi-ntop.google_sheets.account_fields` af
        ON opp.account_id = af._18_digit_account_id
    WHERE opp._fivetran_deleted = FALSE
      AND opp.created_date >= TIMESTAMP(DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL @months_back MONTH), MONTH))
      AND opp.stage_name != 'Rejected'
      AND opp.type IN ('New Business', 'Expansion', 'Renewal/Expansion')
      AND opp.record_type_id IN ('{record_type_list}')
    GROUP BY month_start, opp.record_type_id, opp.type,
             af.account_segment, af.region, af.industry_vertical
    ORDER BY month_start, record_type_id, type
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("months_back", "INT64", months_back),
        ]
    )
    client = get_bq_client()
    df = client.query(query, job_config=job_config).to_dataframe(create_bqstorage_client=False)
    df["month_start"] = pd.to_datetime(df["month_start"]).dt.date

    # Strip "a. " / "b. " etc. sort-prefixes from account_segment values,
    # and map "Missing Info" (z. prefix) to Unknown.
    # Raw values look like: "a. Strategic", "b. Enterprise", "z. Missing Info"
    df["account_segment"] = (
        df["account_segment"]
        .str.replace(r"^[a-z]\.\s+", "", regex=True)
        .replace("Missing Info", "Unknown")
    )

    # Normalize region outliers (country names, city entries) to canonical region buckets.
    df["region"] = df["region"].map(lambda r: REGION_NORMALIZE.get(r, r))

    # Bucket into Strategic / HV / Expansion. Expansion type wins over record_type label
    # (e.g. an HV-record-type-id but Expansion type is an Expansion).
    def bucket(row):
        if row["type"] in ("Expansion", "Renewal/Expansion"):
            return "Expansion"
        return RECORD_TYPE_LABELS.get(row["record_type_id"], "Other")
    df["segment"] = df.apply(bucket, axis=1)
    return df


@st.cache_data(ttl=3600)
def load_middle_lower_funnel_by_month(months_back: int = 18) -> pd.DataFrame:
    """
    Monthly distinct-account counts for the middle and lower funnel stages.
    These are calendar-month counts — how many accounts hit each stage in that month.
    Cohort-based tracking (following a first-aware cohort forward) requires account-level
    Deepline data and is a future rebuild.

    Sources and field assumptions:
    - Contact Created: hubspot.contact — createdate field, property_salesforceaccountid for SF link
    - Lead Routed: salesforce.lead — lean_data_routing_action_c (LeanData custom field),
      converted_account_id (only populated after LeanData conversion, per spec)
    - Opp Created / Opp Qualified / Closed Won: salesforce.opportunity

    Opp Qualified uses current stage_name as a proxy. The spec flags opportunity_history
    as the accurate source; that's a future improvement once the cohort model is built.
    """
    record_type_list = "', '".join(RECORD_TYPE_LABELS.keys())
    query = f"""
    WITH
    contact_created AS (
        SELECT
            DATE_TRUNC(DATE(property_createdate), MONTH) AS month_start,
            COUNT(DISTINCT property_salesforceaccountid) AS accounts_contact_created
        FROM `bi-ntop.hubspot.contact`
        WHERE _fivetran_deleted = FALSE
          AND property_salesforceaccountid IS NOT NULL
          AND property_createdate >= TIMESTAMP(DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL @months_back MONTH), MONTH))
        GROUP BY month_start
    ),
    lead_routed AS (
        SELECT
            DATE_TRUNC(DATE(created_date), MONTH) AS month_start,
            COUNT(DISTINCT converted_account_id) AS accounts_lead_routed
        FROM `bi-ntop.salesforce.lead`
        WHERE _fivetran_deleted = FALSE
          AND lean_data_routing_action_c IS NOT NULL
          AND converted_account_id IS NOT NULL
          AND created_date >= TIMESTAMP(DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL @months_back MONTH), MONTH))
        GROUP BY month_start
    ),
    opp_created AS (
        SELECT
            DATE_TRUNC(DATE(created_date), MONTH) AS month_start,
            COUNT(DISTINCT account_id) AS accounts_opp_created
        FROM `bi-ntop.salesforce.opportunity`
        WHERE _fivetran_deleted = FALSE
          AND stage_name != 'Rejected'
          AND type IN ('New Business', 'Expansion', 'Renewal/Expansion')
          AND record_type_id IN ('{record_type_list}')
          AND created_date >= TIMESTAMP(DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL @months_back MONTH), MONTH))
        GROUP BY month_start
    ),
    opp_qualified AS (
        -- Opps that have moved past nTop's "1 - Qualification" stage, grouped by created month.
        -- Excludes opps still sitting in stage 1 (not yet accepted) and Rejected opps.
        -- Includes Closed Lost — those were accepted into pipeline even if they didn't win.
        -- This number grows over time as pipeline matures — expected behavior.
        SELECT
            DATE_TRUNC(DATE(created_date), MONTH) AS month_start,
            COUNT(DISTINCT account_id) AS accounts_opp_qualified
        FROM `bi-ntop.salesforce.opportunity`
        WHERE _fivetran_deleted = FALSE
          AND stage_name NOT IN ('1 - Qualification', 'Rejected')
          AND type IN ('New Business', 'Expansion', 'Renewal/Expansion')
          AND record_type_id IN ('{record_type_list}')
          AND created_date >= TIMESTAMP(DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL @months_back MONTH), MONTH))
        GROUP BY month_start
    ),
    closed_won AS (
        SELECT
            DATE_TRUNC(close_date, MONTH) AS month_start,
            COUNT(DISTINCT account_id) AS accounts_closed_won
        FROM `bi-ntop.salesforce.opportunity`
        WHERE _fivetran_deleted = FALSE
          AND is_won = TRUE
          AND type IN ('New Business', 'Expansion', 'Renewal/Expansion')
          AND record_type_id IN ('{record_type_list}')
          AND close_date >= DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL @months_back MONTH), MONTH)
        GROUP BY month_start
    )
    SELECT
        m.month_start,
        COALESCE(cc.accounts_contact_created, 0) AS accounts_contact_created,
        COALESCE(lr.accounts_lead_routed, 0)      AS accounts_lead_routed,
        COALESCE(oc.accounts_opp_created, 0)      AS accounts_opp_created,
        COALESCE(oq.accounts_opp_qualified, 0)    AS accounts_opp_qualified,
        COALESCE(cw.accounts_closed_won, 0)       AS accounts_closed_won
    FROM (
        SELECT DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL n MONTH), MONTH) AS month_start
        FROM UNNEST(GENERATE_ARRAY(0, @months_back - 1)) AS n
    ) m
    LEFT JOIN contact_created cc ON m.month_start = cc.month_start
    LEFT JOIN lead_routed      lr ON m.month_start = lr.month_start
    LEFT JOIN opp_created      oc ON m.month_start = oc.month_start
    LEFT JOIN opp_qualified    oq ON m.month_start = oq.month_start
    LEFT JOIN closed_won       cw ON m.month_start = cw.month_start
    ORDER BY m.month_start
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("months_back", "INT64", months_back),
        ]
    )
    client = get_bq_client()
    df = client.query(query, job_config=job_config).to_dataframe(create_bqstorage_client=False)
    df["month_start"] = pd.to_datetime(df["month_start"]).dt.date
    return df


# Helpers
def previous_full_month(today: date) -> date:
    """Return the first day of the previous complete month."""
    first_of_this_month = today.replace(day=1)
    if first_of_this_month.month == 1:
        return date(first_of_this_month.year - 1, 12, 1)
    return date(first_of_this_month.year, first_of_this_month.month - 1, 1)


def fmt_money(value) -> str:
    if value is None or pd.isna(value):
        return "$0"
    return f"${value:,.0f}"


def fmt_count(value) -> str:
    if value is None or pd.isna(value):
        return "0"
    return f"{int(value):,}"


# Load all data once
try:
    spend_df  = load_spend_by_channel_month(months_back=18)
    funnel_df = load_lifecycle_funnel_by_channel_month(months_back=18)
    opp_df    = load_opp_outcomes_by_month(months_back=18)
    mf_df     = load_middle_lower_funnel_by_month(months_back=18)
except Exception as e:
    st.error(f"Failed to load data: {type(e).__name__}: {e}")
    st.stop()


# Month picker
today = date.today()
default_month = previous_full_month(today)
available_months = sorted(
    set(spend_df["month_start"]) | set(funnel_df["month_start"])
    | set(opp_df["month_start"]) | set(mf_df["month_start"]),
    reverse=True,
)
if default_month not in available_months:
    default_month = available_months[0]

selected_month = st.selectbox(
    "Month",
    options=available_months,
    index=available_months.index(default_month),
    format_func=lambda d: d.strftime("%B %Y"),
)

st.caption(
    "Aware and Engaged: Deepline's marketing_lifecycle_funnel model (definitions TBC — see stage definitions above). "
    "Contact Created through Closed Won: HubSpot and Salesforce direct. "
    "Opp outcomes bucketed Strategic / HV / Expansion via record_type_id. "
    "ARR from google_sheets.opportunity_fields (~19% of opps have ARR populated)."
)

# Filters — apply to opp outcomes only; upper funnel requires cohort model to filter
st.markdown("**Filters** — apply to opp outcomes section. Aware/Engaged filtering requires the cohort model rebuild.")
_fcol1, _fcol2, _fcol3 = st.columns(3)
_seg_options  = ["All"] + sorted(s for s in opp_df["account_segment"].dropna().unique() if s and s != "Unknown")
_reg_options  = ["All"] + sorted(r for r in opp_df["region"].dropna().unique() if r and r != "Unknown")
_ind_options  = ["All"] + sorted(i for i in opp_df["industry_vertical"].dropna().unique() if i and i != "Unknown")
sel_acct_seg  = _fcol1.selectbox("Account segment", _seg_options)
sel_region    = _fcol2.selectbox("Region", _reg_options)
sel_industry  = _fcol3.selectbox("Industry", _ind_options)

opp_df_filtered = opp_df.copy()
if sel_acct_seg != "All":
    opp_df_filtered = opp_df_filtered[opp_df_filtered["account_segment"] == sel_acct_seg]
if sel_region != "All":
    opp_df_filtered = opp_df_filtered[opp_df_filtered["region"] == sel_region]
if sel_industry != "All":
    opp_df_filtered = opp_df_filtered[opp_df_filtered["industry_vertical"] == sel_industry]


def render_snapshot(month: date, label_prefix: str = "", opp_data: pd.DataFrame = None) -> None:
    """
    Render the full funnel + outcomes for one month.
    opp_data: pass a pre-filtered slice of opp_df (segment/region/industry filters applied).
              Defaults to the global opp_df if not provided.
    """
    spend_month  = spend_df[spend_df["month_start"] == month]
    funnel_month = funnel_df[funnel_df["month_start"] == month]
    mf_month     = mf_df[mf_df["month_start"] == month]
    opp_month    = (opp_data if opp_data is not None else opp_df)
    opp_month    = opp_month[opp_month["month_start"] == month]

    # Funnel section
    header = f"Funnel, {month.strftime('%B %Y')}"
    if label_prefix:
        header = f"{label_prefix} — {header}"
    st.subheader(header)

    # Cohort age — helps Andrew/Kevin judge how mature down-funnel numbers are
    cohort_age_days = (today - month).days
    if month == today.replace(day=1):
        st.caption(f"Current month — {cohort_age_days} days in progress. Down-funnel stages are incomplete.")
    elif cohort_age_days < 90:
        st.caption(
            f"{month.strftime('%B %Y')} cohort — {cohort_age_days} days old. "
            "Opp creation data still maturing (stabilizes ~90 days)."
        )
    elif cohort_age_days < 365:
        st.caption(
            f"{month.strftime('%B %Y')} cohort — {cohort_age_days} days old. "
            "Closed Won still maturing (stabilizes ~12 months)."
        )
    else:
        st.caption(f"{month.strftime('%B %Y')} cohort — {cohort_age_days} days old. Reasonably mature.")

    paid_funnel = funnel_month[
        funnel_month["channel_group"].isin(["LinkedIn", "Google", "Microsoft", "Other Paid"])
    ]
    total_spend   = spend_month["spend"].sum()
    total_aware   = paid_funnel["accounts_aware"].sum()
    total_engaged = paid_funnel["accounts_engaged"].sum()
    mf_row        = mf_month.iloc[0] if not mf_month.empty else {}

    # Row 1: Spend → Aware → Engaged → Contact Created
    r1c1, r1c2, r1c3, r1c4 = st.columns(4)
    r1c1.metric("Paid Spend",       fmt_money(total_spend))
    r1c2.metric("Aware",            fmt_count(total_aware))
    r1c3.metric("Engaged",          fmt_count(total_engaged))
    r1c4.metric("Contact Created",  fmt_count(mf_row.get("accounts_contact_created", 0)))

    # Row 2: Lead Routed → Opp Created → Opp Qualified → Closed Won
    r2c1, r2c2, r2c3, r2c4 = st.columns(4)
    r2c1.metric("Lead Routed",    fmt_count(mf_row.get("accounts_lead_routed", 0)))
    r2c2.metric("Opp Created",    fmt_count(mf_row.get("accounts_opp_created", 0)))
    r2c3.metric("Opp Qualified",  fmt_count(mf_row.get("accounts_opp_qualified", 0)))
    r2c4.metric("Closed Won",     fmt_count(mf_row.get("accounts_closed_won", 0)))

    # Channel breakdown — Aware and Engaged only (channel attribution stops here;
    # Contact Created onward has no paid-channel dimension in current data)
    st.markdown("**Aware & Engaged by paid channel**")
    channel_view = paid_funnel.groupby("channel_group", as_index=False).agg({
        "accounts_aware": "sum",
        "accounts_engaged": "sum",
    })
    spend_by_channel = spend_month.groupby("platform", as_index=False).agg({"spend": "sum"})
    platform_to_channel = {
        "linkedin_ads": "LinkedIn",
        "google_ads": "Google",
        "microsoft_ads": "Microsoft",
        "reddit_ads": "Other Paid",
        "facebook_ads": "Other Paid",
    }
    spend_by_channel["channel_group"] = (
        spend_by_channel["platform"].map(platform_to_channel).fillna("Other Paid")
    )
    spend_by_channel = spend_by_channel.groupby("channel_group", as_index=False).agg({"spend": "sum"})
    channel_view = channel_view.merge(spend_by_channel, on="channel_group", how="left")
    channel_view["spend"] = channel_view["spend"].fillna(0)
    channel_view = channel_view[["channel_group", "spend", "accounts_aware", "accounts_engaged"]]
    channel_view = channel_view.rename(columns={
        "channel_group": "Channel",
        "spend": "Spend ($)",
        "accounts_aware": "Aware",
        "accounts_engaged": "Engaged",
    })
    st.dataframe(
        channel_view,
        hide_index=True,
        use_container_width=True,
        column_config={
            "Spend ($)": st.column_config.NumberColumn(format="$%.0f"),
            "Aware":     st.column_config.NumberColumn(format="%d"),
            "Engaged":   st.column_config.NumberColumn(format="%d"),
        },
    )

    # Outcomes section
    outcomes_header = f"Outcomes, {month.strftime('%B %Y')}"
    if label_prefix:
        outcomes_header = f"{label_prefix} — {outcomes_header}"
    st.markdown(f"**{outcomes_header}**")

    outcomes = opp_month.groupby("segment", as_index=False).agg({
        "opp_count": "sum",
        "opps_closed": "sum",
        "opps_won": "sum",
        "total_arr": "sum",
        "new_expansion_arr": "sum",
        "opps_with_arr": "sum",
    })
    for segment in ["Strategic", "HV", "Expansion"]:
        if segment not in outcomes["segment"].values:
            outcomes = pd.concat([
                outcomes,
                pd.DataFrame([{
                    "segment": segment, "opp_count": 0, "opps_closed": 0,
                    "opps_won": 0, "total_arr": 0,
                    "new_expansion_arr": 0, "opps_with_arr": 0,
                }]),
            ], ignore_index=True)
    segment_order = {"Strategic": 0, "HV": 1, "Expansion": 2}
    outcomes = outcomes[outcomes["segment"].isin(segment_order.keys())]
    outcomes = outcomes.sort_values("segment", key=lambda s: s.map(segment_order))

    oc1, oc2, oc3 = st.columns(3)
    for col, (_, row) in zip([oc1, oc2, oc3], outcomes.iterrows()):
        col.metric(
            f"{row['segment']} opps",
            fmt_count(row["opp_count"]),
            f"{fmt_money(row['total_arr'])} ARR ({int(row['opps_with_arr'])} of {int(row['opp_count'])} with ARR)",
            delta_color="off",
        )

    wc1, wc2, wc3 = st.columns(3)
    for col, (_, row) in zip([wc1, wc2, wc3], outcomes.iterrows()):
        opps_closed = int(row["opps_closed"])
        opps_won = int(row["opps_won"])
        opps_lost = opps_closed - opps_won
        opps_with_arr = int(row["opps_with_arr"])

        # Guard: hide win rate if cohort is under 90 days old or fewer than 5 closed opps —
        # a single closed deal reads as 100% which is more misleading than no number
        if cohort_age_days < 90 or opps_closed < 5:
            win_rate_display = "N/A — too early"
        else:
            win_rate_display = f"{opps_won / opps_closed:.0%}"

        col.metric(
            f"{row['segment']} win rate",
            win_rate_display,
            f"{opps_won} won / {opps_lost} lost of {opps_closed} closed",
            delta_color="off",
        )

        # Guard: hide avg deal size if fewer than 3 opps have ARR populated
        if opps_with_arr < 3:
            col.caption(f"Avg deal: N/A ({opps_with_arr} of {int(row['opp_count'])} opps have ARR)")
        else:
            avg_deal = row["total_arr"] / opps_with_arr
            col.caption(f"Avg deal: {fmt_money(avg_deal)} ({opps_with_arr} opps with ARR)")


# View selector
view_mode = st.radio(
    "View",
    options=["Single month", "Month-over-month", "Trend over time"],
    horizontal=True,
    label_visibility="collapsed",
)

st.divider()

if view_mode == "Single month":
    render_snapshot(selected_month, opp_data=opp_df_filtered)
elif view_mode == "Month-over-month":
    available_for_comparison = [m for m in available_months if m != selected_month]
    if not available_for_comparison:
        st.warning("Need at least two months of data to compare.")
    else:
        comparison_month = st.selectbox(
            "Compare to",
            options=available_for_comparison,
            index=0,
            format_func=lambda d: d.strftime("%B %Y"),
            key="comparison_month",
        )
        st.divider()
        col_left, col_right = st.columns(2)
        with col_left:
            render_snapshot(selected_month, label_prefix="A", opp_data=opp_df_filtered)
        with col_right:
            render_snapshot(comparison_month, label_prefix="B", opp_data=opp_df_filtered)

else:  # Trend over time
    # Use the last 18 months ascending so the chart reads left to right
    trend_months = sorted(set(funnel_df["month_start"]))[-18:]

    # Build a wide table: rows = month, cols = stages, values = totals across paid channels
    paid_only = funnel_df[
        funnel_df["channel_group"].isin(["LinkedIn", "Google", "Microsoft", "Other Paid"])
    ]
    funnel_trend = paid_only.groupby("month_start", as_index=False).agg({
        "accounts_aware": "sum",
        "accounts_engaged": "sum",
        "accounts_mqa": "sum",
        "accounts_sqa": "sum",
    })
    spend_trend = spend_df.groupby("month_start", as_index=False).agg({"spend": "sum"})

    # Reindex both tables to the full 18-month spine so months with no paid data
    # still appear on the x-axis (as gaps / zero bars) rather than being dropped silently
    _spine = pd.DataFrame({"month_start": trend_months})
    funnel_trend = _spine.merge(
        funnel_trend[funnel_trend["month_start"].isin(trend_months)],
        on="month_start", how="left",
    )  # missing months stay as NaN → Plotly shows a visible gap in the line
    spend_trend = _spine.merge(
        spend_trend[spend_trend["month_start"].isin(trend_months)],
        on="month_start", how="left",
    ).fillna(0)

    # The most recent 3 months are "still maturing" for cohort purposes
    maturity_cutoff = trend_months[-3] if len(trend_months) >= 3 else trend_months[0]

    st.subheader("Funnel trend, last 18 months")
    st.info(
        f"**The last 3 months ({trend_months[-3].strftime('%b')}, "
        f"{trend_months[-2].strftime('%b')}, "
        f"{trend_months[-1].strftime('%b %Y')}) are shown faded** because deals and "
        "upper-funnel accounts from those months are still being created. "
        "Use them as a directional signal, not a final number.",
        icon="ℹ️",
    )

    # Also build the middle/lower funnel trend spine
    mf_trend = mf_df.groupby("month_start", as_index=False).agg({
        "accounts_contact_created": "sum",
        "accounts_lead_routed":     "sum",
        "accounts_opp_created":     "sum",
        "accounts_opp_qualified":   "sum",
        "accounts_closed_won":      "sum",
    })
    mf_trend = _spine.merge(
        mf_trend[mf_trend["month_start"].isin(trend_months)],
        on="month_start", how="left",
    )

    def add_line(fig, df, x_col, y_col, name, color):
        """Add a solid (mature) + dotted/faded (maturing) line pair to fig."""
        mature   = df[df[x_col] < maturity_cutoff]
        maturing = df[df[x_col] >= maturity_cutoff]
        if not mature.empty:
            fig.add_trace(go.Scatter(
                x=mature[x_col], y=mature[y_col], name=name,
                mode="lines+markers", line=dict(color=color, width=3),
                legendgroup=name, showlegend=True,
            ))
        if not maturing.empty:
            fig.add_trace(go.Scatter(
                x=maturing[x_col], y=maturing[y_col], name=name,
                mode="lines+markers",
                line=dict(color=color, width=3, dash="dot"),
                marker=dict(symbol="circle-open"),
                opacity=0.5,
                legendgroup=name, showlegend=False,
            ))

    # Upper funnel chart — Aware and Engaged (Deepline)
    upper_fig = go.Figure()
    add_line(upper_fig, funnel_trend, "month_start", "accounts_aware",   "Aware",   "#0047FF")
    add_line(upper_fig, funnel_trend, "month_start", "accounts_engaged", "Engaged", "#34A853")
    upper_fig.update_layout(
        height=320,
        margin=dict(l=40, r=40, t=20, b=40),
        yaxis=dict(title="Accounts"),
        xaxis=dict(title=None),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode="x unified",
    )
    st.markdown("**Aware & Engaged (Deepline)**")
    st.plotly_chart(upper_fig, use_container_width=True)

    # Lower funnel chart — Contact Created → Closed Won (HubSpot + Salesforce)
    lower_fig = go.Figure()
    add_line(lower_fig, mf_trend, "month_start", "accounts_contact_created", "Contact Created", "#7B61FF")
    add_line(lower_fig, mf_trend, "month_start", "accounts_lead_routed",     "Lead Routed",     "#FF6B35")
    add_line(lower_fig, mf_trend, "month_start", "accounts_opp_created",     "Opp Created",     "#0047FF")
    add_line(lower_fig, mf_trend, "month_start", "accounts_opp_qualified",   "Opp Qualified",   "#F5A623")
    add_line(lower_fig, mf_trend, "month_start", "accounts_closed_won",      "Closed Won",      "#34A853")
    lower_fig.update_layout(
        height=320,
        margin=dict(l=40, r=40, t=20, b=40),
        yaxis=dict(title="Accounts"),
        xaxis=dict(title=None),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode="x unified",
    )
    st.markdown("**Contact Created → Closed Won (HubSpot & Salesforce)**")
    st.plotly_chart(lower_fig, use_container_width=True)

    # Spend trend below
    st.markdown("**Paid spend by month**")
    spend_fig = go.Figure()
    spend_fig.add_trace(go.Bar(
        x=spend_trend["month_start"],
        y=spend_trend["spend"],
        marker_color=[
            "#0047FF" if m < maturity_cutoff else "rgba(0,71,255,0.4)"
            for m in spend_trend["month_start"]
        ],
    ))
    spend_fig.update_layout(
        height=300,
        margin=dict(l=40, r=40, t=20, b=40),
        yaxis=dict(title="Spend ($)"),
        xaxis=dict(title=None),
        bargap=0.2,
        showlegend=False,
    )
    st.plotly_chart(spend_fig, use_container_width=True)

    # Outcomes trend: opp counts by segment, stacked
    st.markdown("**Opp outcomes by month**")
    outcomes_trend = opp_df[opp_df["month_start"].isin(trend_months)].copy()
    outcomes_pivot = outcomes_trend.pivot_table(
        index="month_start", columns="segment", values="opp_count", aggfunc="sum"
    ).fillna(0).reset_index()
    for segment in ["Strategic", "HV", "Expansion"]:
        if segment not in outcomes_pivot.columns:
            outcomes_pivot[segment] = 0
    # Guarantee all 18 trend months appear on the x-axis even if a month has no qualifying opps
    outcomes_pivot = (
        pd.DataFrame({"month_start": trend_months})
        .merge(outcomes_pivot, on="month_start", how="left")
        .fillna(0)
    )

    outcomes_fig = go.Figure()
    seg_colors = {"Strategic": "#0047FF", "HV": "#F5A623", "Expansion": "#34A853"}
    for segment in ["Strategic", "HV", "Expansion"]:
        mature_mask = outcomes_pivot["month_start"] < maturity_cutoff
        outcomes_fig.add_trace(go.Bar(
            x=outcomes_pivot["month_start"],
            y=outcomes_pivot[segment],
            name=segment,
            marker=dict(
                color=seg_colors[segment],
                opacity=[1.0 if mature else 0.4 for mature in mature_mask],
            ),
        ))
    outcomes_fig.update_layout(
        barmode="stack",
        height=350,
        margin=dict(l=40, r=40, t=20, b=40),
        yaxis=dict(title="Opps created"),
        xaxis=dict(title=None),
        bargap=0.2,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    st.plotly_chart(outcomes_fig, use_container_width=True)