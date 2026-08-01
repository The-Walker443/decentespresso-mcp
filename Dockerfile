FROM python:3.12-slim

# Kein 'tk'-Paket: der TCL-Parser (M2) nutzt tkinter.Tcl(), das ohne X-Display
# laeuft. Erst nachziehen, falls sich das im Container widerlegt (SPEC ss7.1).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY migrations ./migrations

# Non-root (SPEC ss10.4). /data wird vom Host gemountet und muss dieser UID
# gehoeren: chown -R 10001:10001 ./data
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown app:app /data
USER app

VOLUME ["/data"]
EXPOSE 8000

CMD ["visualizer-mcp"]
