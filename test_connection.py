from google.cloud import bigquery

client = bigquery.Client(project="bi-ntop")

query = """
SELECT
  platform,
  ROUND(SUM(spend), 2) AS total_spend
FROM `bi-ntop.aero_prod_ad_reporting.ad_reporting__account_report`
WHERE date_day >= '2025-01-01'
GROUP BY platform
ORDER BY total_spend DESC
"""

result = client.query(query).to_dataframe()
print(result)