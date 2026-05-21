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

## 3. Lifecycle stage ordering doesn't behave like a funnel

**Table:** `bi-ntop.aero_prod.marketing_lifecycle_funnel`  
**Fields:** `accounts_aware`, `accounts_engaged`, `accounts_mqa`, `accounts_sqa`

In some months, `accounts_mqa` is higher than `accounts_aware` for the same channel and period. A traditional funnel requires each stage to be equal to or smaller than the one above it.

We've worked around this by showing absolute counts only (no conversion rate between stages), which is fine for our purposes. But we want to make sure we're interpreting the model correctly — are these cumulative counts, point-in-time snapshots, or something else? And is the stage ordering expected to be non-monotonic?

---

## 4. `marketing_lead_source_c` is very sparse

**Table:** `bi-ntop.aero_prod.marketing_lifecycle_funnel` (or related model)

We explored using `marketing_lead_source_c` to attribute upper-funnel accounts to channels, but the field is populated on only a small fraction of records. This made channel-level attribution unreliable, so we dropped it.

Is there a more reliable field or join path for attributing an account's first paid-media touch to a specific channel? Even a rough attribution would be useful for the LinkedIn vs. Google funnel breakdown we'd like to show.

---

## 5. No account segment in the lifecycle funnel (Strategic vs. HV split not possible)

**Table:** `bi-ntop.aero_prod.marketing_lifecycle_funnel`

The funnel model rolls up at the channel grain but doesn't carry an account segment field (Strategic vs. High Velocity). This means we can't show a Strategic-only or HV-only funnel view in the dashboard — we can only show all paid channels combined.

We've noted this as a limitation in the dashboard. If Deepline ever adds a segment dimension to the model, this becomes possible without any dashboard rework.

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

## Summary table

| # | Issue | Impact on dashboard | Resolution needed from |
|---|---|---|---|
| 1 | `accounts_opp` 21x inflated vs Salesforce | Not using it; showing SF direct instead | Deepline — what does this field count? |
| 2 | February 2026 paid-channel gap | Line chart shows a gap that month | Deepline — known gap or labeling issue? |
| 3 | Funnel stages non-monotonic | Showing absolute counts only, no conversion % | Deepline — confirm expected behavior |
| 4 | `marketing_lead_source_c` too sparse | No channel-level attribution above opp stage | Deepline — is there a better path? |
| 5 | No segment dimension in funnel model | Can't split Strategic vs HV in funnel view | Deepline — future enhancement request |
| 6 | Unknown `record_type_id` `0124R000001UuQIQAK` | Those opps excluded from dashboard | Salesforce admin — which segment? |
