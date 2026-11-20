# Self-contained Debian 13 (Trixie) runtime base image with unixODBC and vendor ODBC drivers,
# aligned with federated-query-engine runtime dependencies while preserving UID 65532
# non-root and --read-only root filesystem compatibility for magicDataHub services.
#
# Build using data/runtime-base as the build context (no external registry dependency):
#   docker build -f docker/runtime-odbc.Dockerfile -t mdh-runtime-base:debian13-odbc data/runtime-base
FROM debian:trixie-slim

ARG VENDOR_BUNDLE_SHA256=abf1e81006848298d820f54b8a8478988fd59d974b77bc7adb10d4d98d3e4a5b
ARG DAMENG_ODBC_URL=""

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

COPY odbc-vendor-drivers-x86_64.tar.gz /tmp/vendor/odbc-vendor-drivers-x86_64.tar.gz

RUN set -eu; \
    echo "${VENDOR_BUNDLE_SHA256}  /tmp/vendor/odbc-vendor-drivers-x86_64.tar.gz" | sha256sum -c -; \
    tar -C / -xzf /tmp/vendor/odbc-vendor-drivers-x86_64.tar.gz; \
    rm -rf /tmp/vendor

# Optional Dameng DM8 ODBC override when DAMENG_ODBC_URL is supplied.
RUN set -eu; \
    if [ -n "$DAMENG_ODBC_URL" ]; then \
        apt-get update && \
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends p7zip-full unzip wget && \
        mkdir -p /tmp/dameng/unpack /opt/dameng && cd /tmp/dameng && \
        wget -q "$DAMENG_ODBC_URL" -O dameng-driver.archive && \
        (unzip -q dameng-driver.archive -d unpack || tar -xf dameng-driver.archive -C unpack) && \
        driver_path="$(find unpack -type f -name libdodbc.so -print -quit)" && \
        if [ -z "$driver_path" ]; then \
            iso_path="$(find unpack -type f -name '*.iso' -print -quit)"; \
            test -n "$iso_path"; mkdir -p iso; 7z x "$iso_path" -oiso -y >/dev/null; \
            driver_path="$(find iso -type f -name libdodbc.so -print -quit)"; \
        fi && \
        if [ -z "$driver_path" ]; then \
            installer_path="$(find unpack iso -type f -name DMInstall.bin -print -quit)"; \
            test -n "$installer_path"; payload_start="$(sed -n 's/^skip=\([0-9][0-9]*\)$/\1/p' "$installer_path" | head -1)"; \
            test -n "$payload_start"; mkdir -p payload; \
            tail -n +"$payload_start" "$installer_path" | gzip -cd | \
                tar -xf - -C payload --strip-components=2 \
                    source/bin/libdodbc.so source/bin/libdmdpi.so source/bin/libdmfldr.so \
                    source/bin/dependencies/libcrypto.so source/bin/dependencies/libcrypto.so.3 \
                    source/bin/dependencies/libssl.so source/bin/dependencies/libssl.so.3; \
            driver_path="payload/libdodbc.so"; \
        fi && \
        test -n "$driver_path" && \
        driver_dir="$(dirname "$driver_path")" && \
        for library in libdodbc.so libdmdpi.so libdmfldr.so; do \
            test -f "$driver_dir/$library"; install -m 0755 "$driver_dir/$library" "/opt/dameng/$library"; \
        done && \
        for library in libcrypto.so libcrypto.so.3 libssl.so libssl.so.3; do \
            dependency_path="$(find . -type f -name "$library" -print -quit)"; \
            test -n "$dependency_path"; install -m 0755 "$dependency_path" "/opt/dameng/$library"; \
        done && \
        cd / && rm -rf /tmp/dameng && \
        apt-get purge -y --auto-remove p7zip-full unzip wget && rm -rf /var/lib/apt/lists/*; \
    fi

RUN printf '%s\n' /usr/lib/oracle/23/client64/lib > /etc/ld.so.conf.d/oracle-instantclient.conf && \
    printf '%s\n' /opt/sap/hdbclient > /etc/ld.so.conf.d/hana-odbc.conf && \
    printf '%s\n' /opt/ibm/db2/clidriver/lib > /etc/ld.so.conf.d/db2-cli.conf && \
    printf '%s\n' /opt/dameng > /etc/ld.so.conf.d/dameng-odbc.conf && \
    ldconfig

RUN set -eu; \
    printf '%s\n' \
        '[FreeTDS]' \
        'Description=TDS driver (Sybase/MS SQL)' \
        'Driver=/usr/lib/x86_64-linux-gnu/odbc/libtdsodbc.so' \
        'Setup=/usr/lib/x86_64-linux-gnu/odbc/libtdsS.so' \
        'CPTimeout=' \
        'CPReuse=' \
        'UsageCount=1' \
        '' \
        '[Oracle 23 ODBC driver]' \
        'Description=Oracle ODBC driver for Instant Client' \
        'Driver=/usr/lib/oracle/23/client64/lib/libsqora.so.23.1' \
        'Setup=' \
        'FileUsage=1' \
        'CPTimeout=' \
        'CPReuse=' \
        '' \
        '[HDBODBC]' \
        'Description=SAP HANA ODBC driver' \
        'Driver=/opt/sap/hdbclient/libodbcHDB.so' \
        'Setup=' \
        'FileUsage=1' \
        '' \
        '[IBM DB2 ODBC DRIVER]' \
        'Description=IBM Db2 CLI ODBC driver' \
        'Driver=/opt/ibm/db2/clidriver/lib/libdb2.so' \
        'Setup=' \
        'FileUsage=1' \
        '' \
        '[DM8 ODBC DRIVER]' \
        'Description=DM8 ODBC driver' \
        'Driver=/opt/dameng/libdodbc.so' \
        'Setup=' \
        'FileUsage=1' \
        > /tmp/fedquery-vendor-odbc.ini && \
    for driver in FreeTDS 'Oracle 23 ODBC driver' HDBODBC 'IBM DB2 ODBC DRIVER' 'DM8 ODBC DRIVER'; do \
        for attempt in 1 2 3 4 5; do \
            odbcinst -q -d -n "$driver" >/dev/null 2>&1 || break; \
            odbcinst -u -d -n "$driver" >/dev/null; \
        done; \
        ! odbcinst -q -d -n "$driver" >/dev/null 2>&1; \
    done && \
    cat /tmp/fedquery-vendor-odbc.ini >> /etc/odbcinst.ini && \
    rm -f /tmp/fedquery-vendor-odbc.ini && \
    for driver in FreeTDS 'Oracle 23 ODBC driver' HDBODBC 'IBM DB2 ODBC DRIVER' 'DM8 ODBC DRIVER'; do \
        odbcinst -q -d -n "$driver" >/dev/null; \
    done && \
    for library in \
        /usr/lib/x86_64-linux-gnu/odbc/libtdsodbc.so \
        /usr/lib/oracle/23/client64/lib/libsqora.so.23.1 \
        /opt/sap/hdbclient/libodbcHDB.so \
        /opt/ibm/db2/clidriver/lib/libdb2.so \
        /opt/dameng/libdodbc.so \
        /opt/dameng/libdmdpi.so \
        /opt/dameng/libdmfldr.so \
        /opt/dameng/libcrypto.so \
        /opt/dameng/libcrypto.so.3 \
        /opt/dameng/libssl.so \
        /opt/dameng/libssl.so.3; do \
        test -s "$library"; \
    done && \
    for library in \
        /usr/lib/x86_64-linux-gnu/odbc/libtdsodbc.so \
        /usr/lib/oracle/23/client64/lib/libsqora.so.23.1 \
        /opt/sap/hdbclient/libodbcHDB.so \
        /opt/ibm/db2/clidriver/lib/libdb2.so \
        /opt/dameng/libdodbc.so; do \
        test -z "$(ldd "$library" | awk '/not found/{print}')"; \
    done

RUN groupadd -g 65532 -r nonroot && \
    useradd -u 65532 -g 65532 -r -s /usr/sbin/nologin -d /nonexistent nonroot && \
    mkdir -p /opt/magicdatahub && \
    chown -R 65532:65532 /opt/magicdatahub && \
    chmod a+rX /etc/odbcinst.ini && \
    chmod -R a+rX /usr/lib/oracle /opt/sap /opt/ibm /opt/dameng

USER 65532:65532
