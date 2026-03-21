#!/bin/bash
# Full iteration cycle for Cloud Shell
# Usage: bash iterate.sh [--submit] [--logs-only]

set -e

SUBMIT=${1:-""}
PROJECT="ainm26osl-792"
SERVICE="nmiai"
REGION="europe-north1"

echo ""
echo "=================================================="
echo "  NM i AI 2026 — ITERATION CYCLE"
echo "  $(date)"
echo "=================================================="

# Step 1: Show current state
echo ""
echo "📊 Current competition state:"
python3 competition_state.py

# Step 2: Pull latest code
echo ""
echo "📥 Pulling latest code..."
git pull origin master

# Step 3: Deploy
echo ""
echo "🚀 Deploying to Cloud Run..."
source ~/keys.sh
bash redeploy.sh

# Step 4: Remind to submit
if [ "$SUBMIT" != "--logs-only" ]; then
    echo ""
    echo "⚡ SUBMIT NOW at app.ainm.no"
    echo "   URL: https://nmiai-7ckx6z4ikq-lz.a.run.app/solve"
    echo ""
    read -p "   Press [Enter] AFTER you have submitted and scoring is complete..."
    python3 competition_state.py submit
fi

# Step 5: Fetch logs
echo ""
echo "📋 Fetching scorer logs..."
gcloud logging read \
    "resource.type=cloud_run_revision AND resource.labels.service_name=${SERVICE}" \
    --limit=200 \
    --project=${PROJECT} \
    --format="value(textPayload)" \
    --freshness=10m > scorer_logs.txt 2>/dev/null

# Step 6: Update state from logs
echo ""
echo "💾 Updating competition state..."
python3 competition_state.py logs scorer_logs.txt

# Step 7: Show updated state
echo ""
echo "📊 Updated competition state:"
python3 competition_state.py

echo ""
echo "=================================================="
echo "  CYCLE COMPLETE"
echo "=================================================="
