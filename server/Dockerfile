FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 app

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY --chown=app:app entrypoint.sh /entrypoint.sh
COPY --chown=app:app app /app
# WORKDIR /app is root-owned; collectstatic must be able to mkdir staticfiles.
RUN chmod +x /entrypoint.sh \
    && mkdir -p /app/staticfiles \
    && chown -R app:app /app /entrypoint.sh

USER app
EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "config.asgi:application"]
