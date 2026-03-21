#!/bin/bash
URL="https://nmiai-457603866091.europe-north1.run.app"
echo "Testing ${URL}/health..."
curl -s ${URL}/health
echo ""
echo "Sending test task..."
curl -s -X POST ${URL}/solve \
  -H "Content-Type: application/json" \
  -d "{\"prompt\": \"Create department Test\", \"tripletex_credentials\": {\"base_url\": \"https://tx-proxy-jwanbnu3pq-lz.a.run.app/v2\", \"session_token\": \"test\"}}"
