import os
import glob
import json
import sqlite3
import sys
from typing import List, Dict, Any, Optional

from .tools import PersistentTools

class GateResult:
    def __init__(self, passed: bool, reason: str, evidence: Dict[str, Any]):
        self.passed = passed
        self.reason = reason
        self.evidence = evidence

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "reason": self.reason,
            "evidence": self.evidence
        }

class GateRunner:
    def __init__(self, tools: PersistentTools):
        self.tools = tools

    def check_files_exist(self, paths: List[str]) -> GateResult:
        missing = []
        for p in paths:
            if not self.tools.file_exists(p):
                missing.append(p)
        
        if missing:
            return GateResult(False, f"Missing required files: {missing}", {"missing": missing})
        return GateResult(True, "All required files exist", {"checked": paths})

    def check_python_syntax(self, files: List[str]) -> GateResult:
        failed = []
        for f in files:
            if not f.endswith(".py"):
                continue
            res = self.tools.run_shell_structured(f"python3 -m py_compile {f}")
            if res["exit_code"] != 0:
                failed.append({
                    "file": f,
                    "stderr": res["stderr"]
                })
        
        if failed:
            return GateResult(False, "Python syntax errors detected", {"failures": failed})
        return GateResult(True, "Python syntax check passed", {"checked": files})

    def check_command(self, cmd_spec: Dict[str, Any]) -> GateResult:
        cmd = cmd_spec.get("cmd")
        expect_code = cmd_spec.get("exit_code", 0)
        expect_substr = cmd_spec.get("substr")

        res = self.tools.run_shell_structured(cmd)
        
        if res["exit_code"] != expect_code:
            # Include tail of stderr/stdout
            return GateResult(False, f"Command failed with exit code {res['exit_code']} (expected {expect_code})", {
                "command": cmd,
                "exit_code": res["exit_code"],
                "stdout_tail": res["stdout"][-500:] if res["stdout"] else "",
                "stderr_tail": res["stderr"][-500:] if res["stderr"] else ""
            })
        
        if expect_substr:
            combined = (res["stdout"] + "\n" + res["stderr"])
            if expect_substr not in combined:
                 return GateResult(False, f"Command output missing required substring: '{expect_substr}'", {
                    "command": cmd,
                    "stdout_tail": res["stdout"][-500:]
                 })

        return GateResult(True, "Command passed checks", {"command": cmd, "exit_code": res["exit_code"]})

    def check_db_rows(self, min_rows: int = 1) -> GateResult:
        # Generic DB check: look for any .db file, if any table has rows
        import glob
        # We need to run this check OUTSIDE the agent, but effectively we are the orchestrator running it.
        # Since we have direct filesystem access, we can try to use python's sqlite3 directly OR run it via shell to be safe/sandboxed.
        # Running via shell is safer to match environment.
        
        py_script = (
            "import glob, sqlite3; "
            "dbs = glob.glob('*.db'); "
            "max_rows = 0; "
            "for db in dbs: "
            "  try: "
            "    c = sqlite3.connect(db).cursor(); "
            "    tables = c.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall(); "
            "    for t in tables: "
            "      c.execute(f'SELECT count(*) FROM {t[0]}'); "
            "      max_rows = max(max_rows, c.fetchone()[0]); "
            "  except: pass; "
            "print(f'MAX_ROWS={max_rows}')"
        )
        
        res = self.tools.run_shell_structured(f"python3 -c \"{py_script}\"")
        if res["exit_code"] != 0:
             return GateResult(False, "Failed to inspect DB files", {"error": res["stderr"]})
        
        import re
        m = re.search(r"MAX_ROWS=(\d+)", res["stdout"])
        if m:
            count = int(m.group(1))
            if count >= min_rows:
                return GateResult(True, f"Database has {count} rows (>= {min_rows})", {"rows": count})
            else:
                 return GateResult(False, f"Database has {count} rows (required {min_rows})", {"rows": count})
        
        return GateResult(False, "Could not determine row count", {"output": res["stdout"]})

    def run_apply_gates(self, patch_files: List[Any]) -> GateResult:
        # 1. Check existence
        paths = [f["path"] for f in patch_files if f["action"] == "write"]
        res = self.check_files_exist(paths)
        if not res.passed: return res

        # 2. Check syntax
        py_files = [p for p in paths if p.endswith(".py")]
        res = self.check_python_syntax(py_files)
        if not res.passed: return res
        
        return GateResult(True, "APPLY gates passed", {})

    def run_spec_gates(self, gates: Dict[str, Any]) -> List[GateResult]:
        results = []
        
        # Check 'must_exist'
        if "must_exist" in gates:
            results.append(self.check_files_exist(gates["must_exist"]))

        # Check 'must_run' / 'must_output_contains'
        if "must_run" in gates:
            for cmd in gates["must_run"]:
                # simple string or dict
                if isinstance(cmd, str):
                    c = {"cmd": cmd}
                else:
                    c = cmd
                results.append(self.check_command(c))
                
        if "must_output_contains" in gates:
             for item in gates["must_output_contains"]:
                 results.append(self.check_command(item))

        # Check 'min_db_rows'
        if "min_db_rows" in gates:
            results.append(self.check_db_rows(gates["min_db_rows"]))
            
        return results

def extract_gates_from_spec(spec: Dict[str, Any], objective: str) -> Dict[str, Any]:
    # If spec has explicit gates, use them
    if "gates" in spec and isinstance(spec["gates"], dict):
        return spec["gates"]
    
    # Otherwise, infer defaults
    gates = {
        "must_exist": [],
        "must_run": []
    }
    
    # 1. Infer files from verification plan? Hard to parse text.
    # 2. Use defaults:
    
    # If objective mentions daemon/database, add DB gate
    if any(k in objective.lower() for k in ["daemon", "server", "continuously", "database", "sqlite"]):
        gates["min_db_rows"] = 1
        
    return gates
