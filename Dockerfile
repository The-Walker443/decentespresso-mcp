FROM python:3.12-slim

# libtk8.6 is required by the profile parser (SPEC §7.1).
#
# The _tkinter module IS compiled into python:*-slim - what is missing are the
# Tk runtime libraries it links against. Without them even 'import tkinter'
# fails with:
#     ImportError: libtk8.6.so: cannot open shared object file
# libtk8.6 pulls in libtcl8.6 and the necessary X11 libraries as dependencies;
# the 'tk' metapackage (with wish and its tools) is not needed.
#
# The smoke step in the build workflow checks that the interpreter really runs
# in the finished image - drop this line and the build goes red rather than the
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
# milestone was built - BUILD_REF says which commit. Together they make it
# unambiguous at deployment time whether the new image runs or still the old.
ARG BUILD_REF=""
ENV BUILD_REF=$BUILD_REF

# Non-root (SPEC §10.4). /data is mounted from the host and must belong to this
# gehoeren: chown -R 10001:10001 ./data
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown app:app /data
USER app

VOLUME ["/data"]
EXPOSE 8000

CMD ["decentespresso-mcp"]
