import json
import re
import sys
from typing import Any, Dict, Tuple

from .state import StateManager
from .types import RunContext
from .tools import PersistentTools
from .llm import LLMClient
from .config import (
    SYSTEM_ARCHITECT,
    SYSTEM_CRITIC_PATCH,
    SYSTEM_CRITIC_SPEC,
    SYSTEM_CODER_ENHANCED,
    SYSTEM_CODER_REPAIR,
    SYSTEM_PLANNER,
    SYSTEM_RESEARCHER,
    SYSTEM_TESTER,
    DEFAULT_TEMPERATURE,
    MAX_CODER_STEPS,
    MAX_SPEC_REVIEW_STEPS,
    MAX_PATCH_REVIEW_STEPS,
    MAX_FILES_PER_READ,
    MAX_TESTER_COMMANDS,
)

def format_files_allowed(allowed: Tuple[str, ...]) -> str:
    if not allowed:
        return "(No restriction. All files in workspace allowed except main.py)"
    return str(list(allowed))

class Agent:
    name: str = "agent"
    
    def get_system_prompt(self) -> str:
        raise NotImplementedError
    
    def create_tools(self, workspace_dir: str, files_allowed: Tuple[str, ...], 
                    state_manager: StateManager) -> PersistentTools:
        return PersistentTools(workspace_dir, files_allowed, state_manager, self.name)
    
    def execute_command(self, cmd_data: Dict[str, Any], tools: PersistentTools) -> str:
        """Shared command execution logic for all agents"""
        cmd = cmd_data.get("command")
        
        if cmd == "read_files":
            files = cmd_data.get("files", [])
            if not isinstance(files, list):
                files = [files]
            
            result = {}
            for f in files[:MAX_FILES_PER_READ]:
                try:
                    result[f] = tools.read_text(f)
                except Exception as e:
                    if "Is a directory" in str(e):
                        listing = tools.list_files()
                        result[f] = f"Directory listing:\n{json.dumps(listing, indent=2)}"
                    else:
                        result[f] = f"ERROR: {e}"
            return f"Read Files Output:\n{json.dumps(result, indent=2)}"
            
        elif cmd == "list_files":
            listing = tools.list_files()
            if not listing:
                return "Directory is empty. (No files locally). You should write some code."
            return f"List Files Output:\n{json.dumps(listing, indent=2)}"
            
        elif cmd == "write_file":
            fpath = cmd_data.get("file")
            content = cmd_data.get("content")
            try:
                tools.write_text(fpath, content)
                return f"Successfully wrote file {fpath}."
            except Exception as e:
                return f"Error writing file {fpath}: {e}"
                
        elif cmd == "run_shell":
            args = cmd_data.get("args", "")
            output = tools.run_shell(args)
            return f"Shell Command Output ({args}):\n{output}"
            
        return f"Unknown command: {cmd}"

    def run_with_tools(self, ctx: RunContext, llm: LLMClient, tools: PersistentTools) -> Dict[str, Any]:
        """Override this method to use tools for file operations"""
        raise NotImplementedError
    
    def run(self, ctx: RunContext, llm: LLMClient, tools: PersistentTools) -> Dict[str, Any]:
        # Default implementation uses the old approach
        return self.run_with_tools(ctx, llm, tools)


class ArchitectAgent(Agent):
    name = "architect"
    
    def get_system_prompt(self) -> str:
        return SYSTEM_ARCHITECT
    
    def run_with_tools(self, ctx: RunContext, llm: LLMClient, tools: PersistentTools) -> Dict[str, Any]:
        # First, read any existing relevant files to understand context
        existing_files = tools.list_files()
        relevant_files = []
        
        # Look for common config/spec files
        config_patterns = ['.*spec.*', '.*req.*', 'README', 'config.*', '.*json$', '.*yaml$', '.*yml$']
        for pattern in config_patterns:
            for file in existing_files:
                if re.search(pattern, file, re.IGNORECASE):
                    relevant_files.append(file)
        
        # Read up to 5 relevant files
        file_contents = {}
        for file in relevant_files[:5]:
            try:
                content = tools.read_text(file)
                file_contents[file] = content[:1000] + ("..." if len(content) > 1000 else "")
            except:
                pass
        
        user = (
            f"Objective: {ctx.packet.objective}\n\n"
            f"Files Allowed: {format_files_allowed(ctx.packet.files_allowed)}\n\n"
        )
        
        if file_contents:
            user += f"Existing relevant files:\n{json.dumps(file_contents, indent=2)}\n\n"
        
        user += "Return SPECIFICATION JSON only."
        
        return llm.chat_json(self.get_system_prompt(), user, temperature=DEFAULT_TEMPERATURE)


class PlannerAgent(Agent):
    name = "planner"
    
    def get_system_prompt(self) -> str:
        return SYSTEM_PLANNER
    
    def run_with_tools(self, ctx: RunContext, llm: LLMClient, tools: PersistentTools) -> Dict[str, Any]:
        # Read current code structure to plan better
        code_files = []
        for file in tools.list_files():
            if file.endswith(('.py', '.js', '.ts', '.java', '.cpp', '.c', '.go', '.rs')):
                code_files.append(file)
        
        # Sample a few files to understand structure
        sampled_contents = {}
        for file in code_files[:3]:  # Read up to 3 code files
            try:
                content = tools.read_text(file, max_bytes=2000)
                sampled_contents[file] = content
            except:
                pass
        
        user = (
            f"Objective: {ctx.packet.objective}\n\n"
            f"FROZEN SPEC: {json.dumps(ctx.frozen_spec, ensure_ascii=False)}\n\n"
        )
        
        if sampled_contents:
            user += f"Current codebase (sample):\n{json.dumps(sampled_contents, indent=2)}\n\n"
        
        user += "Return PLAN JSON only."
        
        return llm.chat_json(self.get_system_prompt(), user, temperature=DEFAULT_TEMPERATURE)


class CoderAgent(Agent):
    name = "coder"
    
    def get_system_prompt(self) -> str:
        return SYSTEM_CODER_ENHANCED
    
    def run_with_tools(self, ctx: RunContext, llm: LLMClient, tools: PersistentTools) -> Dict[str, Any]:
        # First, let the coder examine the current state
        files = tools.list_files()
        
        # The coder can decide what files to read based on the objective
        # We'll provide a summary and let the LLM request specific files if needed
        
        user = (
            f"Objective: {ctx.packet.objective}\n\n"
            f"FROZEN SPEC: {json.dumps(ctx.frozen_spec, ensure_ascii=False)}\n\n"
            f"Files Allowed: {format_files_allowed(ctx.packet.files_allowed)}\n"
            f"Workspace files: {files}\n\n"
            "You can first examine files by requesting to read them.\n"
            "Output format:\n"
            "- To read files: {type:'COMMAND', command:'read_files', files:['path1', 'path2']}\n"
            "- To list files: {type:'COMMAND', command:'list_files'}\n"
            "- To write file: {type:'COMMAND', command:'write_file', file:'path', content:'...'}\n"
            "- To run command: {type:'COMMAND', command:'run_shell', args:'ls -la'}\n"
            "- To FINISH: {type:'PATCH', files:[{path:'...', action:'write', content:'...'}]}\n\n"
            "What would you like to do first?"
        )
        
        # Allow up to 15 interaction steps to write/verify code
        max_steps = MAX_CODER_STEPS
        step_count = 0
        last_output = None
        
        while step_count < max_steps:
            response = llm.chat_json(self.get_system_prompt(), user, temperature=DEFAULT_TEMPERATURE)
            
            if response.get("type") == "COMMAND":
                output = self.execute_command(response, tools)
                
                # Loop detection: If output is identical to last time, warn the agent harder or force a break
                if output == last_output:
                     # Force a clearer warning that disrupts the loop
                     warning_msg = (
                         "\nSYSTEM WARNING: You just ran this command and got the exact same output as before. "
                         "You are stuck in a loop. "
                         "STOP running 'list_files' or 'read_files' if you already know the state. "
                         "Write code now using 'write_file' or FINISH with a PATCH."
                     )
                     output += warning_msg
                last_output = output

                user += f"\n\n{output}\n"
                user += "Continue implementing/verifying or output PATCH:"
                step_count += 1
                continue
            
            elif response.get("type") == "PATCH":
                return response
            
            else:
                # Unexpected response, try to continue
                user += f"\n\nPlease use either COMMAND or PATCH.\nReceived: {json.dumps(response, indent=2)}"
                step_count += 1
                continue
        
        # If we've executed enough steps, force a PATCH
        user += "\n\nYou've executed enough steps. Please output a PATCH now."
        final_response = llm.chat_json(self.get_system_prompt(), user, temperature=DEFAULT_TEMPERATURE)
        
        if final_response.get("type") != "PATCH":
            # Fallback
            return {
                "type": "PATCH",
                "files": [{
                    "path": "implementation.py",
                    "action": "write",
                    "content": "# Forced end of session"
                }]
            }
        
        return final_response


class TesterAgent(Agent):
    name = "tester"
    
    def get_system_prompt(self) -> str:
        return SYSTEM_TESTER
    
    def run_with_tools(self, ctx: RunContext, llm: LLMClient, tools: PersistentTools) -> Dict[str, Any]:
        # Same tester logic as before, but with enhanced tools
        objective_lower = ctx.packet.objective.lower()
        looks_daemon = any(k in objective_lower for k in ["every ", "every 60", "runs continuously", "daemon", "server", "loop"])

        if looks_daemon:
            print("[DEBUG] Daemon task detected. Running deterministic smoke test.", file=sys.stderr)
            
            # Identify script name from patches
            script_name = "btc_price_tracker.py"
            for p in reversed(ctx.patches):
                for f in p.get("files", []):
                    if f["path"].endswith(".py") and f["path"] != "main.py":
                        script_name = f["path"]
                        break
            
            # Check if file exists before running
            if not tools.file_exists(script_name):
                return {"type": "TEST_REPORT", "success": False, "report": f"Script not found: {script_name}"}
            
            # Read the script first to understand it
            try:
                script_content = tools.read_text(script_name, max_bytes=5000)
                print(f"[DEBUG] Script preview:\n{script_content[:500]}...", file=sys.stderr)
            except:
                pass
            
            out = tools.run_shell(f"python3 -u {script_name} > agent_test.log 2>&1 &")
            pid_match = re.search(r"__PID__=(\d+)", out)
            pid = pid_match.group(1) if pid_match else None

            tools.run_shell("sleep 5")
            
            # Check DB exists and has at least 1 row
            check = tools.run_shell(
                "python3 - <<'PY'\n"
                "import os, sqlite3, glob\n"
                "dbs = glob.glob('*.db')\n"
                "print(f'FOUND_DBS={dbs}')\n"
                "best_rows = 0\n"
                "for db in dbs:\n"
                "    try:\n"
                "        c = sqlite3.connect(db).cursor()\n"
                "        tables = c.execute(\"SELECT name FROM sqlite_master WHERE type='table';\").fetchall()\n"
                "        for t in tables:\n"
                "            name = t[0]\n"
                "            c.execute(f'SELECT count(*) FROM {name}')\n"
                "            rows = c.fetchone()[0]\n"
                "            print(f'DB={db} TABLE={name} ROWS={rows}')\n"
                "            if rows > best_rows: best_rows = rows\n"
                "    except Exception as e:\n"
                "        print(f'ERROR reading {db}: {e}')\n"
                "print(f'MAX_ROWS={best_rows}')\n"
                "PY"
            )

            log_tail = tools.run_shell("tail -n 10 agent_test.log")

            if pid:
                tools.run_shell(f"kill {pid} || true")

            row_match = re.search(r"MAX_ROWS=\s*(\d+)", check)
            db_ok = row_match and int(row_match.group(1)) > 0
            
            if db_ok:
                return {"type": "TEST_REPORT", "success": True, "report": f"Daemon started, wrote to DB (verified {row_match.group(1)} rows). Log tail: {log_tail}"}
            else:
                if "Traceback" in log_tail or "Error" in log_tail:
                    return {"type": "TEST_REPORT", "success": False, "report": f"Daemon test failed. Log indicates error:\n{log_tail}"}
                
                return {"type": "TEST_REPORT", "success": False, "report": f"Daemon started but DB verification failed.\nCheck output: {check}\nLog: {log_tail}"}

        # Tester loop with file reading capability
        max_commands = MAX_TESTER_COMMANDS
        
        # First, read the main implementation files
        implementation_files = []
        for p in ctx.patches:
            for f in p.get("files", []):
                if f["path"].endswith(('.py', '.js', '.sh')) and f["path"] not in implementation_files:
                    implementation_files.append(f["path"])
        
        # Read up to 2 implementation files
        file_previews = {}
        for file in implementation_files[:2]:
            try:
                content = tools.read_text(file, max_bytes=2000)
                file_previews[file] = content
            except:
                pass
        
        history = (
            f"Objective: {ctx.packet.objective}\n\n"
            f"FROZEN SPEC: {json.dumps(ctx.frozen_spec, ensure_ascii=False)}\n\n"
            f"PATCH: {json.dumps(ctx.patches[-1], ensure_ascii=False)}\n\n"
        )
        
        if file_previews:
            history += f"Implementation files (preview):\n{json.dumps(file_previews, indent=2)}\n\n"
        
        for i in range(max_commands + 2):
            print(f"[DEBUG] Tester loop {i+1}", file=sys.stderr)
            
            force_report = (i >= max_commands)
            prompt = history
            if force_report:
                prompt += "\n\nSTOP: You have reached the command limit. You MUST output a TEST_REPORT now."

            resp = llm.chat_json(SYSTEM_TESTER, prompt, temperature=0.1)
            
            if resp.get("type") == "TEST_REPORT":
                return resp
            
            if resp.get("type") == "COMMAND":
                if force_report:
                    return {
                        "type": "TEST_REPORT",
                        "success": False,
                        "report": "Tester ignored command limit and did not report."
                    }

                cmd = resp.get("command", "")
                out = tools.run_shell(cmd)
                
                history += f"\n>>> COMMAND: {cmd}\n<<< OUTPUT:\n{out}\n\n"
                continue
            
            return resp
            
        return {
            "type": "TEST_REPORT",
            "success": False,
            "report": "Tester exhausted steps without explicit report."
        }
    
class ResearchAgent(Agent):
    name = "researcher"
    
    def get_system_prompt(self) -> str:
        return SYSTEM_RESEARCHER
    
    def run_with_tools(self, ctx: RunContext, llm: LLMClient, tools: PersistentTools) -> Dict[str, Any]:
        raise NotImplementedError("Researcher is called via helper, not main loop")
    
    def verify_url(self, url: str, tools: PersistentTools) -> str:
        print(f"[DEBUG] Validating URL: {url}", file=sys.stderr)
        
        # Security check: basic URL validation
        if not url.startswith("http"):
            return f"Invalid URL format: {url}"
            
        cmd = f"curl -I -L --max-time 10 --insecure '{url}'"
        out = tools.run_shell(cmd)
        
        if "HTTP/2 200" in out or "HTTP/1.1 200" in out:
             return f"URL {url} is VALID (200 OK)."
        elif "404 Not Found" in out:
             return f"URL {url} is BROKEN (404 Not Found)."
        else:
             return f"URL {url} verification result:\n{out[:500]}"


class CriticAgent(Agent):
    name = "critic"
    
    def __init__(self, stage: str) -> None:
        self.stage = stage  # 'SPEC' or 'PATCH'
    
    def get_system_prompt(self) -> str:
        if self.stage == "SPEC":
            return SYSTEM_CRITIC_SPEC
        return SYSTEM_CRITIC_PATCH
    
    def run_with_tools(self, ctx: RunContext, llm: LLMClient, tools: PersistentTools) -> Dict[str, Any]:
        if self.stage == "PATCH":
            # HARD CHECK: Local validation first
            err = tools.validate_patch(ctx.patches[-1])
            if err:
                return {
                    "type": "REVIEW",
                    "status": "REJECT",
                    "critique": f"Hard Validation Failed: {err}",
                    "failure_tags": ["VALIDATION_ERROR"] 
                }

        if self.stage == "SPEC":
            # For SPEC, allow interaction to check URLs
            artifact = ctx.frozen_spec
            
            # Use separate field for research report
            research_report = ctx.latest_research_report
            
            user = f"Objective: {ctx.packet.objective}\n\n"
            if research_report:
                user += f"*** RESEARCH REPORT (AUTO-VERIFIED URLs) ***\n{research_report}\n\n"
            
            user += f"Artifact: {json.dumps(artifact, ensure_ascii=False)}\n\n"
            user += "You can verify URLs using {type:'COMMAND', command:'verify_url', args:'<url>'}\n"
            user += "Return REVIEW JSON only or COMMAND."

            # Interaction loop for SPEC
            max_steps = MAX_SPEC_REVIEW_STEPS
            step_count = 0
            
            while step_count < max_steps:
                response = llm.chat_json(self.get_system_prompt(), user, temperature=0.1)

                if response.get("type") == "COMMAND":
                    if response.get("command") == "verify_url":
                        # Special handling for verify_url in Critic
                        url = response.get("args", "")
                        researcher = ResearchAgent()
                        report = researcher.verify_url(url, tools)
                        user += f"\n\nVerifier Report ({url}):\n{report}\n\nNext?"
                    else:
                        output = self.execute_command(response, tools)
                        user += f"\n\n{output}\n\nNext?"
                    
                    step_count += 1
                    continue

                elif response.get("type") == "REVIEW":
                    return response
                
                else:
                     user += f"\n\nUnknown response: {json.dumps(response)}"
                     step_count += 1
                     continue
            
            # Fallback
            return {
                "type": "REVIEW",
                "status": "REJECT",
                "critique": "Critic exhausted steps during SPEC review.",
                "failure_tags": ["TIMEOUT"]
            }

        # For PATCH, we do the interactive verification
        artifact = ctx.patches[-1]
        frozen = ctx.frozen_spec

        user = f"Objective: {ctx.packet.objective}\n\n"
        if frozen is not None:
            user += f"FROZEN SPEC: {json.dumps(frozen, ensure_ascii=False)}\n\n"
        user += f"Artifact: {json.dumps(artifact, ensure_ascii=False)}\n\n"
        
        user += "You can request to read files or run commands to verify. Output COMMAND or REVIEW."

        # Interaction loop
        max_steps = MAX_PATCH_REVIEW_STEPS
        step_count = 0
        
        while step_count < max_steps:
            response = llm.chat_json(self.get_system_prompt(), user, temperature=0.1)

            if response.get("type") == "COMMAND":
                output = self.execute_command(response, tools)
                user += f"\n\n{output}\n\nNext?"
                step_count += 1
                continue

            elif response.get("type") == "REVIEW":
                return response
            
            else:
                 user += f"\n\nUnknown response: {json.dumps(response)}"
                 step_count += 1
                 continue
        
        # Fallback
        return {
            "type": "REVIEW",
            "status": "REJECT",
            "critique": "Critic exhausted steps without final review.",
            "failure_tags": ["TIMEOUT"]
        }
