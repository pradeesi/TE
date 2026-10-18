# DEMO SOFTWARE DISCLAIMER:
# This code is provided strictly as a demonstration and reference implementation.
# It comes with NO WARRANTY, NO GUARANTEE, and NO SUPPORT of any kind, either expressed or implied.
# Use and deployment in any environment is entirely at your own discretion and risk.

output "agent_runtime_resource_name" {
  description = "Agent Runtime resource name"
  value       = google_vertex_ai_reasoning_engine.app.name
}
