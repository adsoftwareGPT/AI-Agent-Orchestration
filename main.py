import json
import os
import re
import sys
import time
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from enum import Enum, auto
import pickle
import shutil
from pathlib import Path

class State(Enum):
    SPEC = auto()
    SPEC_REVIEW = auto()
    SPEC_REPAIR = auto()
    PLAN = auto()
    PATCH = auto()
    PATCH_REVIEW = auto()
    APPLY = auto()
    TEST = auto()
    REPAIR_PATCH = auto()
    DONE = auto()


ALLOWED_TYPES = {"SPECIFICATION", "PLAN", "PATCH", "TEST_REPORT", "REVIEW", "QUESTION", "COMMAND"}


@dataclass(frozen=True)
class TaskPacket:
    objective: str
    workspace_dir: str = "."
    # Optional allowlist. If empty, any file under workspace is allowed.
    files_allowed: Tuple[str, ...] = ()
    task_id: str = field(default_factory=lambda: f"task_{int(time.time())}")


@dataclass
class RunContext:
    packet: TaskPacket
    frozen_spec: Optional[Dict[str, Any]] = None
    plan: Optional[Dict[str, Any]] = None
    patches: List[Dict[str, Any]] = field(default_factory=list)
    test_reports: List[Dict[str, Any]] = field(default_factory=list)
    
    # State machine transient data
    spec_review: Optional[Dict[str, Any]] = None
    patch_review: Optional[Dict[str, Any]] = None
    
    # Add state tracking
    current_state: State = State.SPEC
    iteration_count: int = 0


# -----------------------------
# Persistent State Manager
# -----------------------------

class StateManager:
    """Manages persistent state storage for the workflow"""
    
    def __init__(self, workspace_dir: str, task_id: str):
        self.workspace_dir = workspace_dir
        self.state_dir = os.path.join(workspace_dir, ".agent_state")
        self.task_id = task_id
        os.makedirs(self.state_dir, exist_ok=True)
        
        # Create a changelog directory for file history
        self.changelog_dir = os.path.join(self.state_dir, "changelogs")
        os.makedirs(self.changelog_dir, exist_ok=True)
    
    def save_context(self, ctx: RunContext) -> None:
        """Save the entire context to disk"""
        context_path = os.path.join(self.state_dir, "context.json")
        # Convert to serializable format
        context_dict = {
            "packet": {
                "objective": ctx.packet.objective,
                "workspace_dir": ctx.packet.workspace_dir,
                "files_allowed": list(ctx.packet.files_allowed),
                "task_id": ctx.packet.task_id
            },
            "frozen_spec": ctx.frozen_spec,
            "plan": ctx.plan,
            "patches": ctx.patches,
            "test_reports": ctx.test_reports,
            "spec_review": ctx.spec_review,
            "patch_review": ctx.patch_review,
            "current_state": ctx.current_state.name,
            "iteration_count": ctx.iteration_count
        }
        
        with open(context_path, 'w', encoding='utf-8') as f:
            json.dump(context_dict, f, indent=2, ensure_ascii=False)
        
        # Also save a backup with timestamp
        backup_path = os.path.join(self.state_dir, f"context_backup_{int(time.time())}.json")
        shutil.copy2(context_path, backup_path)
    
    def load_context(self) -> Optional[RunContext]:
        """Load context from disk if it exists"""
        context_path = os.path.join(self.state_dir, "context.json")
        if not os.path.exists(context_path):
            return None
        
        try:
            with open(context_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            packet = TaskPacket(
                objective=data["packet"]["objective"],
                workspace_dir=data["packet"]["workspace_dir"],
                files_allowed=tuple(data["packet"]["files_allowed"]),
                task_id=data["packet"]["task_id"]
            )
            
            ctx = RunContext(packet=packet)
            ctx.frozen_spec = data.get("frozen_spec")
            ctx.plan = data.get("plan")
            ctx.patches = data.get("patches", [])
            ctx.test_reports = data.get("test_reports", [])
            ctx.spec_review = data.get("spec_review")
            ctx.patch_review = data.get("patch_review")
            ctx.current_state = State[data.get("current_state", "SPEC")]
            ctx.iteration_count = data.get("iteration_count", 0)
            
            return ctx
        except Exception as e:
            print(f"[DEBUG] Failed to load context: {e}", file=sys.stderr)
            return None
    
    def save_artifact(self, name: str, artifact: Dict[str, Any]) -> None:
        """Save a specific artifact (spec, plan, patch, etc.)"""
        artifact_dir = os.path.join(self.state_dir, "artifacts")
        os.makedirs(artifact_dir, exist_ok=True)
        
        artifact_path = os.path.join(artifact_dir, f"{name}_{int(time.time())}.json")
        with open(artifact_path, 'w', encoding='utf-8') as f:
            json.dump(artifact, f, indent=2, ensure_ascii=False)
    
    def save_file_snapshot(self, file_path: str, content: str, agent: str) -> None:
        """Save a snapshot of a file before modification"""
        snapshot_dir = os.path.join(self.changelog_dir, agent)
        os.makedirs(snapshot_dir, exist_ok=True)
        
        snapshot_path = os.path.join(snapshot_dir, 
                                   f"{Path(file_path).name}_{int(time.time())}.snapshot")
        
        snapshot_data = {
            "timestamp": time.time(),
            "agent": agent,
            "file_path": file_path,
            "content": content,
            "task_id": self.task_id
        }
        
        with open(snapshot_path, 'w', encoding='utf-8') as f:
            json.dump(snapshot_data, f, indent=2, ensure_ascii=False)
    
    def get_file_history(self, file_path: str) -> List[Dict[str, Any]]:
        """Get the change history of a specific file"""
        history = []
        for agent_dir in os.listdir(self.changelog_dir):
            agent_path = os.path.join(self.changelog_dir, agent_dir)
            if not os.path.isdir(agent_path):
                continue
            
            for snapshot_file in os.listdir(agent_path):
                if snapshot_file.endswith('.snapshot'):
                    snapshot_path = os.path.join(agent_path, snapshot_file)
                    try:
                        with open(snapshot_path, 'r', encoding='utf-8') as f:
                            snapshot = json.load(f)
                            if snapshot.get("file_path") == file_path:
                                history.append(snapshot)
                    except:
                        continue
        
        # Sort by timestamp
        history.sort(key=lambda x: x.get("timestamp", 0))
        return history


# -----------------------------
# Enhanced Tools with Persistent Read/Write
# -----------------------------

class PersistentTools:
    def __init__(self, workspace_dir: str, files_allowed: Tuple[str, ...], 
                 state_manager: StateManager, agent_name: str) -> None:
        self.workspace_dir = os.path.abspath(workspace_dir)
        self.files_allowed = set(files_allowed)
        self.state_manager = state_manager
        self.agent_name = agent_name
        
        # Track files we've read/written in this session
        self.files_accessed = set()
        self.files_modified = set()
    
    def _abs(self, rel_path: str) -> str:
        p = os.path.abspath(os.path.join(self.workspace_dir, rel_path))
        if not p.startswith(self.workspace_dir + os.sep) and p != self.workspace_dir:
            raise RuntimeError("path escape blocked")
        return p

    def _check_allowed(self, rel_path: str) -> None:
        if os.path.normpath(rel_path) == "main.py":
            raise RuntimeError("security violation: agents rarely need to touch main.py, and are restricted from doing so.")
        if self.files_allowed and rel_path not in self.files_allowed:
            raise RuntimeError(f"file not allowed: {rel_path}")

    def list_files(self, limit: int = 300) -> List[str]:
        out: List[str] = []
        for root, _, files in os.walk(self.workspace_dir):
            for fn in files:
                ap = os.path.join(root, fn)
                rp = os.path.relpath(ap, self.workspace_dir)
                if rp == "main.py":
                    continue
                if rp.startswith(".agent_state"):
                    continue  # Skip state files
                out.append(rp)
        out.sort()
        return out[:limit]
    
    def file_exists(self, rel_path: str) -> bool:
        """Check if a file exists"""
        ap = self._abs(rel_path)
        return os.path.exists(ap)

    def read_text(self, rel_path: str, max_bytes: int = 50_000) -> str:
        """Read file content with persistence tracking"""
        self._check_allowed(rel_path)
        ap = self._abs(rel_path)
        self.files_accessed.add(rel_path)
        
        if not os.path.exists(ap):
            raise RuntimeError(f"File not found: {rel_path}")
        
        with open(ap, "rb") as f:
            b = f.read(max_bytes + 1)
        if len(b) > max_bytes:
            return b[:max_bytes].decode("utf-8", errors="replace") + "\n...TRUNCATED..."
        return b.decode("utf-8", errors="replace")
    
    def read_multiple_files(self, file_paths: List[str]) -> Dict[str, str]:
        """Read multiple files at once"""
        results = {}
        for path in file_paths:
            try:
                results[path] = self.read_text(path)
            except Exception as e:
                results[path] = f"ERROR: {e}"
        return results

    def write_text(self, rel_path: str, content: str, 
                   create_backup: bool = True) -> None:
        """Write file content with persistence tracking"""
        self._check_allowed(rel_path)
        ap = self._abs(rel_path)
        self.files_modified.add(rel_path)
        
        # Create backup before modification
        if create_backup and os.path.exists(ap):
            try:
                with open(ap, 'r', encoding='utf-8') as f:
                    old_content = f.read()
                self.state_manager.save_file_snapshot(rel_path, old_content, self.agent_name)
            except:
                pass  # If we can't read old content, continue anyway
        
        os.makedirs(os.path.dirname(ap), exist_ok=True)
        with open(ap, "w", encoding="utf-8") as f:
            f.write(content)
    
    def append_text(self, rel_path: str, content: str) -> None:
        """Append content to a file"""
        self._check_allowed(rel_path)
        ap = self._abs(rel_path)
        
        # Read existing content if file exists
        existing = ""
        if os.path.exists(ap):
            with open(ap, "r", encoding="utf-8") as f:
                existing = f.read()
        
        self.write_text(rel_path, existing + content, create_backup=True)
    
    def copy_file(self, src_rel_path: str, dest_rel_path: str) -> None:
        """Copy a file within the workspace"""
        self._check_allowed(src_rel_path)
        self._check_allowed(dest_rel_path)
        
        src_abs = self._abs(src_rel_path)
        dest_abs = self._abs(dest_rel_path)
        
        if not os.path.exists(src_abs):
            raise RuntimeError(f"Source file not found: {src_rel_path}")
        
        # Create backup of destination if it exists
        if os.path.exists(dest_abs):
            try:
                with open(dest_abs, 'r', encoding='utf-8') as f:
                    old_content = f.read()
                self.state_manager.save_file_snapshot(dest_rel_path, old_content, self.agent_name)
            except:
                pass
        
        os.makedirs(os.path.dirname(dest_abs), exist_ok=True)
        shutil.copy2(src_abs, dest_abs)
        self.files_modified.add(dest_rel_path)
    
    def get_file_info(self, rel_path: str) -> Dict[str, Any]:
        """Get metadata about a file"""
        ap = self._abs(rel_path)
        if not os.path.exists(ap):
            return {"exists": False}
        
        stat = os.stat(ap)
        return {
            "exists": True,
            "size": stat.st_size,
            "modified": stat.st_mtime,
            "is_file": os.path.isfile(ap),
            "is_dir": os.path.isdir(ap)
        }
    
    def get_file_history(self, rel_path: str) -> List[Dict[str, Any]]:
        """Get the change history of a file"""
        return self.state_manager.get_file_history(rel_path)
    
    def search_in_files(self, pattern: str, file_pattern: str = "*.py") -> List[Dict[str, Any]]:
        """Search for text pattern in files matching file_pattern"""
        import fnmatch
        
        results = []
        for root, _, files in os.walk(self.workspace_dir):
            for file in files:
                if fnmatch.fnmatch(file, file_pattern):
                    rel_path = os.path.relpath(os.path.join(root, file), self.workspace_dir)
                    if rel_path.startswith(".agent_state"):
                        continue
                    
                    try:
                        content = self.read_text(rel_path)
                        if re.search(pattern, content, re.IGNORECASE):
                            # Show context around match
                            lines = content.split('\n')
                            for i, line in enumerate(lines):
                                if re.search(pattern, line, re.IGNORECASE):
                                    start = max(0, i - 2)
                                    end = min(len(lines), i + 3)
                                    context = '\n'.join(lines[start:end])
                                    results.append({
                                        "file": rel_path,
                                        "line": i + 1,
                                        "match": re.search(pattern, line).group(0),
                                        "context": context
                                    })
                    except Exception as e:
                        continue
        
        return results

    def validate_patch(self, patch: Dict[str, Any]) -> Optional[str]:
        # Local deterministic check
        try:
            files = patch.get("files")
            if not isinstance(files, list):
                return "PATCH missing files[]"
            for item in files:
                if not isinstance(item, dict):
                    return "PATCH file entry must be object"
                path = item.get("path")
                action = item.get("action")
                content = item.get("content")
                if not isinstance(path, str) or not path:
                    return "PATCH file entry missing path"
                if action != "write":
                    return "PATCH supports only action=write"
                if not isinstance(content, str):
                    return "PATCH content must be string"
                
                # Check path policies
                self._check_allowed(path)
                
            return None # Valid
        except Exception as e:
            return f"Validation Error: {e}"

    def apply_patch(self, patch: Dict[str, Any]) -> None:
        # PATCH schema:
        # {"type":"PATCH","files":[{"path":"x.py","action":"write","content":"..."}]}
        files = patch.get("files")
        if not isinstance(files, list):
            raise RuntimeError("PATCH missing files[]")
        for item in files:
            if not isinstance(item, dict):
                raise RuntimeError("PATCH file entry must be object")
            path = item.get("path")
            action = item.get("action")
            content = item.get("content")
            if not isinstance(path, str) or not path:
                raise RuntimeError("PATCH file entry missing path")
            if action != "write":
                raise RuntimeError("PATCH supports only action=write in this minimal version")
            if not isinstance(content, str):
                raise RuntimeError("PATCH content must be string")
            self.write_text(path, content)

    def run_shell(self, command: str) -> str:
        # Basic security: only allow running in workspace
        print("[DEBUG] EXECUTING:", command, file=sys.stderr)

        is_background = bool(re.search(r"(^|\s)&\s*$", command.strip()))

        try:
            if is_background:
                proc = subprocess.Popen(
                    ["bash", "-lc", f"{command} echo __PID__=$!"],
                    cwd=self.workspace_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                try:
                    out, _ = proc.communicate(timeout=5)
                    return (out or "").strip()
                except subprocess.TimeoutExpired:
                    return "ERROR: Background launch timed out"

            # Normal (foreground) command
            res = subprocess.run(
                command, 
                shell=True,
                cwd=self.workspace_dir, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.STDOUT, 
                timeout=30,
                text=True
            )
            return res.stdout or ""
        except subprocess.TimeoutExpired:
            return "ERROR: TimeoutExpired"
        except Exception as e:
            return f"ERROR: {e}"


# -----------------------------
# LLM client (same as before)
# -----------------------------

class LLMClient:
    def __init__(self, log_path: Optional[str] = None) -> None:
        self.log_path = log_path
        # Load environment variables from .env file if present
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
        
        self.api_url = os.environ.get("LLM_API_URL", "").strip() or "https://api.deepseek.com/chat/completions"
        self.api_key = os.environ.get("LLM_API_KEY", "").strip()
        self.model = os.environ.get("LLM_MODEL", "").strip() or "deepseek-chat"
        
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

    def chat_json(self, system: str, user: str, temperature: float = 0.2, max_tokens: Optional[int] = None) -> Dict[str, Any]:
        print(f"[DEBUG] LLM request model={self.model} max_tokens={max_tokens}", file=sys.stderr)
        
        self._log_trace("INPUT", f"SYSTEM:\n{system}\n\nUSER:\n{user}")

        retries = 3
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
                with urllib.request.urlopen(req, timeout=300) as resp:
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


# -----------------------------
# Enhanced Prompts with File Awareness
# -----------------------------

SYSTEM_BASE = (
    "You are an agent in a software factory.\n"
    "HARD CONSTRAINTS:\n"
    "1) Output exactly one JSON object. No preamble, no postscript, no markdown fences.\n"
    "2) Only when type=COMMAND wrap the JSON in @@@COMMAND_START@@@ and @@@COMMAND_END@@@ delimiters.\n"
    "3) Allowed top-level output types: SPECIFICATION, PLAN, PATCH, TEST_REPORT, REVIEW, QUESTION, COMMAND\n"
    "4) Do not invent requirements. Use only: objective + frozen spec (if provided).\n"
    "5) Be brief.\n"
)

SYSTEM_ARCHITECT = SYSTEM_BASE + (
    "ROLE: ARCHITECT\n"
    "You can read existing files to understand the current state.\n"
    "Output a SPECIFICATION JSON: {type:'SPECIFICATION', overview:'...', requirements:[...], verification_plan:[...]}\n"
    "Requirements must be binary/testable, include output format and error behavior.\n"
)

SYSTEM_CRITIC = SYSTEM_BASE + (
    "ROLE: CRITIC\n"
    "You can read files to validate them against requirements.\n"
    "Validate an artifact against objective + frozen spec (if provided).\n"
    "CRITICAL RULE: Only REJECT if there is a BLOCKING functional error, missing requirement, or violation of hard restrictions.\n"
    "Do NOT REJECT for style preferences, minor formatting, comments, or 'improvements'. If code works/complies, APPROVE.\n"
    "Output REVIEW JSON: {type:'REVIEW', status:'APPROVE'|'REJECT', critique:'...', failure_tags:[...]}\n"
)

SYSTEM_PLANNER = SYSTEM_BASE + (
    "ROLE: PLANNER\n"
    "You can read files to understand the current codebase.\n"
    "Create a minimal PLAN JSON: {type:'PLAN', steps:[...]} with 2-5 steps.\n"
    "No extra features.\n"
)

SYSTEM_CODER_ENHANCED = SYSTEM_BASE + (
    "ROLE: CODER\n"
    "You have read/write access to files. You can read existing code to understand the context.\n"
    "You can either:\n"
    "1) Output a PATCH JSON: {type:'PATCH', files:[{path, action:'write', content}]}\n"
    "   OR\n"
    "2) First read files to understand current state (optional)\n"
    "Only edit/create files listed in Files Allowed (if provided).\n"
    "When modifying existing files, try to preserve existing functionality unless required to change.\n"
)

SYSTEM_TESTER = SYSTEM_BASE + (
    "ROLE: TESTER\n"
    "You have a REPL cycle to verify the objective.\n"
    "You can read files to understand the codebase before testing.\n"
    "1) To run a command: Output {type:'COMMAND', command:'...'}\n"
    "2) If satisfied, output TEST_REPORT: {type:'TEST_REPORT', success:true, report:'...'}\n"
    "3) If failed/stuck, output TEST_REPORT: {type:'TEST_REPORT', success:false, report:'...'}\n"
    "CRITICAL: If testing a long-running process (server/daemon), verifying it starts and runs for a few seconds is sufficient.\n"
)


# -----------------------------
# Enhanced Agents with File Access
# -----------------------------

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
        
        return llm.chat_json(self.get_system_prompt(), user, temperature=0.2)


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
        
        return llm.chat_json(self.get_system_prompt(), user, temperature=0.2)


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
            "- To write PATCH: {type:'PATCH', files:[{path:'...', action:'write', content:'...'}]}\n\n"
            "What would you like to do first?"
        )
        
        # Allow up to 3 read operations before requiring a patch
        max_reads = 3
        read_count = 0
        max_errors = 3
        error_count = 0
        
        while read_count < max_reads and error_count < max_errors:
            response = llm.chat_json(self.get_system_prompt(), user, temperature=0.2)
            
            if response.get("type") == "COMMAND":
                cmd = response.get("command")
                
                if cmd == "read_files":
                    files_to_read = response.get("files", [])
                    if not isinstance(files_to_read, list):
                        files_to_read = [files_to_read]
                    
                    # Read requested files
                    file_contents = {}
                    for file_path in files_to_read[:5]:  # Limit to 5 files per request
                        try:
                            content = tools.read_text(file_path)
                            file_contents[file_path] = content
                        except Exception as e:
                            file_contents[file_path] = f"ERROR reading file: {e}"
                    
                    # Update user prompt with file contents
                    user += f"\n\nRequested files content:\n{json.dumps(file_contents, indent=2)}\n\n"
                    user += "Continue examining files or output PATCH:"
                    
                    read_count += 1
                    continue
                
                elif cmd == "list_files":
                    files = tools.list_files()
                    user += f"\n\nAdditional file listing:\n{files}\n\n"
                    user += "Continue examining files or output PATCH:"
                    read_count += 1
                    continue

            if response.get("type") == "PATCH":
                return response
            
            # Unexpected response, try to continue
            error_count += 1
            user += f"\n\nPlease use either COMMAND to read files or PATCH to write files.\nReceived: {json.dumps(response, indent=2)}"
            continue
        
        # If we've read enough files, force a PATCH
        user += "\n\nYou've examined enough files. Please output a PATCH now."
        final_response = llm.chat_json(self.get_system_prompt(), user, temperature=0.2)
        
        if final_response.get("type") != "PATCH":
            # Fallback: create a minimal patch
            return {
                "type": "PATCH",
                "files": [{
                    "path": "implementation.py",
                    "action": "write",
                    "content": "# Implementation placeholder\nprint('TODO: Implement according to spec')"
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
        max_commands = 3
        
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


class CriticAgent(Agent):
    name = "critic"
    
    def __init__(self, stage: str) -> None:
        self.stage = stage  # 'SPEC' or 'PATCH'
    
    def get_system_prompt(self) -> str:
        return SYSTEM_CRITIC
    
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
            
            # (Logic removed: Do not read files from disk as they are not applied yet)
            file_contents = {}

        if self.stage == "SPEC":
            artifact = ctx.frozen_spec
            frozen = None
        else:
            artifact = ctx.patches[-1]
            frozen = ctx.frozen_spec

        user = f"Objective: {ctx.packet.objective}\n\n"
        if frozen is not None:
            user += f"FROZEN SPEC: {json.dumps(frozen, ensure_ascii=False)}\n\n"
        user += f"Artifact: {json.dumps(artifact, ensure_ascii=False)}\n\n"
        
        if self.stage == "PATCH" and file_contents:
            user += f"Actual file contents after patch:\n{json.dumps(file_contents, indent=2)}\n\n"
        
        user += "Return REVIEW JSON only."
        
        return llm.chat_json(self.get_system_prompt(), user, temperature=0.1)


# -----------------------------
# Enhanced Orchestrator with Persistence
# -----------------------------

class PersistentOrchestrator:
    def __init__(self, llm: LLMClient, workspace_dir: str, task_id: str) -> None:
        self.llm = llm
        self.workspace_dir = workspace_dir
        self.state_manager = StateManager(workspace_dir, task_id)
        
        # Load existing context if available
        self.ctx = self.state_manager.load_context()
        if self.ctx:
            print(f"[DEBUG] Loaded existing context from state {self.ctx.current_state}", file=sys.stderr)
            self.state = self.ctx.current_state
        else:
            self.state = State.SPEC
        
        self.max_repairs = 3
        self.repair_count = 0
        self.max_spec_repairs = 2
        self.spec_repair_count = 0
    
    def save_state(self) -> None:
        """Save current state to disk"""
        if self.ctx:
            self.ctx.current_state = self.state
            self.state_manager.save_context(self.ctx)
    
    def create_agent_tools(self, agent_name: str) -> PersistentTools:
        """Create tools for a specific agent"""
        files_allowed = self.ctx.packet.files_allowed if self.ctx else ()
        return PersistentTools(self.workspace_dir, files_allowed, self.state_manager, agent_name)
    
    def run(self, packet: Optional[TaskPacket] = None) -> int:
        # Initialize context if not loaded
        if not self.ctx and packet:
            self.ctx = RunContext(packet=packet)
            self.state = State.SPEC
        elif not self.ctx:
            raise RuntimeError("No context loaded and no packet provided")
        
        while self.state != State.DONE:
            print(f"[DEBUG] Entering State: {self.state.name}", file=sys.stderr)
            self.ctx.iteration_count += 1
            
            try:
                if self.state == State.SPEC:
                    tools = self.create_agent_tools("architect")
                    spec = ArchitectAgent().run_with_tools(self.ctx, self.llm, tools)
                    assert_type(spec, "SPECIFICATION")
                    self.ctx.frozen_spec = spec
                    self.state_manager.save_artifact("spec", spec)
                    self.state = State.SPEC_REVIEW
                
                elif self.state == State.SPEC_REVIEW:
                    tools = self.create_agent_tools("critic_spec")
                    review = CriticAgent("SPEC").run_with_tools(self.ctx, self.llm, tools)
                    assert_type(review, "REVIEW")
                    self.ctx.spec_review = review
                    self.state_manager.save_artifact("spec_review", review)
                    
                    if review.get("status") == "APPROVE":
                        self.state = State.PLAN
                    else:
                        self.state = State.SPEC_REPAIR
                
                elif self.state == State.SPEC_REPAIR:
                    if self.spec_repair_count >= self.max_spec_repairs:
                        print("[DEBUG] Max spec repairs reached.", file=sys.stderr)
                        self.state = State.DONE
                        continue
                    
                    self.spec_repair_count += 1
                    tools = self.create_agent_tools("architect")
                    spec2 = self.repair_spec(self.ctx, self.ctx.spec_review, tools)
                    self.ctx.frozen_spec = spec2
                    self.state_manager.save_artifact(f"spec_repair_{self.spec_repair_count}", spec2)
                    self.state = State.SPEC_REVIEW
                
                elif self.state == State.PLAN:
                    tools = self.create_agent_tools("planner")
                    plan = PlannerAgent().run_with_tools(self.ctx, self.llm, tools)
                    assert_type(plan, "PLAN")
                    self.ctx.plan = plan
                    self.state_manager.save_artifact("plan", plan)
                    self.state = State.PATCH
                
                elif self.state == State.PATCH:
                    tools = self.create_agent_tools("coder")
                    patch = CoderAgent().run_with_tools(self.ctx, self.llm, tools)
                    assert_type(patch, "PATCH")
                    self.ctx.patches.append(patch)
                    self.state_manager.save_artifact(f"patch_{len(self.ctx.patches)}", patch)
                    self.state = State.PATCH_REVIEW
                
                elif self.state == State.PATCH_REVIEW:
                    tools = self.create_agent_tools("critic_patch")
                    review = CriticAgent("PATCH").run_with_tools(self.ctx, self.llm, tools)
                    assert_type(review, "REVIEW")
                    self.ctx.patch_review = review
                    self.state_manager.save_artifact("patch_review", review)
                    
                    if review.get("status") == "APPROVE":
                        self.state = State.APPLY
                    else:
                        self.state = State.REPAIR_PATCH
                
                elif self.state == State.APPLY:
                    tools = self.create_agent_tools("orchestrator")
                    tools.apply_patch(self.ctx.patches[-1])
                    self.state = State.TEST
                
                elif self.state == State.TEST:
                    tools = self.create_agent_tools("tester")
                    test_report = TesterAgent().run_with_tools(self.ctx, self.llm, tools)
                    assert_type(test_report, "TEST_REPORT")
                    self.ctx.test_reports.append(test_report)
                    self.state_manager.save_artifact(f"test_report_{len(self.ctx.test_reports)}", test_report)
                    
                    if test_report.get("success"):
                        print("[DEBUG] Test passed! Success.", file=sys.stderr)
                        self.state = State.DONE
                    else:
                        if self.repair_count < self.max_repairs:
                            self.repair_count += 1
                            self.state = State.REPAIR_PATCH
                        else:
                            print("[DEBUG] Max repairs reached.", file=sys.stderr)
                            self.state = State.DONE
                
                elif self.state == State.REPAIR_PATCH:
                    tools = self.create_agent_tools("coder")
                    
                    if self.ctx.patch_review and self.ctx.patch_review.get("status") != "APPROVE":
                        print(f"[DEBUG] Repairing from Critic rejection...", file=sys.stderr)
                        patch2 = self.repair_patch(self.ctx, self.ctx.patch_review, tools)
                    else:
                        print(f"[DEBUG] Repairing from Test failure...", file=sys.stderr)
                        patch2 = self.repair_code_from_test(self.ctx, self.ctx.test_reports[-1], tools)
                    
                    self.ctx.patches.append(patch2)
                    self.state_manager.save_artifact(f"patch_repair_{len(self.ctx.patches)}", patch2)
                    self.state = State.PATCH_REVIEW
                
                # Save state after each step
                self.save_state()
                
            except Exception as e:
                print(f"[DEBUG] Error in state {self.state.name}: {e}", file=sys.stderr)
                import traceback
                traceback.print_exc()
                return 1
        
        # End of loop
        success = False
        if self.ctx.test_reports and self.ctx.test_reports[-1].get("success"):
            success = True
            print("[DEBUG] Workflow SUCCESS", file=sys.stderr)
            sys.stderr.write(json.dumps(self.ctx.test_reports[-1], ensure_ascii=False, indent=2) + "\n")
        else:
            print("[DEBUG] Workflow FAILED", file=sys.stderr)
            if self.ctx.test_reports:
                sys.stderr.write(json.dumps(self.ctx.test_reports[-1], ensure_ascii=False, indent=2) + "\n")
        
        return 0 if success else 1
    
    def repair_code_from_test(self, ctx: RunContext, test_report: Dict[str, Any], 
                             tools: PersistentTools) -> Dict[str, Any]:
        files = tools.list_files()
        user = (
            f"Objective: {ctx.packet.objective}\n\n"
            f"FROZEN SPEC: {json.dumps(ctx.frozen_spec, ensure_ascii=False)}\n\n"
            f"Previous PATCHES: {len(ctx.patches)}\n"
            f"Workspace files: {files}\n\n"
            f"TEST REPORT (FAILURE): {json.dumps(test_report, ensure_ascii=False)}\n\n"
            "Please fix the code to satisfy the test report.\n"
            "Return PATCH JSON only."
        )
        patch = self.llm.chat_json(SYSTEM_CODER_ENHANCED, user, temperature=0.2)
        assert_type(patch, "PATCH")
        return patch

    def repair_spec(self, ctx: RunContext, review: Dict[str, Any], 
                   tools: PersistentTools) -> Dict[str, Any]:
        user = (
            f"Objective: {ctx.packet.objective}\n\n"
            f"Previous SPEC: {json.dumps(ctx.frozen_spec, ensure_ascii=False)}\n\n"
            f"Critic review: {json.dumps(review, ensure_ascii=False)}\n\n"
            "Return corrected SPECIFICATION JSON only."
        )
        spec2 = self.llm.chat_json(SYSTEM_ARCHITECT, user, temperature=0.2)
        assert_type(spec2, "SPECIFICATION")
        return spec2

    def repair_patch(self, ctx: RunContext, review: Dict[str, Any], 
                    tools: PersistentTools) -> Dict[str, Any]:
        user = (
            f"Objective: {ctx.packet.objective}\n\n"
            f"FROZEN SPEC: {json.dumps(ctx.frozen_spec, ensure_ascii=False)}\n\n"
            f"Previous PATCH: {json.dumps(ctx.patches[-1], ensure_ascii=False)}\n\n"
            f"Critic review: {json.dumps(review, ensure_ascii=False)}\n\n"
            "Return corrected PATCH JSON only."
        )
        patch2 = self.llm.chat_json(SYSTEM_CODER_ENHANCED, user, temperature=0.2)
        assert_type(patch2, "PATCH")
        return patch2


# -----------------------------
# JSON gates (same as before)
# -----------------------------

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

def parse_single_json_object(text: str) -> Dict[str, Any]:
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
        raise RuntimeError(f"Invalid JSON: {e}")

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


# -----------------------------
# Entrypoint
# -----------------------------

def main() -> None:
    if len(sys.argv) < 2:
        sys.stderr.write("Usage: python generic_llm_orchestrator.py \"<objective>\"\n")
        raise SystemExit(2)

    objective = sys.argv[1]

    # Optional allowlist via env FILES_ALLOWED="a.py,b.py"
    allow = os.environ.get("FILES_ALLOWED", "").strip()
    files_allowed: Tuple[str, ...] = tuple([p.strip() for p in allow.split(",") if p.strip()])

    # Task ID for persistence
    task_id = os.environ.get("TASK_ID", f"task_{int(time.time())}")
    
    # Check if we're resuming
    resume = os.environ.get("RESUME", "").lower() in ("1", "true", "yes")
    
    if resume:
        # Find the latest task directory
        base_dir = os.path.abspath("sample_project")
        if not os.path.exists(base_dir):
            os.makedirs(base_dir, exist_ok=True)
        
        # Look for existing task directories
        task_dirs = []
        for item in os.listdir(base_dir):
            item_path = os.path.join(base_dir, item)
            if os.path.isdir(item_path) and item.startswith("task_"):
                task_dirs.append(item_path)
        
        if not task_dirs:
            print("[DEBUG] No task directories found to resume", file=sys.stderr)
            resume = False
        else:
            # Use the most recent task directory
            task_dirs.sort(key=lambda x: os.path.getmtime(x), reverse=True)
            workspace_dir = task_dirs[0]
            print(f"[DEBUG] Resuming from {workspace_dir}", file=sys.stderr)
    else:
        # Create new workspace
        base_dir = os.path.abspath("sample_project")
        if not os.path.exists(base_dir):
            os.makedirs(base_dir, exist_ok=True)
        
        workspace_dir = os.path.join(base_dir, task_id)
        os.makedirs(workspace_dir, exist_ok=True)

    # Setup logs
    logs_dir = os.path.abspath("logs")
    os.makedirs(logs_dir, exist_ok=True)
    log_file = os.path.join(logs_dir, f"trace_{int(time.time())}.log")

    packet = TaskPacket(
        objective=objective,
        workspace_dir=workspace_dir,
        files_allowed=files_allowed,
        task_id=task_id
    )

    llm = LLMClient(log_path=log_file)
    
    if resume:
        # Try to resume existing workflow
        orchestrator = PersistentOrchestrator(llm, workspace_dir, task_id)
        code = orchestrator.run()
    else:
        # Start new workflow
        orchestrator = PersistentOrchestrator(llm, workspace_dir, task_id)
        code = orchestrator.run(packet)
    
    raise SystemExit(code)


if __name__ == "__main__":
    main()