"""
Funnel Analysis page.
Spend, engagement, and lifecycle funnel by channel + Strategic/HV/Expansion opp outcomes.
"""

from datetime import date

import pandas as pd
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

# Channels we care about in the funnel view (rest get lumped as "Other")
PAID_CHANNEL_MAP = {
    ("Paid Social", "linkedin_ads"): "LinkedIn",
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
        DATE_TRUNC(DATE(created_date), MONTH) AS month_start,
        opp.record_type_id,
        opp.type,
        COUNT(*) AS opp_count,
        ROUND(SUM(COALESCE(opf.arr, 0)), 2) AS total_arr,
ROUND(SUM(COALESCE(opf.new_expansion_arr, 0)), 2) AS new_expansion_arr,
COUNTIF(COALESCE(opf.arr, 0) > 0) AS opps_with_arr
    FROM `{OPP_TABLE}` opp
LEFT JOIN `bi-ntop.google_sheets.opportunity_fields` opf
  ON opp.id = opf.opportunity_id
    WHERE opp._fivetran_deleted = FALSE
      AND opp.created_date >= TIMESTAMP(DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL @months_back MONTH), MONTH))
      AND opp.stage_name != 'Rejected'
      AND opp.type IN ('New Business', 'Expansion', 'Renewal/Expansion')
      AND opp.record_type_id IN ('{record_type_list}')
    GROUP BY month_start, opp.record_type_id, opp.type
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


# Temporary debug: load all three and show the row counts so we can confirm the loaders work
st.subheader("Loader smoke test")
st.caption("Temporary diagnostic block. Will be removed before step 3.")

try:
    spend_df = load_spend_by_channel_month(months_back=12)
    st.write(f"**Spend loader:** {len(spend_df)} rows")
    st.dataframe(spend_df.head(10), hide_index=True)
except Exception as e:
    st.error(f"Spend loader failed: {type(e).__name__}: {e}")

try:
    funnel_df = load_lifecycle_funnel_by_channel_month(months_back=12)
    st.write(f"**Funnel loader:** {len(funnel_df)} rows")
    st.dataframe(funnel_df.head(10), hide_index=True)
except Exception as e:
    st.error(f"Funnel loader failed: {type(e).__name__}: {e}")

try:
    opp_df = load_opp_outcomes_by_month(months_back=12)
    st.write(f"**Opp loader:** {len(opp_df)} rows")
    st.dataframe(opp_df.head(10), hide_index=True)
except Exception as e:
    st.error(f"Opp loader failed: {type(e).__name__}: {e}")