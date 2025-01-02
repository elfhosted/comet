FROM python:3.11-alpine
LABEL name="Comet" \
    description="Stremio's fastest torrent/debrid search add-on." \
    url="https://github.com/g0ldyy/comet"

WORKDIR /app

ARG DATABASE_PATH

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_HOME="/usr/local" \
    FORCE_COLOR=1 \
    TERM=xterm-256color \
    PYTHONPATH=/app

# Fix python-alpine gcc
RUN apk add --no-cache \
    gcc \
    musl-dev \
    libffi-dev \
    make

# Install poetry and gunicorn
RUN pip install poetry

# Copy dependency files
COPY pyproject.toml .

# Install dependencies
RUN poetry install --no-cache --no-root --without dev

# Copy application code
COPY . .

# Default command using gunicorn
ENTRYPOINT ["poetry", "run", "gunicorn", "comet.main:app", \
    "--config", "comet/gunicorn_conf.py", \
    "--worker-class", "uvicorn.workers.UvicornWorker"]