# ==============================================================================
# Purpose: GCP Agent Runtime (Vertex AI Reasoning Engine) Cloud Logging Metrics,
#          BigQuery Reasoning Engine Log Sink, and Cloud Monitoring Dashboard.
# Architecture/Context: Provides dedicated GCP-native observability for the Telecom
#                       ADK Agent running on Agent Runtime and calling the On-Premises
#                       Apigee API Proxy over Cloud VPN.
# Dependencies/Side Effects: Creates google_logging_metric resources, a ReasoningEngine
#                            BigQuery sink, and a google_monitoring_dashboard in GCP.
# ==============================================================================

# 1. Log Sink routing all Vertex AI Reasoning Engine (Agent Runtime) stdout/stderr logs to BigQuery
resource "google_logging_project_sink" "agent_runtime_logs_to_bq" {
  name        = "${var.project_name}-reasoning-engine-logs"
  project     = var.project_id
  destination = "bigquery.googleapis.com/projects/${var.project_id}/datasets/${google_bigquery_dataset.telemetry_dataset.dataset_id}"
  filter      = "resource.type=\"aiplatform.googleapis.com/ReasoningEngine\""

  unique_writer_identity = true

  bigquery_options {
    use_partitioned_tables = true
  }

  depends_on = [google_bigquery_dataset.telemetry_dataset]
}

resource "google_bigquery_dataset_iam_member" "agent_runtime_logs_bq_writer" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.telemetry_dataset.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = google_logging_project_sink.agent_runtime_logs_to_bq.writer_identity
}

# 2. Log-Based Metric: Total MCP Tool Calls Dispatched from Agent Runtime to Apigee over Cloud VPN
resource "google_logging_metric" "mcp_tool_calls_total" {
  project     = var.project_id
  name        = "${replace(var.project_name, "-", "_")}_mcp_tool_calls_total"
  description = "Count of MCP tool calls dispatched from GCP Agent Runtime to the On-Premises Apigee proxy over Cloud VPN"
  filter      = "resource.type=\"aiplatform.googleapis.com/ReasoningEngine\" AND jsonPayload.event=\"mcp_tool_call_completed\""

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key         = "tool_name"
      value_type  = "STRING"
      description = "Executed Telecom MCP tool name"
    }
    labels {
      key         = "auth_mode"
      value_type  = "STRING"
      description = "Apigee authentication mode used"
    }
  }

  label_extractors = {
    "tool_name" = "EXTRACT(jsonPayload.tool_name)"
    "auth_mode" = "EXTRACT(jsonPayload.auth_mode)"
  }

  depends_on = [google_project_service.services]
}

# 3. Log-Based Metric: Total MCP Tool Call Errors / VPN Connectivity Failures
resource "google_logging_metric" "mcp_tool_errors_total" {
  project     = var.project_id
  name        = "${replace(var.project_name, "-", "_")}_mcp_tool_errors_total"
  description = "Count of failed MCP tool calls (HTTP, JSON-RPC, or Cloud VPN connectivity errors) on GCP Agent Runtime"
  filter      = "resource.type=\"aiplatform.googleapis.com/ReasoningEngine\" AND (jsonPayload.event=\"mcp_tool_call_http_error\" OR jsonPayload.event=\"mcp_tool_call_rpc_error\" OR jsonPayload.event=\"mcp_tool_call_connect_error\" OR jsonPayload.event=\"mcp_tool_call_unexpected_error\")"

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key         = "tool_name"
      value_type  = "STRING"
      description = "Telecom MCP tool name that experienced an error"
    }
    labels {
      key         = "event"
      value_type  = "STRING"
      description = "Failure category (HTTP, RPC, Connect, Unexpected)"
    }
  }

  label_extractors = {
    "tool_name" = "EXTRACT(jsonPayload.tool_name)"
    "event"     = "EXTRACT(jsonPayload.event)"
  }

  depends_on = [google_project_service.services]
}

# 4. Log-Based Metric: Apigee Model Armor Security Policy Blocks observed by Agent Runtime
resource "google_logging_metric" "mcp_model_armor_blocks_total" {
  project     = var.project_id
  name        = "${replace(var.project_name, "-", "_")}_model_armor_blocks_total"
  description = "Count of MCP responses blocked by Apigee Model Armor (-32001) observed by GCP Agent Runtime"
  filter      = "resource.type=\"aiplatform.googleapis.com/ReasoningEngine\" AND jsonPayload.event=\"mcp_tool_blocked_by_model_armor\""

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key         = "tool_name"
      value_type  = "STRING"
      description = "Blocked Telecom MCP tool name"
    }
  }

  label_extractors = {
    "tool_name" = "EXTRACT(jsonPayload.tool_name)"
  }

  depends_on = [google_project_service.services]
}

# 5. Cloud Monitoring Dashboard for GCP Agent Runtime & Apigee Cloud VPN Telemetry
resource "google_monitoring_dashboard" "agent_runtime_dashboard" {
  project = var.project_id
  dashboard_json = jsonencode({
    displayName = "Telecom ADK Agent Runtime & Apigee VPN Observability (${var.project_name})"
    gridLayout = {
      columns = "2"
      widgets = [
        {
          title = "Agent Runtime — Successful MCP Tool Calls over Cloud VPN (by Tool)"
          xyChart = {
            dataSets = [
              {
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.mcp_tool_calls_total.name}\" resource.type=\"aiplatform.googleapis.com/ReasoningEngine\""
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_RATE"
                      crossSeriesReducer = "REDUCE_SUM"
                      groupByFields      = ["metric.label.tool_name"]
                    }
                  }
                }
                plotType = "LINE"
              }
            ]
          }
        },
        {
          title = "Agent Runtime — MCP Tool Call & Cloud VPN Errors (by Category)"
          xyChart = {
            dataSets = [
              {
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.mcp_tool_errors_total.name}\" resource.type=\"aiplatform.googleapis.com/ReasoningEngine\""
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_RATE"
                      crossSeriesReducer = "REDUCE_SUM"
                      groupByFields      = ["metric.label.event", "metric.label.tool_name"]
                    }
                  }
                }
                plotType = "STACKED_BAR"
              }
            ]
          }
        },
        {
          title = "Agent Runtime — Apigee Model Armor Blocked Tool Responses"
          xyChart = {
            dataSets = [
              {
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.mcp_model_armor_blocks_total.name}\" resource.type=\"aiplatform.googleapis.com/ReasoningEngine\""
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_SUM"
                      crossSeriesReducer = "REDUCE_SUM"
                      groupByFields      = ["metric.label.tool_name"]
                    }
                  }
                }
                plotType = "STACKED_BAR"
              }
            ]
          }
        },
        {
          title = "Vertex AI Reasoning Engine — Live Execution & Tool Logs"
          logsPanel = {
            filter        = "resource.type=\"aiplatform.googleapis.com/ReasoningEngine\""
            resourceNames = ["projects/${var.project_id}"]
          }
        }
      ]
    }
  })

  depends_on = [
    google_project_service.services,
    google_logging_metric.mcp_tool_calls_total,
    google_logging_metric.mcp_tool_errors_total,
    google_logging_metric.mcp_model_armor_blocks_total,
  ]
}
