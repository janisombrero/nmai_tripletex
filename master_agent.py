#!/usr/bin/env python3
"""
master_agent.py — The autonomous agent for the NM i AI 2026 Tripletex competition.

This agent automates the process of testing, debugging, and improving the accounting agent.
"""
import os
import sys
import json
import logging
import time
import subprocess
import shutil
import re
from pathlib import Path

import google.genai as genai
from google.genai import types

from dotenv import load_dotenv

# --- Agent Configuration ---
CHECK_INTERVAL_SECONDS = 60 * 10 # Check every 10 minutes
TEST_SCRIPT = "test_local.py"
HANDLERS_FILE = "handlers.py"
AGENT_FILE = "agent.py"

MAX_FIX_ATTEMPTS = 3 # Max attempts to fix a single failing test
GEMINI_MODEL = "gemini-1.5-flash" # Use the flash model for code generation

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MASTER_AGENT] [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# --- Helper Functions ---
from logging import StreamHandler # ADDED

def _call_gemini_for_fix(
    failing_prompt: str,
    error_message: str,
    relevant_code: str,
    code_source_file: str,
    code_target_name: str
) -> str | None:
    """Calls Gemini to generate a code fix for a failing test."""
    log.info("Calling Gemini to generate code fix...")

    # Using single quotes for the f-string to allow easier triple-quote escaping inside
    full_context = f'''
You are an expert Python developer whose task is to fix bugs in an accounting agent.
The accounting agent uses the Tripletex API.

A test case is failing. Here is the test prompt:
```
{failing_prompt}
```

The error message from the test run is:
```
{error_message}
```

The relevant code that is causing the failure is from the file `{code_source_file}`, specifically the function/prompt named `{code_target_name}`. Here is the current code:
```python
{relevant_code}
```

Your task is to provide a corrected version of the code snippet.
- If `{code_target_name}` is "SYSTEM_PROMPT", return the entire corrected SYSTEM_PROMPT string, enclosed in triple quotes (`"""`).
- If `{code_target_name}` is a Python function (starts with `def` or `async def`), return ONLY the entire corrected function, including its signature and docstrings.
- Ensure the corrected code is valid Python syntax and directly addresses the error described.
- Do NOT include any explanations or extra text outside of the requested code snippet.
'''

    try:
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            log.error("GOOGLE_API_KEY is not set. Cannot call Gemini.")
            return None
        
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(GEMINI_MODEL)
        generation_config = types.GenerationConfig(
            temperature=0.2, # Keep temperature low for deterministic code generation
        )
        
        response = model.generate_content(
            contents=full_context,
            generation_config=generation_config,
        )
        raw_fix = response.text.strip()
        
        log.info(f"Gemini proposed fix (first 500 chars):\n{raw_fix[:500]}")
        
        # Clean up markdown code fences if Gemini adds them
        raw_fix = re.sub(r'^```python\s*', '', raw_fix, flags=re.MULTILINE)
        raw_fix = re.sub(r'\s*```$', '', raw_fix, flags=re.MULTILINE)
        raw_fix = raw_fix.strip()

        return raw_fix

    except Exception as e:
        log.error(f"Error calling Gemini for fix: {e}")
        return None

def _apply_fix_to_file(file_path: str, old_code: str, new_code: str) -> bool:
    """Applies a code fix to a file by replacing old_code with new_code."""
    try:
        content = Path(file_path).read_text(encoding="utf-8")
        if old_code not in content:
            log.error(f"Old code not found in {file_path}. Cannot apply fix.")
            return False
        
        # Ensure only one replacement to avoid unintended changes
        updated_content = content.replace(old_code, new_code, 1)
        Path(file_path).write_text(updated_content, encoding="utf-8")
        log.info(f"Successfully applied fix to {file_path}.")
        return True
    except Exception as e:
        log.error(f"Error applying fix to {file_path}: {e}")
        return False

def run_script(script_name: str, args: list = None, cwd: str = None) -> (bool, str):
    """Executes a script in a subprocess and logs its output, returning success status and output."""
    script_path = os.path.join(cwd if cwd else os.path.dirname(__file__), script_name)
    command = ["python3", script_path] + (args if args else [])
    
    log.info(f"--- Running script: {' '.join(command)} ---")
    try:
        process = subprocess.run(
            command,
            capture_output=True, text=True, check=True, cwd=cwd,
        )
        log.info(f"""Output from {script_name}:
{process.stdout}""")
        if process.stderr:
            log.warning(f"""Stderr from {script_name}:
{process.stderr}""")
        log.info(f"--- Finished script: {script_name} ---")
        return True, process.stdout
    except subprocess.CalledProcessError as e:
        log.error(f"!!! Script {script_name} failed with exit code {e.returncode} !!!")
        log.error(f"Stdout: {e.stdout}")
        log.error(f"Stderr: {e.stderr}")
        return False, e.stdout + e.stderr
    except FileNotFoundError:
        log.error(f"!!! Script not found: {script_path} !!!")
        return False, f"Script not found: {script_path}"

def run_script(script_name: str, args: list = None, cwd: str = None) -> (bool, str):
    """Executes a script in a subprocess and logs its output, returning success status and output."""
    script_path = os.path.join(cwd if cwd else os.path.dirname(__file__), script_name)
    command = ["python3", script_path] + (args if args else [])
    
    log.info(f"--- Running script: {' '.join(command)} ---")
    try:
        process = subprocess.run(
            command,
            capture_output=True, text=True, check=True, cwd=cwd,
        )
        log.info(f"""Output from {script_name}:
{process.stdout}""")
        if process.stderr:
            log.warning(f"""Stderr from {script_name}:
{process.stderr}""")
        log.info(f"--- Finished script: {script_name} ---")
        return True, process.stdout
    except subprocess.CalledProcessError as e:
        log.error(f"!!! Script {script_name} failed with exit code {e.returncode} !!!")
        log.error(f"Stdout: {e.stdout}")
        log.error(f"Stderr: {e.stderr}")
        return False, e.stdout + e.stderr
    except FileNotFoundError:
        log.error(f"!!! Script not found: {script_path} !!!")
        return False, f"Script not found: {script_path}"

def extract_function_code(file_path: str, function_name: str) -> str | None:
    """Extracts the source code of a given function from a Python file."""
    try:
        content = Path(file_path).read_text(encoding="utf-8")
        # Regex to find 'def function_name(...):' and capture its indented body
        # This is a bit simplistic and might fail for complex cases (e.g., nested functions)
        # but should work for top-level handlers.
        pattern = re.compile(rf"^(?:async\s+)?def\s+{re.escape(function_name)}\s*\(.*?\):\s*\n(?P<body>(?:\s{{4}}.*\n)*)", re.MULTILINE)
        match = pattern.search(content)
        if match:
            return match.group(0).strip() # Include 'def' line and body
    except Exception as e:
        log.error(f"Error extracting function '{function_name}' from {file_path}: {e}")
    return None

def extract_system_prompt(file_path: str) -> str | None:
    """Extracts the SYSTEM_PROMPT string from agent.py."""
    try:
        content = Path(file_path).read_text(encoding="utf-8")
        pattern = re.compile(r'SYSTEM_PROMPT\s*=\s*"""(?P<prompt>[\s\S]*?)"""', re.MULTILINE)
        match = pattern.search(content)
        if match:
            return '"""' + match.group("prompt").strip() + '"""'
    except Exception as e:
        log.error(f"Error extracting SYSTEM_PROMPT from {file_path}: {e}")
    return None

def read_test_local_file(test_file_path: str) -> dict:
    """Reads test_local.py and extracts test definitions (number, name, prompt)."""
    tests = {}
    try:
        content = Path(test_file_path).read_text(encoding="utf-8")
        
        # Regex to find individual test dicts within the build_tests function
        test_pattern = re.compile(
            r'\{\s*"number":\s*(\d+),\s*'
            r'"name":\s*"(?P<name>[^"]*)",\s*'
            r'"prompt":\s*\((?P<prompt>[\s\S]+?)\)\s*,' # Non-greedy match for prompt
            r'(?:"before_fn":\s*(?P<before_fn>[^,]+?),\s*)?'
            r'(?:"verify_fn":\s*(?P<verify_fn>[^,]+?),\s*)?'
            r'\}',
            re.DOTALL
        )
        
        for match in test_pattern.finditer(content):
            num = int(match.group(1))
            name = match.group("name")
            prompt_raw = match.group("prompt")
            
            # Clean up prompt (remove multiline string formatting)
            prompt_cleaned = (
                prompt_raw.replace('"', '')
                          .replace("'", "")
                          .replace('\\', '')
                          .replace('\n', ' ')
                          .strip()
            )
            
            tests[num] = {
                "number": num,
                "name": name,
                "prompt": prompt_cleaned
            }
        
    except Exception as e:
        log.error(f"Error reading or parsing {test_file_path}: {e}")
    return tests

def parse_test_results(output: str) -> dict:
    """Parses the output of test_local.py and returns a summary."""
    summary = {
        "total_tests": 0,
        "passed_tests": 0,
        "failed_tests": [],
        "raw_output": output,
    }
    
    # Extract overall pass/fail count
    match = re.search(r"(\d+)/(\d+) passed", output)
    if match:
        summary["passed_tests"] = int(match.group(1))
        summary["total_tests"] = int(match.group(2))
    
    # Extract individual failed tests
    failed_matches = re.findall(r"\[FAIL]\s+Test (\d+): (.+)", output)
    for num, name in failed_matches:
        summary["failed_tests"].append({"number": int(num), "name": name})
        
    return summary


def main():
    log.info("--- Master Agent Started ---")
    
    # Load .env (assuming it's in the same directory as this script)
    dotenv_path = os.path.join(os.path.dirname(__file__), '.env')
    load_dotenv(dotenv_path=dotenv_path)

    # Ensure uvicorn server is running locally
    # We will assume user starts it manually for now, or use the agent.py from astar
    log.info("Assuming local uvicorn server is running. (Use 'uvicorn main:app' in separate terminal).")

    while True:
        log.info(f"--- Starting new test cycle ---")
        
        # 1. Run the test suite
        success, output = run_script(TEST_SCRIPT, cwd=os.path.dirname(__file__))
        results = parse_test_results(output)
        
        log.info(f"Test Summary: {results['passed_tests']}/{results['total_tests']} tests passed.")
        
        if results["failed_tests"]:
            log.error(f"!!! {len(results['failed_tests'])} tests failed !!!")
            
            # Read test details from test_local.py
            all_test_details = read_test_local_file(os.path.join(os.path.dirname(__file__), TEST_SCRIPT))

            for fail_test_summary in results["failed_tests"]:
                test_number = fail_test_summary["number"]
                test_details = all_test_details.get(test_number)
                
                if test_details:
                    log.error(f"  FAIL: Test {test_details['number']}: {test_details['name']}")
                    log.error(f"    Prompt: {test_details['prompt']}")

                    # Try to infer the failing function/prompt based on test name
                    inferred_target = test_details['name'].replace(" ", "_") # e.g., create_employee

                    if inferred_target.startswith("agent_fallback"): # If it's the agent fallback test
                        code_source_file = AGENT_FILE
                        code_target_name = "SYSTEM_PROMPT"
                        relevant_code = extract_system_prompt(os.path.join(os.path.dirname(__file__), AGENT_FILE))
                    elif inferred_target in ["create_project_invoice_partial", "register_hours_and_invoice", "order_invoice_payment_chain"]:
                        # These specific tests involve complex chains or known problematic handlers
                        # We specifically look for their full handler function
                        code_target_name = f"handle_{inferred_target}"
                        relevant_code = extract_function_code(os.path.join(os.path.dirname(__file__), HANDLERS_FILE), code_target_name)
                        code_source_file = HANDLERS_FILE
                    else: # Default: assume a handler function
                        code_target_name = f"handle_{inferred_target}"
                        relevant_code = extract_function_code(os.path.join(os.path.dirname(__file__), HANDLERS_FILE), code_target_name)
                        code_source_file = HANDLERS_FILE

                    if relevant_code:
                        log.error(f"  Relevant code from {code_source_file} for {code_target_name}:\n```python\n{relevant_code}\n```")
                    else:
                        log.error(f"  Could not extract relevant code for {code_target_name} from {code_source_file}.")
                    
                    # --- PHASE 2: SELF-HEALING LOGIC WILL GO HERE ---
                    log.info("Attempting self-healing for this failing test...")
                    fix_applied = False
                    for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
                        log.info(f"Self-healing attempt {attempt}/{MAX_FIX_ATTEMPTS}...")
                        
                        # Call Gemini to get a proposed fix
                        gemini_fix = _call_gemini_for_fix(
                            failing_prompt=test_details['prompt'],
                            error_message="Test failed in local runner.", # Generic error for now
                            relevant_code=relevant_code,
                            code_source_file=code_source_file,
                            code_target_name=code_target_name
                        )

                        if gemini_fix:
                            target_file_path = os.path.join(os.path.dirname(__file__), code_source_file)
                            # Apply the fix to the file
                            if _apply_fix_to_file(target_file_path, relevant_code, gemini_fix):
                                log.info("Fix applied. Re-running failing test to verify...")
                                # Re-run only the specific failing test
                                test_success, test_output = run_script(TEST_SCRIPT, args=["--test", str(test_number)], cwd=os.path.dirname(__file__))
                                new_results = parse_test_results(test_output)

                                if test_success and not new_results["failed_tests"]:
                                    log.info(f"Test {test_number} ({test_details['name']}) PASSED after fix!")
                                    # Commit the fix
                                    log.info("Committing the fix...")
                                    commit_message = f"fix: Master Agent auto-fixed Test {test_number}: {test_details['name']}"
                                    run_shell_command(f"git add {code_source_file}", cwd=os.path.dirname(__file__))
                                    run_shell_command(f"git commit -m '{commit_message}'", cwd=os.path.dirname(__file__))
                                    fix_applied = True
                                    break # Exit retry loop
                                else:
                                    log.warning(f"Test {test_number} still FAILED after fix. Retrying...")
                                    # For simplicity, we assume Gemini will try to refine the fix on next attempt
                                    # A more sophisticated agent might revert to original code before retrying
                                    relevant_code = gemini_fix # Use the attempted fix as new relevant code for next Gemini call
                            else:
                                log.error("Failed to apply fix to file. Aborting self-healing for this test.")
                                break # Exit retry loop
                        else:
                            log.error("Gemini failed to generate a fix. Aborting self-healing for this test.")
                            break # Exit retry loop
                    
                    if not fix_applied:
                        log.error(f"!!! Self-healing failed for Test {test_number} after {MAX_FIX_ATTEMPTS} attempts. Manual intervention required. !!!")
                else:
                    log.error(f"  FAIL: Test {test_number}: {fail_test_summary['name']} (Details not found in {TEST_SCRIPT})")
            
        else:
            log.info("All tests passed! Agent is in a perfect state.")
            
        log.info(f"--- Test cycle complete. Sleeping for {CHECK_INTERVAL_SECONDS} seconds ---")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
