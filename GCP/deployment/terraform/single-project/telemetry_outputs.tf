# DEMO SOFTWARE DISCLAIMER:
# This code is provided strictly as a demonstration and reference implementation.
# It comes with NO WARRANTY, NO GUARANTEE, and NO SUPPORT of any kind, either expressed or implied.
# Use and deployment in any environment is entirely at your own discretion and risk.

output "telemetry_dataset_id" {
  description = "BigQuery dataset ID for telemetry data"
  value       = google_bigquery_dataset.telemetry_dataset.dataset_id
}

output "telemetry_bigquery_connection_id" {
  description = "BigQuery connection ID for telemetry GCS access"
  value       = google_bigquery_connection.genai_telemetry_connection.connection_id
}
