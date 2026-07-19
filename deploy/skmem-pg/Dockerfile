FROM pgvector/pgvector:pg17
USER root
# --- ParadeDB pg_search (real BM25, @@@ operator) — PG17 bookworm deb ---
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fL -o /tmp/pg_search.deb https://github.com/paradedb/paradedb/releases/download/v0.24.0/postgresql-17-pg-search_0.24.0-1PARADEDB-bookworm_amd64.deb \
 && apt-get install -y /tmp/pg_search.deb && rm -f /tmp/pg_search.deb
# --- Apache AGE (graph in postgres) — build from PG17 branch ---
RUN apt-get install -y --no-install-recommends build-essential git postgresql-server-dev-17 flex bison \
 && git clone --depth 1 -b PG17/v1.7.0-rc0 https://github.com/apache/age.git /tmp/age \
 && cd /tmp/age && make PG_CONFIG=/usr/lib/postgresql/17/bin/pg_config \
 && make PG_CONFIG=/usr/lib/postgresql/17/bin/pg_config install \
 && rm -rf /tmp/age \
 && apt-get purge -y build-essential git postgresql-server-dev-17 flex bison && apt-get autoremove -y \
 && apt-get clean && rm -rf /var/lib/apt/lists/*
USER postgres
