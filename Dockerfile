FROM python:3.13-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
      dar \
      par2 \
      genisoimage \
      growisofs \
      dvd+rw-tools \
      udisks2 \
      mount \
      eject \
      util-linux \
      lsof \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ARG BD_ARCHIVE_VERSION=0.0.0+docker-local
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${BD_ARCHIVE_VERSION}

WORKDIR /src
COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir .

WORKDIR /data
ENTRYPOINT ["bd-archive"]
CMD ["--help"]
