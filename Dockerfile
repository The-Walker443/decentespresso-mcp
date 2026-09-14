FROM python:3.12-slim

# libtk8.6 ist fuer den Profilparser noetig (SPEC ss7.1).
#
# Das Modul _tkinter IST in python:*-slim einkompiliert - was fehlt, sind die
# Tk-Laufzeitbibliotheken, gegen die es linkt. Ohne sie scheitert schon
# 'import tkinter' mit:
#     ImportError: libtk8.6.so: cannot open shared object file
# libtk8.6 zieht libtcl8.6 und die noetigen X11-Bibliotheken als Abhaengigkeiten
# mit; das Metapaket 'tk' (mit wish und Werkzeugen) braucht es nicht.
#
# Der Smoke-Step im Build-Workflow prueft, dass der Interpreter im fertigen
# Image wirklich laeuft - faellt diese Zeile weg, wird der Build rot statt der
# Container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libtk8.6 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY migrations ./migrations

# Welcher Commit steckt in diesem Image? Die Paketversion sagt, welcher
# Meilenstein gebaut wurde - BUILD_REF sagt, welcher Stand. Zusammen ist beim
# Deployment eindeutig, ob das neue Image laeuft oder noch das alte.
ARG BUILD_REF=""
ENV BUILD_REF=$BUILD_REF

# Non-root (SPEC ss10.4). /data wird vom Host gemountet und muss dieser UID
# gehoeren: chown -R 10001:10001 ./data
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown app:app /data
USER app

VOLUME ["/data"]
EXPOSE 8000

CMD ["decentespresso-mcp"]
