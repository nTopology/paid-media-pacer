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
AD_TABLE          = "bi-ntop.aero_prod_ad_reporting.ad_reporting__account_report"
AD_CAMPAIGN_TABLE = "bi-ntop.aero_prod_ad_reporting.ad_reporting__campaign_report"
FUNNEL_TABLE      = "bi-ntop.aero_prod.marketing_lifecycle_funnel"
OPP_TABLE         = "bi-ntop.salesforce.opportunity"

# Record type IDs from project doc, verified against real data
RECORD_TYPE_LABELS = {
    "0124R000001UuQlQAK": "HV",
    "0124R000001UuQgQAK": "HV",
    "012Qo00000AyuhVIAR": "Strategic",
    "012Qo00000Avzc5IAB": "Strategic",
    "0124R000001JIhxQAG": "Expansion",
}

# All raw region values in account_fields that represent US-based accounts.
# Everything not in this set becomes "International".
_US_RAW_REGIONS: frozenset[str] = frozenset({
    "North America",
    "Americas",
    "USA - East",
    "USA",
    "United States",
    "United states",
    "New York, New York",
    "New York",
    "Virginia, USA",
    "Huntsville, Alabama",
    "Atlanta Georgia",
})

# Channels we care about in the funnel view (rest get lumped as "Other")


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
def load_spend_by_channel_month(months_back: int = 18) -> pd.DataFrame:
    """
    Monthly paid media spend by channel group, sourced from campaign-level data
    so YouTube can be split from Google Search.

    YouTube detection: campaign_name ILIKE '%video%' on google_ads.
    LinkedIn campaigns don't need a split — all map to 'LinkedIn'.

    channel_group values in the result:
        'LinkedIn'       — all linkedin_ads campaigns
        'Google Search'  — google_ads campaigns whose name does NOT contain 'video'
        'YouTube'        — google_ads campaigns whose name contains 'video'
        other platforms  — kept as raw platform string for historical months
    """
    query = f"""
    SELECT
        DATE_TRUNC(date_day, MONTH) AS month_start,
        CASE
            WHEN platform = 'linkedin_ads'                              THEN 'LinkedIn'
            WHEN platform = 'google_ads'
             AND LOWER(campaign_name) LIKE '%video%'                   THEN 'YouTube'
            WHEN platform = 'google_ads'                               THEN 'Google Search'
            ELSE platform
        END AS channel_group,
        ROUND(SUM(spend), 2) AS spend
    FROM `{AD_CAMPAIGN_TABLE}`
    WHERE date_day >= DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL @months_back MONTH), MONTH)
      AND spend > 0
    GROUP BY month_start, channel_group
    ORDER BY month_start, channel_group
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

    # Classify each Deepline row into a channel group.
    #
    # LinkedIn: rows explicitly tagged with linkedin_ads platform.
    # Google:   rows tagged with google_ads platform, PLUS "Paid Search" rows
    #           with null platform (Deepline drops the platform tag on some rows;
    #           Google is the only active paid search platform in 2026 so these
    #           are attributed here rather than buried in Non-Paid).
    # Non-Paid: everything else — organic, email, direct, events, (Other), etc.
    #           These go into the "Other influenced" table, not the paid table.
    #
    # No "Other Paid" bucket: Microsoft and Reddit have $0 spend in 2026.
    # If they reactivate, add them back explicitly rather than via a catch-all.
    def bucket(row):
        ch, pl = row["channel"], row["platform"]
        if pl == "linkedin_ads" and ch in ("Paid Social", "Warm Outbound"):
            return "LinkedIn"
        if pl == "google_ads" and ch == "Paid Search":
            return "Google"
        if pl is None and ch == "Paid Search":
            # Paid Search with missing platform tag.  Only active search platform
            # in 2026 is Google, so classify here.  Revisit if Microsoft reactivates.
            return "Google"
        return "Non-Paid"

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


def normalize_opp_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Apply segment and region normalization to the opp outcomes DataFrame.
    This runs OUTSIDE the cache so normalization changes take effect immediately
    on the next page reload — no need to wait for the 1-hour TTL to expire.
    The expensive BQ query is still cached; this is just cheap Python string ops.
    """
    df = df.copy()
    # Strip "a. " / "b. " sort-prefixes from raw account_segment values.
    # "z. Missing Info" → "Unknown" so it doesn't pollute the dropdown.
    df["account_segment"] = (
        df["account_segment"]
        .str.replace(r"^[a-z]\.\s+", "", regex=True)
        .replace("Missing Info", "Unknown")
    )
    # Collapse region to a simple US / International split.
    df["region"] = df["region"].map(
        lambda r: "US" if r in _US_RAW_REGIONS else "International"
    )
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
    opp_df    = normalize_opp_df(load_opp_outcomes_by_month(months_back=18))
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


def render_snapshot(
    month: date,
    label_prefix: str = "",
    opp_pre_filtered: pd.DataFrame | None = None,
    key_suffix: str = "",
) -> None:
    """
    Render the full funnel + outcomes for one month.

    opp_pre_filtered: pass an already-filtered opp_df slice (e.g. from MoM shared filters).
                      When None (single-month mode), filter widgets are rendered inline
                      just above the outcomes section.
    key_suffix: appended to widget keys to avoid Streamlit duplicate-key errors when this
                function is called more than once per page (MoM mode).
    """
    spend_month  = spend_df[spend_df["month_start"] == month]
    funnel_month = funnel_df[funnel_df["month_start"] == month]
    mf_month     = mf_df[mf_df["month_start"] == month]
    # opp_month is resolved later, just before the outcomes section

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

    paid_funnel = funnel_month[funnel_month["channel_group"].isin(["LinkedIn", "Google"])]
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

    # ── Paid channel table ────────────────────────────────────────────────────
    # Only channels with actual spend in this month.
    # Google is split into Search and YouTube by campaign name (contains 'video').
    # Attribution (Aware/Engaged) comes from Deepline; spend from the campaign table.
    # Deepline doesn't expose campaign IDs, so Google attribution can't be split
    # by campaign type — all Google-attributed accounts appear on the Search row.
    st.markdown("**Paid channels — Aware & Engaged**")

    paid_rows = []

    # LinkedIn
    lk_funnel = funnel_month[funnel_month["channel_group"] == "LinkedIn"]
    lk_spend  = spend_month[spend_month["channel_group"] == "LinkedIn"]["spend"].sum()
    if lk_spend > 0:
        paid_rows.append({
            "Channel": "LinkedIn",
            "Spend ($)": lk_spend,
            "Aware":   int(lk_funnel["accounts_aware"].sum()),
            "Engaged": int(lk_funnel["accounts_engaged"].sum()),
        })

    # Google — spend split by campaign type; attribution shows combined total
    search_spend = spend_month[spend_month["channel_group"] == "Google Search"]["spend"].sum()
    yt_spend     = spend_month[spend_month["channel_group"] == "YouTube"]["spend"].sum()
    goo_funnel   = funnel_month[funnel_month["channel_group"] == "Google"]
    goo_aware    = int(goo_funnel["accounts_aware"].sum())
    goo_engaged  = int(goo_funnel["accounts_engaged"].sum())
    google_split = (search_spend > 0 and yt_spend > 0)  # both active → need footnote

    if search_spend > 0:
        paid_rows.append({
            "Channel":  "Google Search" + (" †" if google_split else ""),
            "Spend ($)": search_spend,
            "Aware":    goo_aware,    # full google_ads total — can't split by campaign type
            "Engaged":  goo_engaged,
        })
    if yt_spend > 0:
        paid_rows.append({
            "Channel":  "YouTube" + (" †" if google_split else ""),
            "Spend ($)": yt_spend,
            "Aware":    None,  # same accounts as Google Search row above; not double-counted
            "Engaged":  None,
        })

    if paid_rows:
        st.dataframe(
            pd.DataFrame(paid_rows),
            hide_index=True,
            use_container_width=True,
            column_config={
                "Spend ($)": st.column_config.NumberColumn(format="$%.0f"),
                "Aware":     st.column_config.NumberColumn(format="%d"),
                "Engaged":   st.column_config.NumberColumn(format="%d"),
            },
        )
        if google_split:
            st.caption(
                "† Deepline's attribution links accounts to google_ads but not to individual "
                "campaigns, so the Google/YouTube split can't be applied to Aware/Engaged. "
                f"Google total this month: {goo_aware:,} aware, {goo_engaged:,} engaged — "
                "shown on the Google Search row. YouTube row shows spend only."
            )
    else:
        st.caption("No paid channel spend in this month.")

    # ── Other influenced channels (non-paid) ─────────────────────────────────
    # Deepline attributes accounts to channels beyond paid — organic search, email,
    # direct, events, etc.  These are real signals but structurally have $0 spend,
    # so they don't belong in the paid table.
    non_paid = funnel_month[funnel_month["channel_group"] == "Non-Paid"].copy()
    if not non_paid.empty:
        other_view = (
            non_paid.groupby("channel", as_index=False)
            .agg({"accounts_aware": "sum", "accounts_engaged": "sum"})
            .query("accounts_aware > 0 or accounts_engaged > 0")
            .sort_values("accounts_aware", ascending=False)
        )
        # Give Deepline's anonymous catch-all a more descriptive label
        other_view["channel"] = other_view["channel"].replace(
            "(Other)", "(Unattributed — Deepline catch-all)"
        )
        other_view = other_view.rename(columns={
            "channel":           "Channel",
            "accounts_aware":    "Aware",
            "accounts_engaged":  "Engaged",
        })
        st.markdown("**Other influenced channels (non-paid, no spend)**")
        st.caption(
            "Deepline attributes these accounts to non-paid touch-points. "
            "No spend column because there's no paid budget behind them. "
            "Kept separate so they don't inflate the paid CPA."
        )
        st.dataframe(
            other_view,
            hide_index=True,
            use_container_width=True,
            column_config={
                "Aware":   st.column_config.NumberColumn(format="%d"),
                "Engaged": st.column_config.NumberColumn(format="%d"),
            },
        )

    # ── Outcomes section ─────────────────────────────────────────────────────
    st.divider()
    outcomes_header = f"Outcomes, {month.strftime('%B %Y')}"
    if label_prefix:
        outcomes_header = f"{label_prefix} — {outcomes_header}"
    st.markdown(f"**{outcomes_header}**")

    # Filters live here, right above the outcomes numbers.
    # In single-month mode (opp_pre_filtered is None) we render widgets inline.
    # In MoM mode the caller renders one shared filter block and passes the
    # result in as opp_pre_filtered, so we skip the widgets here.
    if opp_pre_filtered is None:
        _fc1, _fc2, _fc3 = st.columns(3)
        _seg_opts = ["All"] + sorted(
            s for s in opp_df["account_segment"].dropna().unique() if s and s != "Unknown"
        )
        _reg_opts = ["All"] + sorted(
            r for r in opp_df["region"].dropna().unique() if r and r != "Unknown"
        )
        _ind_opts = ["All"] + sorted(
            i for i in opp_df["industry_vertical"].dropna().unique() if i and i != "Unknown"
        )
        _sel_seg = _fc1.selectbox("Account segment", _seg_opts, key=f"seg_{key_suffix}")
        _sel_geo = _fc2.selectbox("Geography",        _reg_opts, key=f"geo_{key_suffix}")
        _sel_ind = _fc3.selectbox("Industry",         _ind_opts, key=f"ind_{key_suffix}")
        opp_month = opp_df[opp_df["month_start"] == month].copy()
        if _sel_seg != "All":
            opp_month = opp_month[opp_month["account_segment"] == _sel_seg]
        if _sel_geo != "All":
            opp_month = opp_month[opp_month["region"] == _sel_geo]
        if _sel_ind != "All":
            opp_month = opp_month[opp_month["industry_vertical"] == _sel_ind]
    else:
        opp_month = opp_pre_filtered[opp_pre_filtered["month_start"] == month]

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
    # Filters render inline inside render_snapshot, right above outcomes
    render_snapshot(selected_month, key_suffix="sm")

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
        # One shared filter block for both columns — you want to compare the same
        # segment/geo/industry across the two months, not filter each independently
        st.markdown("**Outcomes filters**")
        _mc1, _mc2, _mc3 = st.columns(3)
        _m_seg_opts = ["All"] + sorted(
            s for s in opp_df["account_segment"].dropna().unique() if s and s != "Unknown"
        )
        _m_reg_opts = ["All"] + sorted(
            r for r in opp_df["region"].dropna().unique() if r and r != "Unknown"
        )
        _m_ind_opts = ["All"] + sorted(
            i for i in opp_df["industry_vertical"].dropna().unique() if i and i != "Unknown"
        )
        _m_sel_seg = _mc1.selectbox("Account segment", _m_seg_opts, key="mom_seg")
        _m_sel_geo = _mc2.selectbox("Geography",        _m_reg_opts, key="mom_geo")
        _m_sel_ind = _mc3.selectbox("Industry",         _m_ind_opts, key="mom_ind")
        _mom_filtered = opp_df.copy()
        if _m_sel_seg != "All":
            _mom_filtered = _mom_filtered[_mom_filtered["account_segment"] == _m_sel_seg]
        if _m_sel_geo != "All":
            _mom_filtered = _mom_filtered[_mom_filtered["region"] == _m_sel_geo]
        if _m_sel_ind != "All":
            _mom_filtered = _mom_filtered[_mom_filtered["industry_vertical"] == _m_sel_ind]

        col_left, col_right = st.columns(2)
        with col_left:
            render_snapshot(selected_month, label_prefix="A", opp_pre_filtered=_mom_filtered)
        with col_right:
            render_snapshot(comparison_month, label_prefix="B", opp_pre_filtered=_mom_filtered)

else:  # Trend over time
    # Use the last 18 months ascending so the chart reads left to right
    trend_months = sorted(set(funnel_df["month_start"]))[-18:]

    # Build a wide table: rows = month, cols = stages, values = totals across paid channels
    paid_only = funnel_df[funnel_df["channel_group"].isin(["LinkedIn", "Google"])]
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
    st.divider()
    st.markdown("**Opp outcomes by month**")
    # Filters right above the chart
    _tc1, _tc2, _tc3 = st.columns(3)
    _t_seg_opts = ["All"] + sorted(
        s for s in opp_df["account_segment"].dropna().unique() if s and s != "Unknown"
    )
    _t_reg_opts = ["All"] + sorted(
        r for r in opp_df["region"].dropna().unique() if r and r != "Unknown"
    )
    _t_ind_opts = ["All"] + sorted(
        i for i in opp_df["industry_vertical"].dropna().unique() if i and i != "Unknown"
    )
    _t_sel_seg = _tc1.selectbox("Account segment", _t_seg_opts, key="trend_seg")
    _t_sel_geo = _tc2.selectbox("Geography",        _t_reg_opts, key="trend_geo")
    _t_sel_ind = _tc3.selectbox("Industry",         _t_ind_opts, key="trend_ind")
    _trend_opp = opp_df.copy()
    if _t_sel_seg != "All":
        _trend_opp = _trend_opp[_trend_opp["account_segment"] == _t_sel_seg]
    if _t_sel_geo != "All":
        _trend_opp = _trend_opp[_trend_opp["region"] == _t_sel_geo]
    if _t_sel_ind != "All":
        _trend_opp = _trend_opp[_trend_opp["industry_vertical"] == _t_sel_ind]
    outcomes_trend = _trend_opp[_trend_opp["month_start"].isin(trend_months)].copy()
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