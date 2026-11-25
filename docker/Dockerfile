# Artifacts are compiled and fingerprinted by scripts/build-images.py.
# Three-layer layout separates rarely-changing DuckDB core libraries and
# pre-installed extensions from the frequently updated service binary/config.
ARG BASE_IMAGE=gcr.io/distroless/cc-debian13@sha256:cc66d222f0a7ec996b78a99fe76a969bc80b539a5016ad866c14d1949bc6d0ab
FROM ${BASE_IMAGE}
COPY --chown=65532:65532 layer-duckdb/ /opt/magicdatahub/
COPY --chown=65532:65532 layer-extensions/ /opt/magicdatahub/
COPY --chown=65532:65532 layer-app/ /opt/magicdatahub/
ENV LD_LIBRARY_PATH=/opt/magicdatahub/lib:/opt/magicdatahub/plugins/drivers/oracle:/opt/magicdatahub/plugins/drivers/hana:/opt/magicdatahub/plugins/drivers/db2/clidriver/lib:/opt/magicdatahub/plugins/drivers/db2/clidriver/lib/icc:/opt/magicdatahub/plugins/drivers/dameng
ENV RUST_LOG=info
EXPOSE 5432 8080 1883
WORKDIR /opt/magicdatahub
USER 65532:65532
STOPSIGNAL SIGTERM
ENTRYPOINT ["/opt/magicdatahub/bin/service"]
CMD ["--config", "/opt/magicdatahub/config/host.toml"]
