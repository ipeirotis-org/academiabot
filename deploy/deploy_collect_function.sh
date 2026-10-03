#!/bin/bash
# Deploy the collection Cloud Function (gen 2) and a Cloud Scheduler job that calls it
# every 30 minutes. Run from the repository root with gcloud authenticated to project
# wikidata-academia.
#
#   bash deploy/deploy_collect_function.sh            # deploy function; scheduler job left PAUSED
#   bash deploy/deploy_collect_function.sh --resume   # also un-pause the scheduler job
# A redeploy always pauses an existing job, whether or not it was running, so that
# nothing spends LLM credit on the new run id until a person resumes it.
#
# Timing: Cloud Scheduler cancels an HTTP call after 30 minutes at most, so each slice
# gets a 25 minute budget (time_budget_s=1500) and the function timeout is 30 minutes.
# A university starts only if 3 minutes (reserve_s=180), or the longest university so
# far, are left in the budget. Two slices per hour is about 40 universities per hour.
#
# Prerequisites (one time, by a project owner):
#   gcloud services enable cloudfunctions.googleapis.com run.googleapis.com cloudbuild.googleapis.com \
#       artifactregistry.googleapis.com cloudscheduler.googleapis.com eventarc.googleapis.com \
#       cloudresourcemanager.googleapis.com --project=wikidata-academia
set -euo pipefail

PROJECT=wikidata-academia
REGION=${REGION:-us-east1}
FUNCTION=academiabot-collect
JOB=academiabot-collect-slice
LEGACY_JOB=academiabot-collect-hourly
SA=claude-agent@wikidata-academia.iam.gserviceaccount.com
# The run id is stable across redeploys on purpose: a date-based default would start
# a new run (and a new log) every time the function is redeployed on a later day.
RUN_ID=${RUN_ID:-us-tier1}
LIST_OBJECT=${LIST_OBJECT:-universities_us_tier1.json}   # from scripts/filter_universities.py --upload
MAX_PER_SLICE=${MAX_PER_SLICE:-150}   # a 25 min slice does about 100 at 4 a minute; the cap must not be the limit
SCHEDULE="7,37 * * * *"

# The deployed code must be the committed code, so that GIT_COMMIT in every run record
# names exactly what ran. Set ALLOW_DIRTY=1 to deploy anyway: the uncommitted changes
# (tracked and untracked) are then saved as a patch in the bucket under deploys/, and
# the commit is recorded as <sha>-dirty-<patch hash>, so the exact source of any run
# record can still be rebuilt with "git checkout <sha> && git apply <patch>".
GIT_COMMIT=$(git rev-parse --short HEAD)
if [ -n "$(git status --porcelain -- wikidata_discover deploy)" ]; then
  if [ "${ALLOW_DIRTY:-}" = "1" ]; then
    PATCH=$(mktemp)
    git diff --binary HEAD -- wikidata_discover deploy > "$PATCH"
    git ls-files -z --others --exclude-standard -- wikidata_discover deploy | while IFS= read -r -d '' f; do
      git diff --binary --no-index -- /dev/null "$f" >> "$PATCH" || true   # exit 1 means "differs"
    done   # -z and -d '': a path with whitespace stays one path
    GIT_COMMIT="${GIT_COMMIT}-dirty-$(sha256sum "$PATCH" | cut -c1-12)"
    gcloud storage cp "$PATCH" "gs://academiabot/deploys/${GIT_COMMIT}.patch" --project="$PROJECT" >/dev/null
    rm -f "$PATCH"
    echo "WARNING: uncommitted changes are being deployed; recorded as $GIT_COMMIT, patch saved to gs://academiabot/deploys/${GIT_COMMIT}.patch."
  else
    echo "Refusing to deploy: uncommitted changes in wikidata_discover/ or deploy/. Commit first, or set ALLOW_DIRTY=1." >&2
    exit 1
  fi
fi

# Source directory for the build: the package (minus generated data) and a root main.py.
SRC=$(mktemp -d)
cp -r wikidata_discover "$SRC/"
rm -rf "$SRC"/wikidata_discover/results/cache "$SRC"/wikidata_discover/results/runs "$SRC"/wikidata_discover/results/reports
# Local secrets and caches never ship: a developer's .env would otherwise be read
# by the function before Secret Manager and put unrecorded keys into cloud runs.
find "$SRC" \( -name '.env' -o -name '.env.*' -o -name '*.enc' -o -name '*.pem' -o -name '*credentials*' \
  -o -name '__pycache__' -o -name '.pytest_cache' \) -prune -exec rm -rf {} +
if find "$SRC" \( -name '.env' -o -name '.env.*' -o -name '*.enc' \) | grep -q .; then
  echo "Refusing to deploy: a secrets file is still in the staging tree." >&2; exit 1
fi
cp wikidata_discover/cloud/requirements.txt "$SRC/requirements.txt"
printf 'from wikidata_discover.cloud.collect_function import collect  # noqa: F401\n' > "$SRC/main.py"

# Pause the job before touching the function, so no scheduled tick runs during a slow
# or failed deployment. The job is left paused at the end unless --resume is given.
for J in "$JOB" "$LEGACY_JOB"; do
  STATE=$(gcloud scheduler jobs describe "$J" --project="$PROJECT" --location="$REGION" --format="value(state)" 2>/dev/null || true)
  if [ "$STATE" = "ENABLED" ]; then
    gcloud scheduler jobs pause "$J" --project="$PROJECT" --location="$REGION" >/dev/null
    echo "Scheduler job $J paused for the deployment."
  elif [ -n "$STATE" ]; then
    echo "Scheduler job $J is $STATE; nothing to pause."
  fi
done

# The invoker policy (authenticated callers only) is set when the function is first
# created. On a redeploy the flag is omitted: the policy is kept, and setting it again
# needs run.services.setIamPolicy, which the deploying service account does not have.
AUTH_FLAG=--no-allow-unauthenticated
if gcloud functions describe "$FUNCTION" --project="$PROJECT" --region="$REGION" --gen2 >/dev/null 2>&1; then
  AUTH_FLAG=""
fi
gcloud functions deploy "$FUNCTION" \
  --project="$PROJECT" --region="$REGION" --gen2 \
  --runtime=python311 --source="$SRC" --entry-point=collect \
  --trigger-http $AUTH_FLAG \
  --service-account="$SA" \
  --memory=1Gi --timeout=1800s --max-instances=1 --concurrency=1 \
  --update-env-vars="GIT_COMMIT=$GIT_COMMIT"   # keeps any other variables set on the function

URL=$(gcloud functions describe "$FUNCTION" --project="$PROJECT" --region="$REGION" --gen2 --format="value(serviceConfig.uri)")
echo "Function URL: $URL"

# The scheduler calls the function as $SA with an OIDC token. A gen 2 function needs
# run.routes.invoke on its Cloud Run service; roles/run.developer (which $SA holds)
# includes it, and roles/run.invoker is the narrow alternative. Granting needs
# run.services.setIamPolicy, which $SA lacks, so the binding is attempted but not
# required; the call below proves the path works either way.
gcloud run services add-iam-policy-binding "$FUNCTION" --project="$PROJECT" --region="$REGION" \
  --member="serviceAccount:$SA" --role="roles/run.invoker" >/dev/null 2>&1 \
  && echo "Granted roles/run.invoker to $SA." \
  || echo "Could not grant roles/run.invoker (no permission); relying on the roles $SA already has."

# Prove the scheduler's call path (same account, same token type, same URL) with a
# request that does no work: an explicit empty QID list. Costs nothing. The token
# must belong to $SA: minted directly when gcloud runs as $SA, otherwise by
# impersonation (needs roles/iam.serviceAccountTokenCreator on $SA).
ACTIVE=$(gcloud config get-value account 2>/dev/null || true)
if [ "$ACTIVE" = "$SA" ]; then
  TOKEN=$(gcloud auth print-identity-token --audiences="$URL/" 2>/dev/null || true)
else
  TOKEN=$(gcloud auth print-identity-token --impersonate-service-account="$SA" --audiences="$URL/" 2>/dev/null || true)
fi
if [ -n "$TOKEN" ]; then
  CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$URL/" -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" -d '{"run_id": "deploy-check", "qids": []}')
  if [ "$CODE" = "200" ]; then
    echo "Invoke check passed: $SA can call the function (HTTP 200)."
  else
    echo "Invoke check FAILED (HTTP $CODE): the scheduler will get the same answer. Grant roles/run.invoker to $SA on service $FUNCTION." >&2
    exit 1
  fi
elif [ "${SKIP_INVOKE_CHECK:-}" = "1" ]; then
  echo "Could not mint an identity token for $SA; invoke check skipped (SKIP_INVOKE_CHECK=1)."
else
  echo "Could not mint an identity token for $SA (active account: $ACTIVE). Run this script as $SA, grant yourself roles/iam.serviceAccountTokenCreator on it, or set SKIP_INVOKE_CHECK=1 to deploy unverified." >&2
  exit 1
fi

# The earlier hourly job, if still present, is removed so only one job can run.
if gcloud scheduler jobs describe "$LEGACY_JOB" --project="$PROJECT" --location="$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs delete "$LEGACY_JOB" --project="$PROJECT" --location="$REGION" --quiet
  echo "Removed old job $LEGACY_JOB."
fi

BODY="{\"run_id\": \"$RUN_ID\", \"list_object\": \"$LIST_OBJECT\", \"max_universities\": $MAX_PER_SLICE, \"time_budget_s\": 1500, \"reserve_s\": 180}"
if gcloud scheduler jobs describe "$JOB" --project="$PROJECT" --location="$REGION" >/dev/null 2>&1; then
  # Paused above if it was running: an update changes the run id in the request body,
  # and the job must not start spending on the new run without an explicit --resume.
  gcloud scheduler jobs update http "$JOB" --project="$PROJECT" --location="$REGION" \
    --schedule="$SCHEDULE" --uri="$URL" --http-method=POST --message-body="$BODY" \
    --update-headers="Content-Type=application/json" --oidc-service-account-email="$SA" --attempt-deadline=30m
  echo "Scheduler job $JOB updated and PAUSED."
else
  gcloud scheduler jobs create http "$JOB" --project="$PROJECT" --location="$REGION" \
    --schedule="$SCHEDULE" --uri="$URL" --http-method=POST --message-body="$BODY" \
    --headers="Content-Type=application/json" --oidc-service-account-email="$SA" --attempt-deadline=30m
  gcloud scheduler jobs pause "$JOB" --project="$PROJECT" --location="$REGION"
  echo "Scheduler job $JOB created PAUSED."
fi

if [ "${1:-}" = "--resume" ]; then
  gcloud scheduler jobs resume "$JOB" --project="$PROJECT" --location="$REGION"
  echo "Scheduler job $JOB resumed: it calls the function at 7 and 37 minutes past every hour."
fi
rm -rf "$SRC"
