# ==============================================================================
# Purpose: Vertex AI Reasoning Engine (Agent Runtime) resource definition for the
#          Telecom ADK Agent.
# Architecture/Context: Deploys the container-based ADK Agent to GCP Agent Runtime
#                       with Cloud Trace, Cloud Logging, GCS prompt/response export,
#                       and Cloud VPN Apigee proxy configuration. Agents deployed to
#                       Agent Runtime are automatically registered in GCP Agent Registry.
# Dependencies/Side Effects: Depends on apis.tf, iam.tf, and storage.tf.
# ==============================================================================

locals {
  dummy_source_b64 = trimspace(file("${path.module}/../shared/dummy_source.b64"))
}

resource "google_vertex_ai_reasoning_engine" "app" {
  display_name = var.project_name
  description  = "Telecom Customer Care ADK Agent on GCP Agent Runtime (connected to On-Premises MCP Server via Apigee over Cloud VPN)"
  region       = var.region
  project      = var.project_id

  spec {
    agent_framework = "google-adk"
    service_account = google_service_account.app_sa.email

    deployment_spec {
      min_instances         = 1
      max_instances         = 10
      container_concurrency = 9

      resource_limits = {
        cpu    = "4"
        memory = "8Gi"
      }

      env {
        name  = "LOGS_BUCKET_NAME"
        value = google_storage_bucket.logs_data_bucket.name
      }

      # GOOGLE_CLOUD_PROJECT is automatically injected by Agent Runtime.
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = var.region
      }

      env {
        name  = "GOOGLE_GENAI_USE_VERTEXAI"
        value = "True"
      }

      env {
        name  = "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY"
        value = "true"
      }

      env {
        name  = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
        value = "EVENT_ONLY"
      }

      env {
        name  = "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"
        value = "false"
      }

      env {
        name  = "OTEL_SEMCONV_STABILITY_OPT_IN"
        value = "gen_ai_latest_experimental"
      }

      env {
        name  = "MCP_SERVER_URL"
        value = var.mcp_server_url
      }

      env {
        name  = "APIGEE_AUTH_MODE"
        value = var.apigee_auth_mode
      }

      env {
        name  = "APIGEE_VERIFY_SSL"
        value = var.apigee_verify_ssl
      }
    }

    source_code_spec {
      inline_source {
        source_archive = local.dummy_source_b64
      }
      image_spec {}
    }
  }

  lifecycle {
    ignore_changes = [
      spec[0].container_spec,
      spec[0].source_code_spec,
      spec[0].deployment_spec,
    ]
  }

  depends_on = [google_project_service.services]
}
