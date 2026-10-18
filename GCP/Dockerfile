# Purpose: Container definition for Telecom Customer Care ADK Agent.
# Architecture/Context: Runs the FastAPI ADK Runner on Cloud Run.
#
# DEMO SOFTWARE DISCLAIMER:
# This code is provided strictly as a demonstration and reference implementation.
# It comes with NO WARRANTY, NO GUARANTEE, and NO SUPPORT of any kind, either expressed or implied.
# Use and deployment in any environment is entirely at your own discretion and risk.

FROM python:3.12-slim

RUN pip install --no-cache-dir uv==0.8.13

WORKDIR /code

COPY ./pyproject.toml ./

COPY ./app ./app

RUN uv sync --no-cache

ARG COMMIT_SHA=""
ENV COMMIT_SHA=${COMMIT_SHA}

ARG AGENT_VERSION=0.0.0
ENV AGENT_VERSION=${AGENT_VERSION}

EXPOSE 8080

CMD ["uv", "run", "uvicorn", "app.fast_api_app:app", "--host", "0.0.0.0", "--port", "8080"]
