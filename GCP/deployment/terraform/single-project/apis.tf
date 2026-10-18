# ==============================================================================
# Purpose: Enables required Google Cloud APIs for Agent Runtime, Agent Registry,
#          Cloud VPN / PSC networking, and GCP Logging/Monitoring/Trace telemetry.
# Architecture/Context: Provisioned by Terraform in TE/GCP/deployment/terraform/single-project/.
# Dependencies/Side Effects: Enables APIs on var.project_id with disable_on_destroy = false.
# ==============================================================================

locals {
  services = [
    "aiplatform.googleapis.com",
    "agentregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "run.googleapis.com",
    "bigquery.googleapis.com",
    "bigqueryconnection.googleapis.com",
    "storage.googleapis.com",
    "secretmanager.googleapis.com",
    "compute.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
    "serviceusage.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "cloudtrace.googleapis.com",
    "telemetry.googleapis.com",
  ]
}

resource "google_project_service" "services" {
  count              = length(local.services)
  project            = var.project_id
  service            = local.services[count.index]
  disable_on_destroy = false
}

resource "google_project_service_identity" "vertex_sa" {
  provider   = google-beta
  project    = var.project_id
  service    = "aiplatform.googleapis.com"
  depends_on = [google_project_service.services]
}
