#!/bin/bash
set -e
echo "Pulling latest code..."
git pull origin main

echo "Deploying to Cloud Run..."
gcloud run deploy nmiai \
  --source . \
  --region europe-north1 \
  --allow-unauthenticated \
  --min-instances 1 \
  --memory 1Gi \
  --timeout 300 \
  --set-env-vars "GOOGLE_API_KEY=${GOOGLE_API_KEY}"

echo "Done. Testing health endpoint..."
URL=$(gcloud run services describe nmiai --region europe-north1 --format="value(status.url)")
curl -s ${URL}/health
echo ""
echo "Service URL: ${URL}"
