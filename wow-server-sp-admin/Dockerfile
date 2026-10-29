FROM python:3.12-slim

ARG HOST_UID=1000
ARG HOST_GID=1000

# Docker CE CLI + Compose v2 plugin so the admin app can shell out to
# `docker compose ...` against the bind-mounted host socket.
# gnupg + pass + clevis + clevis-tpm2 (pulls tpm2-tools) are the Cloud backup
# vault: the Proton Drive session lives in pass/GPG and the GPG passphrase is
# sealed to the host TPM by clevis.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git util-linux pass clevis clevis-tpm2 && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg | \
        gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
    chmod a+r /etc/apt/keyrings/docker.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release; echo $VERSION_CODENAME) stable" \
        > /etc/apt/sources.list.d/docker.list && \
    apt-get update && apt-get install -y --no-install-recommends \
        docker-ce-cli docker-compose-plugin && \
    rm -rf /var/lib/apt/lists/*

# Proton Drive CLI for the Cloud backup feature, pinned and checksum-verified:
# a mismatch fails the build (&&-chained), so nothing unverified is installed.
# To bump it, edit BOTH args together from
# https://proton.me/download/drive/cli/version.json (linux x64 entry).
ARG PROTON_DRIVE_CLI_VERSION=0.9.0
ARG PROTON_DRIVE_CLI_SHA512=3533025ba69ae112b64e3e01fbcc1ad0688136a4043f6cf6a72886967d85fdcd9ec235479c2e113171614be5225bfba93427509a05ca5aa6071d924fa7e91ca8
RUN curl -fsSL "https://proton.me/download/drive/cli/${PROTON_DRIVE_CLI_VERSION}/linux-x64/proton-drive" \
        -o /usr/local/bin/proton-drive && \
    echo "${PROTON_DRIVE_CLI_SHA512}  /usr/local/bin/proton-drive" | sha512sum -c - && \
    chmod 0755 /usr/local/bin/proton-drive

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Bake the .conf.dist files for offline key-index parsing.
COPY dist/ /app/dist/

COPY app/ /app/app/

# Canonical backup script — single source of truth shared with the host cron.
# Staged into the build context as ./backup.sh by the install/redeploy scripts.
COPY backup.sh /app/scripts/backup.sh
RUN chmod +x /app/scripts/backup.sh

# Match host UID/GID so writes to bind-mounted docker-compose.admin.yml
# keep installer-friendly ownership.
RUN groupadd -g ${HOST_GID} admin && useradd -m -u ${HOST_UID} -g admin admin && \
    chown -R admin:admin /app
USER admin

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
