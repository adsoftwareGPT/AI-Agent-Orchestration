import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from .config import ACTIVE_LLM, ALLOWED_TYPES, LLM_CONFIGS, LLM_RETRIES, LLM_TIMEOUT, DEFAULT_TEMPERATURE

class LLMClient:
    def __init__(self, log_path: Optional[str] = None) -> None:
        self.log_path = log_path
        # Load environment variables from .env file if present
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
        
        config = LLM_CONFIGS.get(ACTIVE_LLM, {})
        
        self.api_url = config.get("api_url") or os.environ.get("LLM_API_URL", "").strip() or "https://api.deepseek.com/chat/completions"
        
        if "api_key" in config:
            self.api_key = config["api_key"]
        elif "api_key_env" in config:
            self.api_key = os.environ.get(config["api_key_env"], "").strip()
        else:
            self.api_key = os.environ.get("LLM_API_KEY", "").strip()

        self.model = config.get("model") or os.environ.get("LLM_MODEL", "").strip() or "deepseek-chat"
        
        if not self.api_key:
            raise RuntimeError("LLM_API_KEY environment variable is not set.")
        if not self.api_url:
            raise RuntimeError("LLM_API_URL environment variable is not set.")

    def _log_trace(self, role: str, content: str) -> None:
        if not self.log_path:
            return
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [{role}] =========================\n{content}\n\n")

    def chat_json(self, system: str, user: str, temperature: float = DEFAULT_TEMPERATURE, max_tokens: Optional[int] = None) -> Dict[str, Any]:
        print(f"[DEBUG] LLM request model={self.model} max_tokens={max_tokens}", file=sys.stderr)
        
        self._log_trace("INPUT", f"SYSTEM:\n{system}\n\nUSER:\n{user}")

        retries = LLM_RETRIES
        truncation_warning = False
        parsed_error_msg = ""

        for attempt in range(retries):
            current_user = user
            if attempt > 0:
                msg = "\n\n(Note: Previous attempt failed."
                if truncation_warning:
                    msg += " The response was TRUNCATED due to length limits. You MUST reduce the size of your output."
                elif parsed_error_msg:
                    msg += f" JSON Error: {parsed_error_msg}. Please ensure valid JSON output."
                else:
                    msg += " Please ensure valid JSON output."
                current_user += msg

            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": current_user},
            ]
            
            payload = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
            }
            if max_tokens is not None:
                payload["max_tokens"] = max_tokens

            try:
                print(f"[DEBUG] sending request (attempt {attempt+1}/{retries})...", file=sys.stderr)
                req = urllib.request.Request(
                    self.api_url,
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.api_key}",
                    },
                    data=json.dumps(payload).encode("utf-8"),
                )
                with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                print("[DEBUG] response received", file=sys.stderr)
                
                self._log_trace("OUTPUT", raw)

                data = json.loads(raw)
                choice = data["choices"][0]
                content = choice["message"]["content"]
                
                if choice.get("finish_reason") == "length":
                    truncation_warning = True
                    print(f"[DEBUG] Response truncated (finish_reason=length)", file=sys.stderr)
                else:
                    truncation_warning = False
                
                try:
                    return parse_single_json_object(content)
                except Exception as parse_err:
                    parsed_error_msg = str(parse_err)
                    print(f"[DEBUG] Parse error: {parse_err}", file=sys.stderr)
                    if attempt < retries - 1:
                        continue
                    else:
                        raise RuntimeError(f"LLM failed format after {retries} retries: {parse_err}")
                
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
                print(f"[DEBUG] HTTP error: {getattr(e, 'code', '?')} {body[:200]}", file=sys.stderr)
                if attempt == retries - 1:
                    raise RuntimeError(f"LLM HTTPError: {getattr(e, 'code', '?')} {body[:400]}")
            except Exception as e:
                print(f"[DEBUG] Error during LLM request/parsing: {e}", file=sys.stderr)
                if attempt == retries - 1:
                    raise RuntimeError(f"LLM request failed: {e}")
                print("[DEBUG] Retrying...", file=sys.stderr)
        
        raise RuntimeError("LLM retries exhausted")

def validate_schema(obj: Dict[str, Any]) -> None:
    t = obj.get("type")
    if t == "PATCH":
        if "files" not in obj or not isinstance(obj["files"], list):
            raise RuntimeError("PATCH missing 'files' list")
        for f in obj["files"]:
            if "path" not in f or "content" not in f or "action" not in f:
                raise RuntimeError("PATCH file entry missing path/content/action")
    elif t == "PLAN":
        if "steps" not in obj or not isinstance(obj["steps"], list):
            raise RuntimeError("PLAN missing 'steps' list")
    elif t == "SPECIFICATION":
        if "requirements" not in obj or not isinstance(obj["requirements"], list):
            raise RuntimeError("SPECIFICATION missing 'requirements' list")
    elif t == "REVIEW":
        if "status" not in obj or obj["status"] not in ["APPROVE", "REJECT"]:
            raise RuntimeError("REVIEW missing status (APPROVE/REJECT)")
    elif t == "TEST_REPORT":
        if "success" not in obj:
            raise RuntimeError("TEST_REPORT missing 'success' boolean")
    elif t == "COMMAND":
        if "command" not in obj:
            raise RuntimeError("COMMAND missing 'command' string")
        
        # Validations for specific commands
        cmd = obj["command"]
        if cmd == "run_shell":
            if "args" not in obj or not isinstance(obj["args"], str):
                raise RuntimeError("COMMAND run_shell requires 'args' string")
        elif cmd == "write_file":
            if "file" not in obj or not isinstance(obj["file"], str):
                raise RuntimeError("COMMAND write_file requires 'file' string")
            if "content" not in obj or not isinstance(obj["content"], str):
                raise RuntimeError("COMMAND write_file requires 'content' string")
        elif cmd == "read_files":
            if "files" not in obj or not isinstance(obj["files"], list):
                raise RuntimeError("COMMAND read_files requires 'files' list")
        elif cmd == "verify_url":
            if "args" not in obj or not isinstance(obj["args"], str):
                raise RuntimeError("COMMAND verify_url requires 'args' string")


def parse_single_json_object(text: str) -> Dict[str, Any]:
    # Remove <think>...</think> blocks if present (common in reasoning models)
    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.DOTALL).strip()

    match = re.search(r"@@@COMMAND_START@@@\s*(.*?)\s*@@@COMMAND_END@@@", text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    
    match_md = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text, re.IGNORECASE)
    if match_md:
        text = match_md.group(1).strip()
    
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]

    try:
        obj = json.loads(text)
    except Exception as e:
        raise RuntimeError(f"Invalid JSON: {e} \nText: {text[:100]}...")

    if not isinstance(obj, dict):
        raise RuntimeError("LLM output must be a JSON object")
    
    validate_schema(obj)
    
    t = obj.get("type")
    if t not in ALLOWED_TYPES:
        raise RuntimeError(f"Invalid or missing type: {t}")
    return obj

def assert_type(obj: Dict[str, Any], expected: str) -> None:
    if obj.get("type") != expected:
        raise RuntimeError(f"Expected type={expected}, got {obj.get('type')}")
