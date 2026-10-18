# ==============================================================================
# Purpose: Input variables for deploying the Telecom ADK Agent to GCP Agent Runtime
#          with Agent Registry, Cloud VPN Apigee connectivity, and GCP Observability.
# Architecture/Context: Consumed by Terraform resources in TE/GCP/deployment/terraform/single-project/.
# Dependencies/Side Effects: Parameterizes project ID, region, Apigee URL, and IAM roles.
# ==============================================================================

variable "project_name" {
  type        = string
  description = "Project name used as a base for Agent Runtime, Agent Registry, and telemetry resource naming."
  default     = "telecom-agent-hybrid-apigee"
}

variable "project_id" {
  type        = string
  description = "Google Cloud Project ID for Agent Runtime deployment."
}

variable "region" {
  type        = string
  description = "Google Cloud region for Agent Runtime and Agent Registry deployment."
  default     = "us-central1"
}

variable "mcp_server_url" {
  type        = string
  description = "Base URL of the On-Premises Apigee API Proxy reached over Cloud VPN (e.g., https://apigee.onprem.internal/telecom-mcp)."
  default     = "https://apigee.onprem.internal/telecom-mcp"
}

variable "apigee_auth_mode" {
  type        = string
  description = "Flexible Apigee authentication mode: auto, none, api_key, bearer, google_id_token, basic, or custom_header."
  default     = "auto"
}

variable "apigee_verify_ssl" {
  type        = string
  description = "Whether to verify SSL certificates when calling the on-premises Apigee proxy over Cloud VPN ('true' or 'false')."
  default     = "true"
}

variable "telemetry_logs_filter" {
  type        = string
  description = "Log Sink filter for capturing Agent Runtime telemetry logs."
  default     = "resource.type=\"aiplatform.googleapis.com/ReasoningEngine\" OR (labels.service_name=\"telecom-agent\" AND labels.type=\"agent_telemetry\")"
}

variable "feedback_logs_filter" {
  type        = string
  description = "Log Sink filter for capturing user feedback logs."
  default     = "jsonPayload.log_type=\"feedback\" jsonPayload.service_name=\"telecom-agent\""
}

variable "app_sa_roles" {
  description = "List of IAM roles assigned to the Agent Runtime application service account."
  type        = list(string)
  default = [
    "roles/aiplatform.user",
    "roles/logging.logWriter",
    "roles/cloudtrace.agent",
    "roles/monitoring.metricWriter",
    "roles/storage.admin",
    "roles/secretmanager.secretAccessor",
    "roles/serviceusage.serviceUsageConsumer",
  ]
}
