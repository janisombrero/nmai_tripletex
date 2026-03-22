import base64
import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from tripletex import TripletexClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI()

_bank_account_initialized = False

def ensure_sandbox_bank_account(client):
    global _bank_account_initialized
    if _bank_account_initialized:
        return
    status, data = client.get("/ledger/account", params={"isBankAccount": "true", "count": 10})
    if status == 200 and data.get("values"):
        for acc in data["values"]:
            if acc.get("bankAccountNumber"):
                _bank_account_initialized = True
                return
        first_acc = data["values"][0]
        first_acc["bankAccountNumber"] = "12345678903"
        client.put(f"/ledger/account/{first_acc['id']}", json=first_acc)
        logger.info("Auto-registered bank account number %s for account %s", "12345678903", first_acc['id'])
    else:
        st, d = client.get("/ledger/account", params={"count": 100})
        if st == 200 and d.get("values"):
            for acc in d["values"]:
                if 1900 <= acc.get("number", 0) <= 1999:
                    acc["isBankAccount"] = True
                    acc["bankAccountNumber"] = "12345678903"
                    client.put(f"/ledger/account/{acc['id']}", json=acc)
                    logger.info("Auto-registered bank account number %s for newly configured bank account %s", "12345678903", acc['id'])
                    break
    _bank_account_initialized = True

import os
from datetime import datetime

VERSION = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

@app.get("/health")
async def health():
    return {"status": "ok", "version": VERSION}

@app.post("/solve")
async def solve(request: Request):
    try:
        body = await request.json()
        prompt = body.get("prompt", "")
        raw_files = body.get("files", [])
        creds = body.get("tripletex_credentials", {})

        base_url = creds.get("base_url") or creds.get("baseUrl") or "https://tripletex.no/v2"
        session_token = (
            creds.get("session_token") or creds.get("sessionToken") or
            creds.get("session_id") or creds.get("token") or ""
        )
        consumer_token = creds.get("consumer_token") or creds.get("consumerToken") or ""

        logger.info("Incoming prompt: %s", prompt)

        # 1. Process files
        extracted_texts = []
        image_parts = []
        for f in raw_files:
            try:
                fname = f.get("filename", "file")
                mtype = f.get("mime_type", "")
                b64 = f.get("content_base64", "")
                if not b64: continue
                data = base64.b64decode(b64)
                if "pdf" in mtype.lower():
                    try:
                        import io as _io
                        from pypdf import PdfReader
                        reader = PdfReader(_io.BytesIO(data))
                        text = "".join([p.extract_text() or "" for p in reader.pages])
                        if text.strip():
                            extracted_texts.append(f"[PDF:{fname}]\n{text.strip()}")
                    except: pass
                elif "image" in mtype.lower():
                    image_parts.append({"mime_type": mtype, "data": data})
                elif "csv" in mtype.lower() or fname.endswith(".csv"):
                    try:
                        text = data.decode("utf-8", errors="ignore")
                        extracted_texts.append(f"[CSV:{fname}]\n{text.strip()}")
                    except: pass
            except: pass

        # 2. Client & Parser
        client = TripletexClient(base_url=base_url, session_token=session_token, consumer_token=consumer_token)
        ensure_sandbox_bank_account(client)
        from agent import parse_task
        parsed = parse_task(prompt, extracted_texts=extracted_texts, image_parts=image_parts)

        from handlers import TaskHandler
        handler = TaskHandler(client, files=raw_files)

        import datetime as _dt
        context = {
            "date.today": "2026-03-20",
            "date.yesterday": (_dt.date.today() - _dt.timedelta(days=1)).strftime("%Y-%m-%d")
        }

        def inject_file_data(fields_obj):
            if not isinstance(fields_obj, dict): return
            if not fields_obj.get("csv_text") and extracted_texts:
                csv_data = "\n\n".join([t for t in extracted_texts if "[CSV:" in t])
                if csv_data: fields_obj["csv_text"] = csv_data
            if not fields_obj.get("pdf_text") and extracted_texts:
                pdf_data = "\n\n".join([t for t in extracted_texts if "[PDF:" in t])
                if pdf_data: fields_obj["pdf_text"] = pdf_data

        # 3. Execution
        final_result = {"success": False, "results": []}

        if isinstance(parsed, list):
            logger.info("Handling multi-task sequence (%d steps)", len(parsed))
            results_list = []
            for i, t in enumerate(parsed):
                tt = t.get("task_type", "unknown")
                tf = t.get("fields", {})
                
                # Auto-inject cached IDs (efficiency)
                for id_key in ("customerId", "employeeId", "projectId", "supplierId", "orderId"):
                    if id_key in context and id_key not in tf:
                        tf[id_key] = context[id_key]

                tf = handler.resolve_templates(tf, context)
                inject_file_data(tf)
                
                if handler.has_empty_id(tf):
                    logger.warning("Step %d depends on missing placeholder — skipping", i)
                    results_list.append({"success": False, "message": "Missing prerequisite ID", "task_type": tt})
                    continue

                step_res = handler.dispatch(tt, tf, context=context)
                results_list.append(step_res)
                if step_res.get("success"):
                    sid = step_res.get("id")
                    if sid:
                        context[f"step_{i}.id"] = sid
                        context[f"step_{i}.value.id"] = sid

                    # Merge all relevant ID keys from the result into context
                    for k, v in step_res.items():
                        if k.endswith("Id") or k == "id":
                            context[k] = v

            final_result = {
                "success": all(r.get("success") for r in results_list) if results_list else False,
                "results": results_list
            }

            # If any step failed or needs fallback, run the whole prompt through fallback
            if not final_result["success"]:
                logger.warning("Multi-task sequence had failures. Triggering whole-prompt fallback.")
                fallback = handler.handle_unknown_with_agent(prompt, {})
                if fallback.get("success"):
                    final_result = fallback
        else:
            task_type = parsed.get("task_type", "unknown")
            fields = parsed.get("fields", {})
            
            if isinstance(fields, list):
                logger.info("Handling bulk tasks (type=%s)", task_type)
                results_list = []
                for item in fields:
                    inject_file_data(item)
                    results_list.append(handler.dispatch(task_type, item, context=context))
                final_result = {
                    "success": all(r.get("success") for r in results_list) if results_list else False,
                    "results": results_list
                }
            else:
                logger.info("Handling single task (type=%s)", task_type)
                inject_file_data(fields)
                result = handler.dispatch(task_type, fields, context=context)
                
                # Fallback
                if task_type == "unknown" or result.get("_needs_fallback"):
                    result = handler.handle_unknown_with_agent(prompt, fields)
                elif not result.get("success"):
                    fallback = handler.handle_unknown_with_agent(prompt, fields)
                    if fallback.get("success"): result = fallback

                final_result = result

        logger.info("Final solver result: %s", final_result)

    except Exception as e:
        logging.exception("Unhandled solver error")
        return JSONResponse({"status": "completed", "success": False, "error": str(e)})

    # Final response MUST include the outcome fields (success, message, etc.)
    return JSONResponse({**final_result, "status": "completed"})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
