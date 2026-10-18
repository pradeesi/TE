# DEMO SOFTWARE DISCLAIMER:
# This code is provided strictly as a demonstration and reference implementation.
# It comes with NO WARRANTY, NO GUARANTEE, and NO SUPPORT of any kind, either expressed or implied.
# Use and deployment in any environment is entirely at your own discretion and risk.

provider "google" {
  project               = var.project_id
  region                = var.region
  user_project_override = true
}

resource "google_storage_bucket" "logs_data_bucket" {
  name                        = "${var.project_id}-${var.project_name}-logs"
  location                    = var.region
  project                     = var.project_id
  uniform_bucket_level_access = true

  depends_on = [resource.google_project_service.services]
}
