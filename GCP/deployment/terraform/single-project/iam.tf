# DEMO SOFTWARE DISCLAIMER:
# This code is provided strictly as a demonstration and reference implementation.
# It comes with NO WARRANTY, NO GUARANTEE, and NO SUPPORT of any kind, either expressed or implied.
# Use and deployment in any environment is entirely at your own discretion and risk.

locals {
  project_ids = {
    default = var.project_id
  }
}


# Get the project number
data "google_project" "project" {
  project_id = var.project_id
}

# Grant Storage Object Creator role to default compute service account
resource "google_project_iam_member" "default_compute_sa_storage_object_creator" {
  project    = var.project_id
  role       = "roles/cloudbuild.builds.builder"
  member     = "serviceAccount:${data.google_project.project.number}-compute@developer.gserviceaccount.com"
  depends_on = [resource.google_project_service.services]
}

# Agent service account
resource "google_service_account" "app_sa" {
  account_id   = "${var.project_name}-app"
  display_name = "${var.project_name} Agent Service Account"
  project      = var.project_id
  depends_on   = [resource.google_project_service.services]
}

# Grant application SA the required permissions to run the application
resource "google_project_iam_member" "app_sa_roles" {
  for_each = {
    for pair in setproduct(keys(local.project_ids), var.app_sa_roles) :
    join(",", pair) => {
      project = local.project_ids[pair[0]]
      role    = pair[1]
    }
  }

  project    = each.value.project
  role       = each.value.role
  member     = "serviceAccount:${google_service_account.app_sa.email}"
  depends_on = [resource.google_project_service.services]
}


# Grant required permissions to Vertex AI service account for Agent Runtime
resource "google_project_iam_member" "vertex_ai_sa_permissions" {
  for_each = {
    for pair in setproduct(keys(local.project_ids), var.app_sa_roles) :
    join(",", pair) => pair[1]
  }

  project = var.project_id
  role    = each.value
  member  = google_project_service_identity.vertex_sa.member
  depends_on = [resource.google_project_service.services]
}

