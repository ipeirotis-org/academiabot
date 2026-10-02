#!/bin/bash
# Deploy the collection Cloud Function (gen 2) and an hourly Cloud Scheduler job.
# Run from the repository root with gcloud authenticated to project wikidata-academia.
#
#   bash deploy/deploy_collect_function.sh            # deploy function; scheduler job left PAUSED
#   bash deploy/deploy_collect_function.sh --resume   # also un-pause the scheduler job
# A redeploy always pauses an existing job, whether or not it was running, so that
# nothing spends LLM credit on the new run id until a person resumes it.
#
# Prerequisites (one time, by a project owner):
#   gcloud services enable cloudfunctions.googleapis.com run.googleapis.com cloudbuild.googleapis.com \
#       artifactregistry.googleapis.com cloudscheduler.googleapis.com eventarc.googleapis.com \
#       --project=wikidata-academia
#
# The scheduler job is created PAUSED so nothing runs (or spends) until a person resumes it.
set -euo pipefail

PROJECT=wikidata-academia
REGION=${REGION:-us-east1}
FUNCTION=academiabot-collect
JOB=academiabot-collect-hourly
SA=claude-agent@wikidata-academia.iam.gserviceaccount.com
RUN_ID=${RUN_ID:-cloud-$(date -u +%Y-%m-%d)}
MAX_PER_HOUR=${MAX_PER_HOUR:-60}

# Source directory for the build: the package (minus generated data) and a root main.py.
SRC=$(mktemp -d)
cp -r wikidata_discover "$SRC/"
rm -rf "$SRC"/wikidata_discover/results/cache "$SRC"/wikidata_discover/results/runs "$SRC"/wikidata_discover/results/reports
cp wikidata_discover/cloud/requirements.txt "$SRC/requirements.txt"
printf 'from wikidata_discover.cloud.collect_function import collect  # noqa: F401\n' > "$SRC/main.py"

gcloud functions deploy "$FUNCTION" \
  --project="$PROJECT" --region="$REGION" --gen2 \
  --runtime=python311 --source="$SRC" --entry-point=collect \
  --trigger-http --no-allow-unauthenticated \
  --service-account="$SA" \
  --memory=1Gi --timeout=3600s --max-instances=1 --concurrency=1 \
  --set-env-vars="GIT_COMMIT=$(git rev-parse --short HEAD)"

URL=$(gcloud functions describe "$FUNCTION" --project="$PROJECT" --region="$REGION" --gen2 --format="value(serviceConfig.uri)")
echo "Function URL: $URL"

BODY="{\"run_id\": \"$RUN_ID\", \"max_universities\": $MAX_PER_HOUR, \"time_budget_s\": 3000}"
if gcloud scheduler jobs describe "$JOB" --project="$PROJECT" --location="$REGION" >/dev/null 2>&1; then
  # Pause first: an update changes the run id in the request body, and a job that was
  # running before must not start spending on the new run without an explicit --resume.
  gcloud scheduler jobs pause "$JOB" --project="$PROJECT" --location="$REGION" >/dev/null
  gcloud scheduler jobs update http "$JOB" --project="$PROJECT" --location="$REGION" \
    --schedule="7 * * * *" --uri="$URL" --http-method=POST --message-body="$BODY" \
    --headers="Content-Type=application/json" --oidc-service-account-email="$SA" --attempt-deadline=30m
  echo "Scheduler job $JOB updated and PAUSED."
else
  gcloud scheduler jobs create http "$JOB" --project="$PROJECT" --location="$REGION" \
    --schedule="7 * * * *" --uri="$URL" --http-method=POST --message-body="$BODY" \
    --headers="Content-Type=application/json" --oidc-service-account-email="$SA" --attempt-deadline=30m
  gcloud scheduler jobs pause "$JOB" --project="$PROJECT" --location="$REGION"
  echo "Scheduler job $JOB created PAUSED."
fi

if [ "${1:-}" = "--resume" ]; then
  gcloud scheduler jobs resume "$JOB" --project="$PROJECT" --location="$REGION"
  echo "Scheduler job $JOB resumed: it calls the function at 7 minutes past every hour."
fi
rm -rf "$SRC"
