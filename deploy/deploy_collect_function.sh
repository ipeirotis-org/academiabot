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
# A university starts only if 7 minutes (reserve_s=420), or the longest university so
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
RUN_ID=${RUN_ID:-cloud-$(date -u +%Y-%m-%d)}
MAX_PER_SLICE=${MAX_PER_SLICE:-60}
SCHEDULE="7,37 * * * *"

# Source directory for the build: the package (minus generated data) and a root main.py.
SRC=$(mktemp -d)
cp -r wikidata_discover "$SRC/"
rm -rf "$SRC"/wikidata_discover/results/cache "$SRC"/wikidata_discover/results/runs "$SRC"/wikidata_discover/results/reports
cp wikidata_discover/cloud/requirements.txt "$SRC/requirements.txt"
printf 'from wikidata_discover.cloud.collect_function import collect  # noqa: F401\n' > "$SRC/main.py"

# Pause the job before touching the function, so no scheduled tick runs during a slow
# or failed deployment. The job is left paused at the end unless --resume is given.
for J in "$JOB" "$LEGACY_JOB"; do
  if gcloud scheduler jobs describe "$J" --project="$PROJECT" --location="$REGION" >/dev/null 2>&1; then
    gcloud scheduler jobs pause "$J" --project="$PROJECT" --location="$REGION" >/dev/null
    echo "Scheduler job $J paused for the deployment."
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
  --update-env-vars="GIT_COMMIT=$(git rev-parse --short HEAD)"   # keeps any other variables set on the function

URL=$(gcloud functions describe "$FUNCTION" --project="$PROJECT" --region="$REGION" --gen2 --format="value(serviceConfig.uri)")
echo "Function URL: $URL"

# The earlier hourly job, if still present, is removed so only one job can run.
if gcloud scheduler jobs describe "$LEGACY_JOB" --project="$PROJECT" --location="$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs delete "$LEGACY_JOB" --project="$PROJECT" --location="$REGION" --quiet
  echo "Removed old job $LEGACY_JOB."
fi

BODY="{\"run_id\": \"$RUN_ID\", \"max_universities\": $MAX_PER_SLICE, \"time_budget_s\": 1500, \"reserve_s\": 420}"
if gcloud scheduler jobs describe "$JOB" --project="$PROJECT" --location="$REGION" >/dev/null 2>&1; then
  # Already paused above: an update changes the run id in the request body, and a job
  # that was running before must not start spending on the new run without --resume.
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
