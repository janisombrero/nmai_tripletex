#!/usr/bin/env python3
"""
competition_agent.py — The Competition Agent for the NM i AI 2026 Tripletex competition.

This agent runs in a continuous loop to:
1.  Monitor local codebase for changes (new git commits).
2.  Redeploy the Cloud Run service upon detecting changes.
3.  Alert the user for manual task submission via the competition dashboard.
4.  Download and process Cloud Run logs to update local competition state.
"""
import os
import sys
import json
import logging
import time
import subprocess
import shutil

from dotenv import load_dotenv

# --- Agent Configuration ---
CHECK_INTERVAL_SECONDS = 60 * 5  # Check every 5 minutes
REDEPLOY_SCRIPT = "redeploy.sh"
COMPETITION_STATE_SCRIPT = "competition_state.py"
SCORER_LOGS_FILE = "scorer_logs.txt"

# Project-specific configuration (from redeploy.sh and iterate.sh)
PROJECT = "ainm26osl-792"
SERVICE = "nmiai"
REGION = "europe-north1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [COMP_AGENT] [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# --- Helper Functions ---

def run_shell_command(command: str, cwd: str = None, check: bool = True) -> (bool, str):
    """Executes a shell command and returns success status and output."""
    log.info(f"Running command: {command}")
    try:
        process = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            check=check,
            cwd=cwd,
        )
        if process.stdout:
            log.info(f"""Stdout:
{process.stdout.strip()}""")
        if process.stderr:
            log.warning(f"""Stderr:
{process.stderr.strip()}""")
        return True, process.stdout.strip()
    except subprocess.CalledProcessError as e:
        log.error(f"Command failed with exit code {e.returncode}: {command}")
        log.error(f"""Stdout:
{e.stdout.strip()}""")
        log.error(f"""Stderr:
{e.stderr.strip()}""")
        return False, e.stdout.strip() + e.stderr.strip()


def get_current_commit_hash(repo_path: str) -> str:
    """Gets the current HEAD commit hash of the git repository."""
    success, output = run_shell_command("git rev-parse HEAD", cwd=repo_path)
    if success:
        return output.strip()
    return ""


def redeploy_service(repo_path: str) -> bool:
    """Executes the redeploy.sh script."""
    log.info("Initiating Cloud Run service redeployment...")
    success, _ = run_shell_command(f"bash {REDEPLOY_SCRIPT}", cwd=repo_path)
    return success


def download_and_process_logs(repo_path: str) -> bool:
    """Downloads Cloud Run logs and processes them with competition_state.py."""
    log.info("Downloading Cloud Run logs...")
    # The gcloud command string itself has f-strings, needs careful escaping
    gcloud_command = (
        f"gcloud logging read "
        f"\"resource.type=cloud_run_revision AND resource.labels.service_name={SERVICE}\" " 
        f"--limit=200 "
        f"--project={PROJECT} "
        f"--format=\"value(textPayload)\" " 
        f"--freshness=10m > {SCORER_LOGS_FILE} 2>/dev/null"
    )
    success, _ = run_shell_command(gcloud_command, cwd=repo_path)
    
    if success:
        log.info(f"Logs downloaded to {SCORER_LOGS_FILE}. Processing with competition_state.py...")
        process_success, _ = run_shell_command(
            f"python3 {COMPETITION_STATE_SCRIPT} logs {SCORER_LOGS_FILE}",
            cwd=repo_path
        )
        return process_success
    return False


def main():
    log.info("--- Competition Agent Started ---")
    
    # Ensure GOOGLE_API_KEY is available for gcloud (from .env)
    dotenv_path = os.path.join(os.path.dirname(__file__), '.env')
    load_dotenv(dotenv_path=dotenv_path)

    repo_path = os.path.dirname(__file__)
    last_commit_hash = get_current_commit_hash(repo_path)
    log.info(f"Initial commit hash: {last_commit_hash}")

    while True:
        log.info("Checking for new code commits...")
        run_shell_command("git pull origin main", cwd=repo_path) # Pull latest changes first
        current_commit_hash = get_current_commit_hash(repo_path)

        if current_commit_hash != last_commit_hash:
            log.info(f"New commit detected: {last_commit_hash[:7]} -> {current_commit_hash[:7]}")
            if redeploy_service(repo_path):
                log.info("Cloud Run service redeployed successfully.")
                log.warning("!!! NEW CODE IS LIVE. PLEASE MANUALLY SUBMIT A TASK VIA THE COMPETITION DASHBOARD !!!")
                log.warning("Waiting for manual submission and scoring (5 minutes)...")
                time.sleep(60 * 5) # Wait for task to be submitted and scored
                last_commit_hash = current_commit_hash
            else:
                log.error("Redeployment failed. Previous commit hash retained.")
        else:
            log.info("No new commits.")

        # Always download and process logs, regardless of new commits
        download_and_process_logs(repo_path)
        
        log.info(f"--- Cycle complete. Sleeping for {CHECK_INTERVAL_SECONDS} seconds ---")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
