# Deepline Feedback & Open Questions

**From:** Michael Fynn, nTop Marketing  
**To:** Jai / Saf, Deepline  
**Context:** We're building a paid media funnel analysis dashboard in Streamlit that pulls from Deepline's `marketing_lifecycle_funnel` model alongside Salesforce and ad platform data. This doc captures questions and data issues we hit during that build.

---

## 1. `accounts_opp` count is 21x higher than Salesforce actuals

**Table:** `bi-ntop.aero_prod.marketing_lifecycle_funnel`  
**Field:** `accounts_opp`

When we sum `accounts_opp` over the last 12 months, we get roughly **29,000**. The actual count of New Business + Expansion + Renewal/Expansion opportunities in `bi-ntop.salesforce.opportunity` over the same period (non-rejected, known record types) is **~1,382**.

That's a 21x difference. We decided not to use `accounts_opp` in the dashboard at all and pull opp counts directly from Salesforce instead. But we'd like to understand what `accounts_opp` is actually counting — is it account-touches, pipeline signals, or something else? Knowing this would help us figure out whether there's a valid use for it.

---

## 2. February 2026 gap in paid-channel funnel data

**Table:** `bi-ntop.aero_prod.marketing_lifecycle_funnel`

When we filter the funnel table to paid channels (LinkedIn, Google, Microsoft) for February 2026, we get no rows. Spend data from the ad platforms shows normal LinkedIn and Google activity in February, so the campaigns were running. The gap makes the funnel line chart skip February entirely.

Possible causes we thought of but can't confirm:
- A processing or backfill gap in the model for that month
- The `report_week` field spanning month boundaries in a way that drops some February weeks into January or March when truncated
- Channel or platform label values in the February data that differ from the labels used in other months

Can you confirm whether this is a known gap or a labeling issue we should account for on our end?

---

## 3. Stage definitions and ordering: what do MQA and SQA actually mean?

**Table:** `bi-ntop.aero_prod.marketing_lifecycle_funnel`  
**Fields:** `accounts_aware`, `accounts_engaged`, `accounts_mqa`, `accounts_sqa`

We're displaying these four fields as funnel stages in the dashboard, but we don't have confirmed definitions for what MQA and SQA mean in the context of this model. Specific questions:

- **Aware**: is this an account that received an ad impression? Visited the site? Something else?
- **Engaged**: clicked an ad? Spent time on site? Multi-touch threshold?
- **MQA**: what scoring threshold or signal triggers this? Is it nTop's standard MQA definition or Deepline's own?
- **SQA**: is this equivalent to an SQL/SAL at nTop? How does it relate to LeanData routing?

We're also seeing `accounts_mqa` higher than `accounts_aware` for the same channel and period in some months. A traditional funnel requires each downstream stage to be smaller than the one above it. Are these cumulative counts, point-in-time snapshots, or something else? Is the non-monotonic ordering expected behavior?

We've worked around this by showing absolute counts only (no conversion rates between stages), but confirming the definitions would let us label these stages correctly for an executive audience.

---

## 4. `marketing_lead_source_c` is very sparse

**Table:** `bi-ntop.aero_prod.marketing_lifecycle_funnel` (or related model)

We explored using `marketing_lead_source_c` to attribute upper-funnel accounts to channels, but the field is populated on only a small fraction of records. This made channel-level attribution unreliable, so we dropped it.

Is there a more reliable field or join path for attributing an account's first paid-media touch to a specific channel? Even a rough attribution would be useful for the LinkedIn vs. Google funnel breakdown we'd like to show.

---

## 5. Aware/Engaged can't be filtered by account segment

**Table:** `bi-ntop.aero_prod.marketing_lifecycle_funnel`

The funnel model rolls up at the channel grain with no account segment field. Segment, region, and industry filters (from `google_sheets.account_fields`) will work for the Salesforce-based stages (Opp Created onward) once the cohort model is built, because those join on account IDs we control. But Aware and Engaged can't be filtered by segment until Deepline either adds a segment dimension to this model or provides account-level rows we can join against `account_fields` ourselves.

The dashboard will show segment filters on Opp stages first, then extend to Aware/Engaged if Deepline adds the dimension.

---

## 6. Unexpected `record_type_id` in Salesforce opportunity data

**Table:** `bi-ntop.salesforce.opportunity`  
**Field:** `record_type_id`

Our project documentation listed these as the known record type IDs:

| ID | Segment |
|---|---|
| `0124R000001UuQlQAK` | HV |
| `0124R000001UuQgQAK` | HV |
| `012Qo00000AyuhVIAR` | Strategic |
| `012Qo00000Avzc5IAB` | Strategic |
| `0124R000001JIhxQAG` | Expansion |

In live data we also saw `0124R000001UuQIQAK` — note the capital `I` near the end, vs. the lowercase `l` in `0124R000001UuQlQAK`. These look nearly identical but are different IDs. We don't know which segment this belongs to.

This isn't a Deepline question per se (it's a Salesforce config question), but flagging it here since Deepline's models consume this data and may need to account for it.

---

## 7. Account-level funnel data needed for cohort model

**Table:** `bi-ntop.aero_prod.marketing_lifecycle_funnel`

The next major version of the dashboard will track **account cohorts** — grouping accounts by the month they first appeared in the aware stage, then following that group forward through every subsequent funnel stage over time. This is a fundamentally different model from the current approach (which just counts whatever happened in a given calendar month).

Building this requires one row per account with a first-aware timestamp — not pre-aggregated counts. The current table as we're querying it groups by `channel`, `platform`, and `report_week` with no `account_id` field visible.

Before we can build the cohort model, we need to know:

1. Does `marketing_lifecycle_funnel` (or a related table in `aero_prod`) have **account-level rows** with per-account stage timestamps or first-touch dates?
2. If not, is there a separate table — something like an `account_funnel_history` or `account_stage_events` — we should be using instead?
3. If the current table is the right source, what field contains the account identifier, and how is the first-aware month defined for a given account?

This is the most important open question for the dashboard rebuild.

---

## Summary table

| # | Issue | Impact on dashboard | Resolution needed from |
|---|---|---|---|
| 1 | `accounts_opp` 21x inflated vs Salesforce | Not using it; showing SF direct instead | Deepline — what does this field count? |
| 2 | February 2026 paid-channel gap | Line chart shows a gap that month | Deepline — known gap or labeling issue? |
| 3 | MQA/SQA definitions unknown; stages non-monotonic | Showing absolute counts only, no conversion %; stages mislabeled for exec audience | Deepline — confirm definitions and expected behavior |
| 4 | `marketing_lead_source_c` too sparse | No channel-level attribution above opp stage | Deepline — is there a better path? |
| 5 | No segment dimension in funnel model | Can't split Strategic vs HV in funnel view | Deepline — future enhancement request |
| 6 | Unknown `record_type_id` `0124R000001UuQIQAK` | Those opps excluded from dashboard | Salesforce admin — which segment? |
| 7 | No account-level funnel data confirmed | Cohort model rebuild blocked | Deepline — **highest priority** |
