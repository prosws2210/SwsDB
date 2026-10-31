# swsdb: PostgreSQL + Qdrant + FalkorDB behind one API, supervised by s6-overlay.
# Build from the repository root:  docker build -f docker/Dockerfile -t swsdb/swsdb:dev .

ARG PYTHON_IMAGE=python:3.12-slim-trixie
ARG QDRANT_IMAGE=qdrant/qdrant:v1.19.0
ARG FALKORDB_IMAGE=falkordb/falkordb-server:v4.22.3

FROM ${QDRANT_IMAGE} AS qdrant
FROM ${FALKORDB_IMAGE} AS falkordb

# ------------------------------------------------------------------ the server's virtual environment
FROM ${PYTHON_IMAGE} AS server
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
COPY server/requirements.lock /tmp/requirements.lock
RUN python -m venv /opt/swsdb/venv \
 && /opt/swsdb/venv/bin/pip install --require-hashes -r /tmp/requirements.lock
COPY server/ /tmp/server/
COPY sdk/python/ /tmp/sdk/
RUN /opt/swsdb/venv/bin/pip install --no-deps /tmp/server /tmp/sdk \
 && find /opt/swsdb/venv -name '__pycache__' -prune -exec rm -rf {} +
# The default embedding models, baked in so swsdb works offline with no key
# (and the cross-encoder for optional re-ranking, loaded only when a query asks for it)
RUN /opt/swsdb/venv/bin/python -c "from fastembed import TextEmbedding, SparseTextEmbedding; from fastembed.rerank.cross_encoder import TextCrossEncoder; TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='/opt/swsdb/models'); SparseTextEmbedding('Qdrant/bm25', cache_dir='/opt/swsdb/models'); TextCrossEncoder('Xenova/ms-marco-MiniLM-L-6-v2', cache_dir='/opt/swsdb/models')"  && find /opt/swsdb/models -name '*.lock' -delete

# ------------------------------------------------------------------ runtime
FROM ${PYTHON_IMAGE}

ARG TARGETARCH
ARG S6_OVERLAY_VERSION=3.2.3.2
ARG PG_MAJOR=17

LABEL org.opencontainers.image.title="swsdb" \
      org.opencontainers.image.description="One database for AI applications: relational, vector and graph in one container" \
      org.opencontainers.image.licenses="FSL-1.1-ALv2" \
      org.opencontainers.image.vendor="Shakti Swaroop Sahu"

# Runtime libraries the engines link against, PostgreSQL from the PGDG repository (no default cluster: swsdb
# creates its own on the data volume), and curl for the health check.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl xz-utils \
        libunwind8 liblzma5 libssl3t64 libstdc++6 libgomp1 zlib1g libzstd1 libsystemd0 libcap2; \
    install -d /usr/share/postgresql-common/pgdg /etc/postgresql-common/createcluster.d; \
    curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc; \
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt trixie-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list; \
    echo "create_main_cluster = false" > /etc/postgresql-common/createcluster.d/swsdb.conf; \
    apt-get update; \
    apt-get install -y --no-install-recommends "postgresql-${PG_MAJOR}"; \
    rm -rf /var/lib/apt/lists/* /var/lib/postgresql/* /usr/share/doc/* /usr/share/man/*

# The lite profile's extensions (SWSDB_PROFILE=lite): pgvector (PostgreSQL License) and Apache AGE (Apache-2.0)
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends "postgresql-${PG_MAJOR}-pgvector" "postgresql-${PG_MAJOR}-age"; \
    install -d /opt/swsdb/licenses; \
    cp "/usr/share/doc/postgresql-${PG_MAJOR}-pgvector/copyright" /opt/swsdb/licenses/pgvector.copyright; \
    cp "/usr/share/doc/postgresql-${PG_MAJOR}-age/copyright" /opt/swsdb/licenses/apache-age.copyright; \
    rm -rf /var/lib/apt/lists/* /usr/share/doc/* /usr/share/man/*

# s6-overlay, checksum-verified
RUN set -eux; \
    case "${TARGETARCH:-amd64}" in amd64) arch=x86_64 ;; arm64) arch=aarch64 ;; *) echo "unsupported arch ${TARGETARCH}"; exit 1 ;; esac; \
    base="https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}"; \
    cd /tmp; \
    for f in s6-overlay-noarch.tar.xz "s6-overlay-${arch}.tar.xz"; do \
        curl -fsSLO "${base}/${f}"; curl -fsSLO "${base}/${f}.sha256"; sha256sum -c "${f}.sha256"; \
        tar -C / -Jxpf "${f}"; rm "${f}" "${f}.sha256"; \
    done

# The engines, copied from their official images at pinned versions
COPY --from=qdrant /qdrant/qdrant /opt/swsdb/engines/qdrant/qdrant
COPY --from=qdrant /qdrant/qdrant.spdx.json /opt/swsdb/licenses/qdrant.spdx.json
COPY --from=falkordb /usr/local/bin/redis-server /usr/local/bin/redis-cli /opt/swsdb/engines/falkordb/
COPY --from=falkordb /var/lib/falkordb/bin/falkordb.so /opt/swsdb/engines/falkordb/falkordb.so
RUN set -eux; \
    for b in /opt/swsdb/engines/qdrant/qdrant /opt/swsdb/engines/falkordb/redis-server /opt/swsdb/engines/falkordb/falkordb.so; do \
        if ldd "$b" | grep -q "not found"; then ldd "$b"; echo "missing libraries for $b"; exit 1; fi; \
    done; \
    ln -s /opt/swsdb/engines/falkordb/redis-cli /usr/local/bin/redis-cli

COPY --from=server /opt/swsdb/venv /opt/swsdb/venv
COPY --from=server /opt/swsdb/models /opt/swsdb/models
COPY docker/rootfs/ /
COPY LICENSE /opt/swsdb/licenses/swsdb.LICENSE

# Checkouts made on Windows can carry CRLF line endings and lose execute bits; normalise both.
RUN set -eux; \
    find /etc/s6-overlay/s6-rc.d /opt/swsdb/bin -type f -exec sed -i 's/\r$//' {} +; \
    chmod 0755 /opt/swsdb/bin/* /etc/s6-overlay/s6-rc.d/*/run /etc/s6-overlay/s6-rc.d/*/finish; \
    chmod -R a+rX /opt/swsdb/models; \
    useradd --system --uid 10001 --user-group --home-dir /swsdb --no-create-home --shell /usr/sbin/nologin swsdb; \
    install -d -o swsdb -g swsdb -m 0750 /swsdb /swsdb/data; \
    ln -s /opt/swsdb/venv/bin/swsdb-admin /usr/local/bin/swsdb-admin; \
    ln -s /opt/swsdb/venv/bin/swsdb /usr/local/bin/swsdb

ENV PATH="/opt/swsdb/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    SWSDB_PG_MAJOR=${PG_MAJOR} \
    S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    S6_CMD_WAIT_FOR_SERVICES_MAXTIME=0 \
    S6_SERVICES_GRACETIME=30000 \
    S6_KILL_GRACETIME=5000 \
    S6_VERBOSITY=1

VOLUME ["/swsdb/data"]
EXPOSE 6464 5432 6333 6334 6379
HEALTHCHECK --interval=10s --timeout=3s --start-period=90s --retries=3 \
    CMD ["/opt/swsdb/bin/healthcheck"]

ENTRYPOINT ["/init"]
