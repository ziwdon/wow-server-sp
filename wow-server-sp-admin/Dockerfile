FROM python:3.12-slim

ARG HOST_UID=1000
ARG HOST_GID=1000

# Docker CE CLI + Compose v2 plugin so the admin app can shell out to
# `docker compose ...` against the bind-mounted host socket.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg | \
        gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
    chmod a+r /etc/apt/keyrings/docker.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release; echo $VERSION_CODENAME) stable" \
        > /etc/apt/sources.list.d/docker.list && \
    apt-get update && apt-get install -y --no-install-recommends \
        docker-ce-cli docker-compose-plugin && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Bake the .conf.dist files for offline key-index parsing.
COPY dist/ /app/dist/

COPY app/ /app/app/

# Match host UID/GID so writes to bind-mounted docker-compose.admin.yml
# keep installer-friendly ownership.
RUN groupadd -g ${HOST_GID} admin && useradd -m -u ${HOST_UID} -g admin admin && \
    chown -R admin:admin /app
USER admin

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
