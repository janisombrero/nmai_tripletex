# NM i AI 2026 — Tripletex Autonomous Multi-Agent System Handover

## Overview

This project implements a sophisticated multi-agent system designed to autonomously improve your Tripletex accounting agent's performance in the NM i AI 2026 competition. It automates the process of testing, debugging, and refining your code based on real competition failures, significantly reducing manual effort and accelerating development.

The system is composed of three interconnected autonomous agents:

1.  **Competition Agent (`competition_agent.py`):** Manages interaction with the competition platform (deployments, log collection).
2.  **Development Agent (`master_agent.py`):** Continuously tests your code locally, and uses Gemini to self-heal when tests fail.
3.  **Test Generation Agent (`test_generator_agent.py`):** Dynamically creates new local test cases based on failures observed in the live competition.

---

## Architecture and Workflow

The agents work in a continuous loop:

1.  **Development Agent (Local):** Runs `test_local.py`. If a test fails, it:
    *   Identifies the failing test's prompt and relevant code (from `handlers.py` or `agent.py`).
    *   Constructs a meta-prompt for Gemini, asking for a code fix.
    *   Applies the Gemini-generated fix to the codebase.
    *   Re-runs the test. If it passes, it `git commit`s the change.
2.  **Competition Agent (Local & Cloud):**
    *   Monitors the local `git` repository for new commits (from the Development Agent).
    *   If new commits are detected, it runs `bash redeploy.sh` to deploy the updated code to your Cloud Run service.
    *   **Alerts you to manually submit a task via the competition dashboard** (as this step cannot be automated).
    *   After a waiting period, it downloads the Cloud Run logs via `gcloud` and processes them with `competition_state.py`.
3.  **Test Generation Agent (Local):**
    *   Monitors `competition_state.json` (updated by the Competition Agent).
    *   If it finds a new, failed prompt from the competition for which no local test exists, it:
        *   Uses Gemini to generate Python code for a new test case (including `prompt`, `name`, `before_fn`, and an AI-generated `verify_fn`).
        *   Appends this new test to `test_local.py`.
4.  **Loop Continues:** The Development Agent will then pick up this newly added (and likely failing) test, and begin its self-healing process.

---

## How to Run the System

To run the entire multi-agent system, you will need to start **two** agents in the background. It is also necessary to have your local Uvicorn server running.

### 1. Start Your Local Uvicorn Server (once)

You will need to have your FastAPI server running locally so the `test_local.py` suite can connect to it. This should be run only once.

```bash
cd nmiai_clean && uvicorn main:app --host 0.0.0.0 --port 8000 > uvicorn.log 2>&1 &
```
*   You can check its status with `tail -f nmiai_clean/uvicorn.log`.

### 2. Start the Development Agent (`master_agent.py`)

This agent should be started first, as it's the core of the self-improvement loop.

```bash
nohup python3 nmiai_clean/master_agent.py > nmiai_clean/master_agent.log 2>&1 &
```
*   Monitor its logs: `tail -f nmiai_clean/master_agent.log`

### 3. Start the Competition Agent (`competition_agent.py`)

This agent handles deployments and log processing.

```bash
nohup python3 nmiai_clean/competition_agent.py > nmiai_clean/competition_agent.log 2>&1 &
```
*   Monitor its logs: `tail -f nmiai_clean/competition_agent.log`

### 4. Start the Test Generation Agent (`test_generator_agent.py`)

This agent dynamically creates new tests.

```bash
nohup python3 nmiai_clean/test_generator_agent.py > nmiai_clean/test_generator_agent.log 2>&1 &
```
*   Monitor its logs: `tail -f nmiai_clean/test_generator_agent.log`

---

## What to Expect

-   **Continuous Improvement:** The system will continuously run tests, detect failures (either from the initial 15, or newly generated ones), attempt to fix code, and redeploy.
-   **Manual Submission Required:** You will still need to manually submit tasks on the competition dashboard after each redeployment to provide new failure data. The `competition_agent.py` will log alerts when a redeployment happens.
-   **Logs are Your Friend:** Monitor the log files (`master_agent.log`, `competition_agent.log`, `test_generator_agent.log`, `uvicorn.log`) to understand what the agents are doing and troubleshoot any issues.
-   **Git Activity:** Expect frequent `git commit`s from the `master_agent.py` as it fixes code. The `competition_agent.py` will then `git pull` and redeploy these changes.

---

## Troubleshooting

-   If an agent crashes, check its specific log file for Python tracebacks.
-   Ensure your `GOOGLE_API_KEY` is correctly set in `nmiai_clean/.env` and your `gcloud` CLI is authenticated and configured for the correct project (`ainm26osl-792`).

---

This system should provide a powerful foundation for achieving a high score in the competition! Good luck!