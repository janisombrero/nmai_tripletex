import os
from pathlib import Path


import os
import sys
import json
import logging
import time
import subprocess
import re
from pathlib import Path

from google import genai
from google.genai import types

from dotenv import load_dotenv
from logging import StreamHandler

# --- Agent Configuration ---
CHECK_INTERVAL_SECONDS = 60 * 2 # Check every 2 minutes
COMPETITION_STATE_FILE = "competition_state.json"
TEST_LOCAL_FILE = "test_local.py"

GEMINI_MODEL = "gemini-2.5-flash" # Use the flash model for code generation

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [TEST_GEN_AGENT] [%(levelname)s] %(message)s",
    handlers=[StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# --- Helper Functions ---

def _load_competition_state() -> dict:
    """Loads the competition state from competition_state.json."""
    state_path = os.path.join(os.path.dirname(__file__), COMPETITION_STATE_FILE)
    if Path(state_path).exists():
        return json.loads(Path(state_path).read_text(encoding="utf-8"))
    return {"task_scores": {}, "known_failures": {}, "prompts_seen": []}

def _load_existing_local_tests() -> list[dict]:
    """Reads test_local.py and extracts existing test prompts."""
    tests = []
    try:
        content = (Path(os.path.dirname(__file__)) / TEST_LOCAL_FILE).read_text(encoding="utf-8")
        
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
            prompt_raw = match.group("prompt")
            # Correctly handle escaped backslashes in the code
            prompt_cleaned = prompt_raw.strip()
            tests.append({
                "number": int(match.group(1)),
                "name": match.group("name"),
                "prompt": prompt_cleaned
            })
    except Exception as e:
        log.error(f"Error reading or parsing {TEST_LOCAL_FILE}: {e}")
    return tests

def _call_gemini_to_generate_test(failing_prompt: str, task_type: str) -> str | None:
    """
    Calls Gemini to generate Python code for a new test case.
    This includes the prompt, name, and a verify_fn.
    """
    log.info(f"Calling Gemini to generate test for prompt: {failing_prompt[:80]}...")

    # We need the Tripletex API reference from README.md to provide context to Gemini
    # Adjust path assuming test_generator_agent.py is in nmiai_clean
    readme_path = os.path.join(os.path.dirname(__file__), "README.md")
    readme_content = ""
    try:
        readme_content = Path(readme_path).read_text(encoding="utf-8")
        # Extract API reference section
        match = re.search(r"Tripletex API Reference([\s\S]*?)Common Errors", readme_content, re.DOTALL)
        if match:
            readme_content = match.group(0)
    except FileNotFoundError:
        log.warning("README.md not found for Gemini context. Proceeding without.")

    # Using single quotes for the f-string to allow easier triple-quote escaping inside
    full_context = f'''
You are an expert Python developer and a test engineer for a Tripletex accounting agent.
Your task is to generate a new test case to be added to the `test_local.py` file.
The agent recently failed on the following prompt, which was classified as `{task_type}`:
```
{failing_prompt}
```

Here is a partial Tripletex API reference for context:
```
{readme_content}
```

Generate a new Python dictionary for a test case. The dictionary will be added to the `build_tests` list in `test_local.py`.
The dictionary MUST have the following keys: `number`, `name`, `prompt`.
It should also include a `verify_fn` which is a Python function definition. This function will use `sandbox_get` and `sandbox_post` helpers (provided by `test_local.py`) to verify that the task was successfully completed in the Tripletex sandbox. The `verify_fn` should return `True` for success, `False` for failure.
If setup is needed, include a `before_fn` which is also a Python function definition.

Example `verify_fn` for `create_employee`:
```python
def verify_employee_creation():
    # Find the created employee
    r = sandbox_get("/employee", email="anna.larsen@example.org")
    employees = r.json().get("values", [])
    if not employees: return False
    # Check details if necessary
    return employees[0]["firstName"] == "Anna"
```

Return ONLY the Python dictionary for the test case, including the `before_fn` and `verify_fn` as Python function definitions (not strings).
'''

    try:
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            log.error("GOOGLE_API_KEY is not set. Cannot call Gemini.")
            return None
        
        _gemini_client = genai.Client(api_key=api_key)
        generation_config = types.GenerateContentConfig(
            temperature=0.7, # Higher temperature for creative code generation
        )
        
        response = _gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=full_context,
            config=generation_config,
        )
        raw_test_code = response.text.strip()
        
        # The log.info will use a triple-quoted f-string.
        log.info(f"""Gemini proposed test (first 500 chars):
{raw_test_code[:500]}""")
        
        # Clean up markdown code fences if Gemini adds them
        raw_test_code = re.sub(r'^```python\s*', '', raw_test_code, flags=re.MULTILINE)
        raw_test_code = re.sub(r'\s*```$', '', raw_test_code, flags=re.MULTILINE)
        raw_test_code = raw_test_code.strip()

        return raw_test_code

    except Exception as e:
        log.error(f"Error calling Gemini to generate test: {e}")
        return None

def _append_test_to_file(test_code: str):
    """Appends a new test case to the build_tests list in test_local.py."""
    try:
        content = (Path(os.path.dirname(__file__)) / TEST_LOCAL_FILE).read_text(encoding="utf-8")
        
        # Find the end of the build_tests list
        # This regex is brittle; assumes 'return [...]' is the last thing.
        match = re.search(r'(return\s*\[\s*([\s\S]*?)\s*\])', content, re.DOTALL)
        if not match:
            log.error(f"Could not find build_tests list in {TEST_LOCAL_FILE}.")
            return False

        current_list_str = match.group(2).strip()
        if not current_list_str.endswith(','):
            current_list_str += ','

        # Strip any markdown fence lines Gemini may have left in the generated code
        clean_lines = [
            line for line in test_code.splitlines()
            if line.strip() not in ('```python', '```')
        ]
        test_code = '\n'.join(clean_lines)

        # The new test_code needs to be indented correctly
        new_list_content = f"{current_list_str}\n        {test_code}"
        
        updated_content = content.replace(match.group(1), f"return [\n        {new_list_content}\n    ]")
        
        (Path(os.path.dirname(__file__)) / TEST_LOCAL_FILE).write_text(updated_content, encoding="utf-8")
        log.info(f"Successfully added new test to {TEST_LOCAL_FILE}.")
        return True
    except Exception as e:
        log.error(f"Error appending test to {TEST_LOCAL_FILE}: {e}")
        return False

def main():
    log.info("--- Test Generator Agent Started ---")
    
    # Load .env (assuming it's in the same directory as this script)
    dotenv_path = os.path.join(os.path.dirname(__file__), ".env")
    load_dotenv(dotenv_path=dotenv_path)

    processed_prompts = set() # Keep track of prompts for which we've already generated tests

    while True:
        log.info(f"--- Starting new test generation cycle ---")
        
        # 1. Load competition state
        state = _load_competition_state()
        
        # 2. Load existing local tests to avoid duplicates
        existing_tests = _load_existing_local_tests()
        existing_prompts = {t["prompt"].strip() for t in existing_tests}
        
        # 3. Identify new failing prompts
        new_failing_prompts = []
        for prompt_record in state.get("prompts_seen", []):
            if prompt_record["result"] == "failed":
                cleaned_prompt = prompt_record["prompt"].strip()
                if cleaned_prompt not in existing_prompts and cleaned_prompt not in processed_prompts:
                    new_failing_prompts.append(prompt_record)
        
        if new_failing_prompts:
            log.info(f"Found {len(new_failing_prompts)} new failing prompts to process.")
            for failing_prompt_record in new_failing_prompts:
                failing_prompt = failing_prompt_record["prompt"]
                task_type = failing_prompt_record["task_type"]
                
                log.info(f"Attempting to generate test for: {failing_prompt[:80]}...")
                generated_test_code = _call_gemini_to_generate_test(failing_prompt, task_type)
                
                if generated_test_code:
                    if _append_test_to_file(generated_test_code):
                        processed_prompts.add(failing_prompt.strip()) # Mark as processed
                        log.info("New test added. The Development Agent will pick this up.")
                    else:
                        log.error("Failed to append generated test to file.")
                else:
                    log.error("Gemini failed to generate test code.")
        else:
            log.info("No new failing prompts found to generate tests for.")
            
        log.info(f"--- Cycle complete. Sleeping for {CHECK_INTERVAL_SECONDS} seconds ---")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
