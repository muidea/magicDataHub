# Shared OCI base layer containing mdh-plugin-init and odbc_catalog_scanner
# so db2, hana, and dameng plugin images deduplicate the 45 MiB scanner extension layer.
ARG BASE_IMAGE=gcr.io/distroless/cc-debian13@sha256:cc66d222f0a7ec996b78a99fe76a969bc80b539a5016ad866c14d1949bc6d0ab
FROM ${BASE_IMAGE}
COPY --chown=65532:65532 bin/ /opt/magicdatahub/bin/
COPY --chown=65532:65532 mount-root/ /
COPY --chown=65532:65532 payload-ext/ /payload/
USER 65532:65532
ENTRYPOINT ["/opt/magicdatahub/bin/mdh-plugin-init"]
CMD ["--source", "/payload", "--target", "/plugins"]
