# Runtime only: the source is mounted at /app by the deployment's compose file, so a code
# change needs a container restart, not a rebuild. Rebuild when requirements.txt changes.
FROM python:3.12.8-slim-bookworm

ARG UID=1000
ARG GID=1000
RUN apt-get update \
 && apt-get install -y --no-install-recommends rsync openssh-client \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g ${GID} jellymirror \
 && useradd -u ${UID} -g ${GID} -m -s /bin/sh jellymirror

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

ENV JM_DATA=/data PYTHONUNBUFFERED=1
WORKDIR /app
USER ${UID}:${GID}
EXPOSE 8097
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8097/health')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8097"]
