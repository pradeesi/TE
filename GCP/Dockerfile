# Purpose: Container image definition packaged by `agents-cli deploy` for Vertex AI Agent Runtime.
# Architecture/Context: Built and executed by Vertex AI Agent Engine (`ReasoningEngine`) to serve the ADK Agent.
# Dependencies/Side Effects: Installs Python dependencies from `pyproject.toml` via `uv` and starts `app.fast_api_app:app`.

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
