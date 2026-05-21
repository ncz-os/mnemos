# syntax=docker/dockerfile:1.6
#
# Db2 12.1.5 Early Access Program — local development container build.
#
# REQUIRES USER-SUPPLIED IBM MEDIA + EXPLICIT LICENSE ACCEPTANCE.
#
# The build context MUST contain (NOT shipped in this repo):
#   - The IBM Db2 12.1.5 AESE installation media (server tarball) from
#     your own IBM EAP / Passport Advantage entitlement
#   - Any IBM-supplied license files for that media
#   - A response.rsp configured with terms YOU have accepted
#
# Pass `--build-arg ACCEPT_DB2_LICENSE=YES` to acknowledge that you
# have read and accepted the IBM Db2 EAP terms governing the media
# you are using. The build will fail without this flag.
#
FROM ubuntu:22.04

ARG ACCEPT_DB2_LICENSE=NO
RUN if [ "${ACCEPT_DB2_LICENSE}" != "YES" ]; then \
        echo "ERROR: Build requires explicit acceptance of IBM Db2 license/EAP terms." >&2; \
        echo "       Re-run with: --build-arg ACCEPT_DB2_LICENSE=YES" >&2; \
        echo "       See docs/db2-eap-recipe-2026-05-20.md for full terms." >&2; \
        exit 1; \
    fi

ARG DB2_RESPONSE_FILE=response.rsp
ARG DB2INST1_PASSWORD=changeme_required

ENV DEBIAN_FRONTEND=noninteractive \
    DB2INSTANCE=db2inst1 \
    DB2INST1_PASSWORD=${DB2INST1_PASSWORD} \
    DBNAME=MNEMOS \
    ENABLE_ORACLE_COMPATIBILITY=true \
    PATH=/opt/ibm/db2/V12.1/bin:/opt/ibm/db2/V12.1/adm:/opt/ibm/db2/V12.1/misc:${PATH}

RUN if [ "${DB2INST1_PASSWORD}" = "changeme_required" ]; then \
        echo "ERROR: Build requires --build-arg DB2INST1_PASSWORD=<your-password>" >&2; \
        exit 1; \
    fi

SHELL ["/bin/bash", "-c"]

RUN dpkg --add-architecture i386 \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        binutils \
        ca-certificates \
        file \
        ksh \
        libaio1 \
        libaio1:i386 \
        libcurl4 \
        libcurl4:i386 \
        libnsl2 \
        libnsl2:i386 \
        libnuma1 \
        libnuma1:i386 \
        libpam0g \
        libpam0g:i386 \
        libstdc++6 \
        libstdc++6:i386 \
        libxml2 \
        libxml2:i386 \
        locales \
        passwd \
        procps \
        sudo \
        tzdata \
    && locale-gen en_US.UTF-8 \
    && touch /etc/services \
    && mkdir -p /database/config /database/data \
    && ln -sf /bin/bash /bin/sh \
    && (test -x /usr/bin/ksh93 && ln -sf /usr/bin/ksh93 /bin/ksh || true) \
    && rm -rf /var/lib/apt/lists/*

ENV LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8

COPY ${DB2_RESPONSE_FILE} /tmp/db2-response.rsp
COPY entrypoint.sh /usr/local/bin/db2-eap-entrypoint

RUN --mount=type=bind,source=server,target=/mnt/db2-server,readonly \
    set -eux; \
    test -x /mnt/db2-server/db2setup; \
    /mnt/db2-server/db2setup -r /tmp/db2-response.rsp -l /tmp/db2setup.log -t /tmp/db2setup.trc \
      || { echo "db2setup failed; trace:"; tail -n 200 /tmp/db2setup.log /tmp/db2setup.trc 2>/dev/null || true; exit 1; }; \
    test -x /opt/ibm/db2/V12.1/bin/db2; \
    chmod 0755 /usr/local/bin/db2-eap-entrypoint; \
    rm -f /tmp/db2-response.rsp /tmp/db2setup.trc; \
    rm -rf /tmp/*

COPY server/db2/license/db2adv.lic server/db2/license/db2ese.lic /opt/ibm/db2/V12.1/license/

EXPOSE 50000

HEALTHCHECK --interval=15s --timeout=10s --start-period=120s --retries=20 \
    CMD ["bash", "-c", "su - db2inst1 -c \"db2 connect to ${DBNAME:-MNEMOS} >/dev/null 2>&1\" || exit 1"]

ENTRYPOINT ["/usr/local/bin/db2-eap-entrypoint"]
