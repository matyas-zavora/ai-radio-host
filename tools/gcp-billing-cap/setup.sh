#!/usr/bin/env bash
# Hard spending cap for the Google TTS project: a budget whose Pub/Sub alerts trigger
# a Cloud Function that unlinks billing when the budget is reached.
#
# Usage: ./setup.sh PROJECT_ID [MAX_COST] [BUDGET]
#   MAX_COST: cut billing once reported cost exceeds this (default 0 = first real charge)
#   BUDGET:   budget amount for the email alerts (default 1), in the account's currency
# Needs: gcloud logged in (gcloud auth login) as the project owner + billing admin.
set -euo pipefail

PROJECT="${1:?usage: $0 PROJECT_ID [MAX_COST] [BUDGET]}"
MAX_COST="${2:-0}"
BUDGET="${3:-1}"
REGION="us-central1"          # free-tier region for Cloud Run / Functions
TOPIC="billing-cap"
SA="billing-cap@${PROJECT}.iam.gserviceaccount.com"
HERE="$(cd "$(dirname "$0")" && pwd)"

BILLING="$(gcloud billing projects describe "$PROJECT" --format='value(billingAccountName)')"
BILLING="${BILLING#billingAccounts/}"
[ -n "$BILLING" ] || { echo "Project $PROJECT has no billing account linked."; exit 1; }
CURRENCY="$(gcloud billing accounts describe "$BILLING" --format='value(currencyCode)')"
echo "Project $PROJECT, billing account $BILLING: cut billing above ${MAX_COST} ${CURRENCY}, alerts on a ${BUDGET} ${CURRENCY} budget"

echo "== Enabling APIs"
gcloud services enable --project "$PROJECT" \
  cloudfunctions.googleapis.com run.googleapis.com cloudbuild.googleapis.com \
  eventarc.googleapis.com pubsub.googleapis.com artifactregistry.googleapis.com \
  cloudbilling.googleapis.com billingbudgets.googleapis.com

echo "== Pub/Sub topic"
gcloud pubsub topics describe "$TOPIC" --project "$PROJECT" >/dev/null 2>&1 \
  || gcloud pubsub topics create "$TOPIC" --project "$PROJECT"

echo "== Service account for the function"
gcloud iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1 \
  || gcloud iam service-accounts create billing-cap --project "$PROJECT" \
       --display-name "Unlinks billing when the budget is reached"
# Project Billing Manager can unlink billing from this project (nothing else).
gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" \
  --role roles/billing.projectManager --condition None >/dev/null

echo "== Deploying the function"
gcloud functions deploy stop-billing --gen2 --project "$PROJECT" --region "$REGION" \
  --runtime python312 --source "$HERE" --entry-point stop_billing \
  --trigger-topic "$TOPIC" --service-account "$SA" --trigger-service-account "$SA" \
  --set-env-vars "TARGET_PROJECT=$PROJECT,MAX_COST=$MAX_COST" --memory 256Mi --max-instances 1 --quiet
# The Pub/Sub trigger calls the function as $SA, so it needs invoker rights.
gcloud functions add-invoker-policy-binding stop-billing --project "$PROJECT" \
  --region "$REGION" --member "serviceAccount:$SA" >/dev/null

echo "== Budget"
EXISTING="$(gcloud billing budgets list --billing-project "$PROJECT" --billing-account "$BILLING" \
  --filter='displayName="radio-host hard cap"' --format='value(name)')"
if [ -n "$EXISTING" ]; then
  gcloud billing budgets update "$EXISTING" --billing-project "$PROJECT" \
    --budget-amount "${BUDGET}${CURRENCY}"
else
  gcloud billing budgets create --billing-project "$PROJECT" --billing-account "$BILLING" \
    --display-name "radio-host hard cap" --budget-amount "${BUDGET}${CURRENCY}" \
    --filter-projects "projects/$PROJECT" \
    --threshold-rule percent=0.5 --threshold-rule percent=0.9 --threshold-rule percent=1.0 \
    --notifications-rule-pubsub-topic "projects/$PROJECT/topics/$TOPIC"
fi

echo "== Test (a fake over-budget message flagged as test; nothing is unlinked)"
gcloud pubsub topics publish "$TOPIC" --project "$PROJECT" \
  --message "{\"costAmount\": 0.01, \"budgetAmount\": $BUDGET, \"currencyCode\": \"$CURRENCY\", \"test\": true}"
echo "Check the log in ~1 min:"
echo "  gcloud functions logs read stop-billing --gen2 --project $PROJECT --region $REGION --limit 10"
