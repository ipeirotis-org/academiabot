#!/bin/bash
set -e

# --- Install gcloud if missing ---
if ! command -v gcloud &> /dev/null; then
  curl -sSL https://sdk.cloud.google.com | bash -s -- --disable-prompts --install-dir=/home/user
  export PATH="/home/user/google-cloud-sdk/bin:$PATH"
fi

# --- Auto-authenticate if credentials exist ---
CONFIG=".cloud-config.json"
if [ ! -f "$CONFIG" ]; then exit 0; fi

PROVIDER=$(jq -r .provider "$CONFIG" 2>/dev/null)
if [ "$PROVIDER" != "gcp" ]; then exit 0; fi

USER_EMAIL=$(git config user.email 2>/dev/null || true)
ENC_FILE=".cloud-credentials.${USER_EMAIL}.enc"
if [ -z "$USER_EMAIL" ] || [ ! -f "$ENC_FILE" ]; then exit 0; fi

KEY="${GCP_CREDENTIALS_KEY:-$CLOUD_CREDENTIALS_KEY}"
if [ -z "$KEY" ]; then exit 0; fi

# One private, uniquely named file per session so overlapping sessions never
# share or overwrite each other's key. Removed by cloud-cleanup.sh at SessionEnd.
SA_KEY_FILE="$(mktemp "${TMPDIR:-/tmp}/gcp-sa-XXXXXXXX.json")" || exit 0
echo "$KEY" | openssl enc -d -aes-256-cbc -pbkdf2 \
  -pass stdin -in "$ENC_FILE" -out "$SA_KEY_FILE" 2>/dev/null || exit 0
chmod 600 "$SA_KEY_FILE"

gcloud auth activate-service-account --key-file="$SA_KEY_FILE" 2>/dev/null
gcloud config set project "$(jq -r .project_id "$CONFIG")" 2>/dev/null

# Keep the key file for the session so Google client libraries (BigQuery, GCS,
# Secret Manager) can authenticate. The bq CLI does not work behind the session
# proxy, but the Python clients do when these variables are set.
if [ -n "$CLAUDE_ENV_FILE" ]; then
  {
    echo "export GOOGLE_APPLICATION_CREDENTIALS='$SA_KEY_FILE'"
    echo "export GOOGLE_CLOUD_PROJECT='$(jq -r .project_id "$CONFIG")'"
    [ -f /root/.ccr/ca-bundle.crt ] && echo "export REQUESTS_CA_BUNDLE=/root/.ccr/ca-bundle.crt"
  } >> "$CLAUDE_ENV_FILE"
fi

echo "GCP credentials activated for $USER_EMAIL"
