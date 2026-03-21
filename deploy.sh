#!/bin/bash
gcloud run deploy nmiai \
  --source . \
  --region europe-north1 \
  --allow-unauthenticated \
  --min-instances 1 \
  --memory 1Gi \
  --timeout 300 \
  --set-env-vars "GOOGLE_API_KEY=${GOOGLE_API_KEY}"
echo "Deployment complete"
