# Inherits shared mdh-plugin-init + odbc_catalog_scanner base layer for DB2, HANA, and DM8 plugins.
ARG BASE_IMAGE=mdh-odbc-plugin-base:local
FROM ${BASE_IMAGE}
COPY --chown=65532:65532 payload-driver/ /payload/
USER 65532:65532
ENTRYPOINT ["/opt/magicdatahub/bin/mdh-plugin-init"]
CMD ["--source", "/payload", "--target", "/plugins"]
