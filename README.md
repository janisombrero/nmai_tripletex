# NM i AI 2026 — Tripletex Accounting Agent

An AI agent that completes accounting tasks in Tripletex via natural language prompts.

## Overview

This agent exposes a `/solve` HTTP endpoint that:
1. Receives an accounting task prompt (in Norwegian, English, Spanish, Portuguese, German, French, or Nynorsk)
2. Uses Google Gemini to parse the task and extract relevant fields
3. Executes the appropriate Tripletex REST API calls
4. Returns `{"status": "completed"}`

## Tech Stack

- Python 3.10+
- FastAPI + Uvicorn
- Google Gemini 2.5 Flash-Lite (prompt parsing)
- Tripletex v2 REST API

## Setup

1. Clone the repo
2. Install dependencies:
   pip install -r requirements.txt
3. Create .env file:
   GEMINI_API_KEY=your_key_here
4. Run the server:
   uvicorn main:app --host 0.0.0.0 --port 8000

## Expose publicly (for competition)

npx cloudflared tunnel --url http://localhost:8000

## Endpoint

POST /solve

Accepts a JSON body with prompt, files, and tripletex_credentials.
Returns {"status": "completed"} when done.

## Cloud Run Deployment

1. Open Google Cloud Shell at console.cloud.google.com
2. Run: `git clone https://github.com/janisombrero/nmiai.git && cd nmiai`
3. Run: `export GOOGLE_API_KEY=your_key`
4. Run: `bash deploy.sh`
5. Copy the URL shown and submit at app.ainm.no

## License

MIT
