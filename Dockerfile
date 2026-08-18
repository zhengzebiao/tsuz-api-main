FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1     PYTHONUNBUFFERED=1     PDM_VENV_IN_PROJECT=1     PATH="/app/.venv/bin:$PATH"

RUN pip install --no-cache-dir pdm

COPY pyproject.toml pdm.lock ./
RUN pdm install --prod --no-self --frozen-lockfile

COPY . .

EXPOSE 8000
CMD ["sh", "-c", "gunicorn app.main:app -k uvicorn_worker.UvicornWorker --bind 0.0.0.0:8000 --workers ${WEB_CONCURRENCY:-4} --timeout ${GUNICORN_TIMEOUT:-60} --graceful-timeout ${GUNICORN_GRACEFUL_TIMEOUT:-30}"]
