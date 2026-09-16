FROM python:3.12-slim

# No apt layer. The profile parser used to need libtk8.6 for tkinter.Tcl(),
# because the previous source served profiles as TCL. Decaid ships the profile
# as JSON with the shot, so the Tcl interpreter and its X11 dependencies are
# gone, and with them an apt layer. How much that saves is not measured here -
# the CI build will show it.

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# LICENSE travels with pyproject.toml: it is named there in `license-files`,
# so the build fails without it - and a GPL work distributed as an image should
# carry its licence anyway.
COPY pyproject.toml LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY migrations ./migrations

# Which commit is in this image? The package version says what was built and
# BUILD_REF says which commit. Together they make it unambiguous at deployment
# time whether the new image is running or still the old one.
ARG BUILD_REF=""
ENV BUILD_REF=$BUILD_REF

# Non-root (SPEC §12). /data is mounted from the host and must belong to this
# user: chown -R 10001:10001 ./data
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown app:app /data
USER app

VOLUME ["/data"]
EXPOSE 8000

CMD ["decentespresso-mcp"]
