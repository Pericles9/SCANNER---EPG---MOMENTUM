FROM python:3.11-slim

WORKDIR /app

# postgresql-client provides psql for the DB migration step at container startup
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies in a cached layer
COPY live/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy only the packages needed at runtime
COPY backtest/ backtest/
COPY live/ live/

ENV PYTHONPATH=/app:/app/backtest

CMD ["sh", "-c", "python -c 'from backtest.runner import _hawkes_replay_with_refit; print(\"Numba warmed\")' && exec python -m live.main"]
