# Lightweight Debian 13 (Trixie) runtime base image with unixODBC and open-source
# system runtime libraries (no commercial vendor ODBC drivers bundled).
# Non-root UID 65532 and --read-only root filesystem compatible; vendor ODBC drivers
# and optional DuckDB extensions are mounted on demand into /opt/magicdatahub/plugins.
ARG CORE_BASE_IMAGE=debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
FROM ${CORE_BASE_IMAGE}

RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates \
        freetds-common \
        libaio1t64 \
        libaudit1 \
        libcap-ng0 \
        libcrypt1 \
        libodbc2 \
        libodbcinst2 \
        libpam0g \
        libstdc++6 \
        libxml2 \
        odbcinst \
        tdsodbc \
        unixodbc && \
    if [ ! -e /usr/lib/x86_64-linux-gnu/libaio.so.1 ] && [ -e /usr/lib/x86_64-linux-gnu/libaio.so.1t64 ]; then \
        ln -s libaio.so.1t64 /usr/lib/x86_64-linux-gnu/libaio.so.1; \
    fi && \
    if [ ! -e /usr/lib/x86_64-linux-gnu/libodbcinst.so ] && [ -e /usr/lib/x86_64-linux-gnu/libodbcinst.so.2 ]; then \
        ln -s libodbcinst.so.2 /usr/lib/x86_64-linux-gnu/libodbcinst.so; \
    fi && \
    rm -rf /var/lib/apt/lists/*

RUN groupadd -g 65532 -r nonroot && \
    useradd -u 65532 -g 65532 -r -s /usr/sbin/nologin -d /nonexistent nonroot && \
    mkdir -p \
        /opt/magicdatahub/bin \
        /opt/magicdatahub/lib \
        /opt/magicdatahub/extensions \
        /opt/magicdatahub/config \
        /opt/magicdatahub/var \
        /opt/magicdatahub/plugins/extensions/manifest.d \
        /opt/magicdatahub/plugins/odbc/odbcinst.d \
        /opt/magicdatahub/plugins/drivers && \
    chown -R 65532:65532 /opt/magicdatahub && \
    chmod a+rX /etc/odbcinst.ini

ENV ODBCSYSINI=/opt/magicdatahub/plugins/odbc \
    DB2_CLI_DRIVER_INSTALL_PATH=/opt/magicdatahub/plugins/drivers/db2/clidriver \
    FEDENGINE_EXTENSION_DIRECTORY=/opt/magicdatahub/extensions \
    FEDENGINE_PLUGIN_EXTENSION_DIRECTORY=/opt/magicdatahub/plugins/extensions \
    LD_LIBRARY_PATH=/opt/magicdatahub/lib:/opt/magicdatahub/plugins/drivers/oracle:/opt/magicdatahub/plugins/drivers/hana:/opt/magicdatahub/plugins/drivers/db2/clidriver/lib:/opt/magicdatahub/plugins/drivers/db2/clidriver/lib/icc:/opt/magicdatahub/plugins/drivers/dameng

USER 65532:65532
