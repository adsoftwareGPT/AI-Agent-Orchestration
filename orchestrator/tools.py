import os
import re
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

from .state import StateManager
from .config import ALLOWED_COMMANDS, BLACKLIST_PATTERNS, MAX_FILE_LIST_LIMIT, MAX_FILE_READ_BYTES, SHELL_TIMEOUT, SHELL_BACKGROUND_TIMEOUT


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
        # if os.path.normpath(rel_path) == "main.py":
        #    raise RuntimeError("security violation: agents rarely need to touch main.py, and are restricted from doing so.")
        if self.files_allowed and rel_path not in self.files_allowed:
            raise RuntimeError(f"file not allowed: {rel_path}")

    def list_files(self, limit: int = MAX_FILE_LIST_LIMIT) -> List[str]:
        out: List[str] = []
        for root, _, files in os.walk(self.workspace_dir):
            for fn in files:
                ap = os.path.join(root, fn)
                rp = os.path.relpath(ap, self.workspace_dir)
                # if rp == "main.py":
                #    continue
                if rp.startswith(".agent_state"):
                    continue  # Skip state files
                out.append(rp)
        out.sort()
        return out[:limit]
    
    def file_exists(self, rel_path: str) -> bool:
        """Check if a file exists"""
        ap = self._abs(rel_path)
        return os.path.exists(ap)

    def read_text(self, rel_path: str, max_bytes: int = MAX_FILE_READ_BYTES) -> str:
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

        # 1. Check blacklist
        for pattern in BLACKLIST_PATTERNS:
            if re.search(pattern, command):
                return f"ERROR: Command blocked by blacklist pattern: {pattern}"

        # 2. Check whitelist (simple heuristic: first token)
        # Handle simple chaining like "ls -la | grep x" by checking the first command
        # This is not a perfect parser, just a basic safety net requested by user.
        parts = command.strip().split()
        if not parts:
            return "ERROR: Empty command"
        
        base_cmd = parts[0]
        # Allow running local scripts like "./script.py" or "python script.py"
        is_local_script = base_cmd.startswith("./") or base_cmd.endswith(".py") or base_cmd.endswith(".sh")
        
        if base_cmd not in ALLOWED_COMMANDS and not is_local_script:
             return f"ERROR: Command '{base_cmd}' not in allowed list."

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
                    out, _ = proc.communicate(timeout=SHELL_BACKGROUND_TIMEOUT)
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
                timeout=SHELL_TIMEOUT,
                text=True
            )
            return res.stdout or ""
        except subprocess.TimeoutExpired:
            return "ERROR: TimeoutExpired"
        except Exception as e:
            return f"ERROR: {e}"
