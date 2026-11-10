# Artifacts are compiled and fingerprinted by scripts/build-images.py.
# The source checkout and native files are verified before this context is created.
ARG BASE_IMAGE=gcr.io/distroless/cc-debian13@sha256:cc66d222f0a7ec996b78a99fe76a969bc80b539a5016ad866c14d1949bc6d0ab
FROM ${BASE_IMAGE}
COPY --chown=65532:65532 app/ /opt/magicdatahub/
ENV LD_LIBRARY_PATH=/opt/magicdatahub/lib
ENV RUST_LOG=info
WORKDIR /opt/magicdatahub
USER 65532:65532
STOPSIGNAL SIGTERM
ENTRYPOINT ["/opt/magicdatahub/bin/service"]
CMD ["--config", "/opt/magicdatahub/config/host.toml"]
