"""symphony/worker.py — Claude Code CLI subprocess runner."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import dataclass

from .config import WorkflowConfig  # VENDOR-PATCH: relative import for vendoring

log = logging.getLogger("symphony")


class WorkerError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass
class WorkerResult:
    success: bool
    output: str
    error: str | None = None
    exit_code: int = 0


class Worker:
    def __init__(self, config: WorkflowConfig):
        self.config = config

    def _build_mcp_config(self, issue_skills: list[str]) -> dict | None:
        """Build MCP config dict from workflow config and issue skills."""
        servers = {}
        for mcp in self.config.mcp_servers:
            name = mcp.get("name", "")
            command = mcp.get("command", "")
            if name and command:
                parts = command.split()
                servers[name] = {
                    "command": parts[0],
                    "args": parts[1:] if len(parts) > 1 else [],
                }
                if mcp.get("env"):
                    servers[name]["env"] = mcp["env"]
        if not servers:
            return None
        return {"mcpServers": servers}

    def _build_claude_args(
        self,
        prompt: str,
        cwd: str,
        issue_skills: list[str],
    ) -> list[str]:
        """Build the claude CLI argument list."""
        args = [
            self.config.agent_command,
            "-p", prompt,
            "--output-format", "json",
        ]

        if self.config.permission_mode:
            mode = self.config.permission_mode
            if mode == "acceptEdits":
                args.extend(["--permission-mode", "acceptEdits"])
            elif mode == "bypassPermissions":
                args.extend(["--dangerously-skip-permissions"])

        return args

    async def run_turn(
        self,
        prompt: str,
        cwd: str,
        issue_skills: list[str] | None = None,
        timeout_ms: int = 3600000,
    ) -> WorkerResult:
        """Run a single Claude Code turn."""
        issue_skills = issue_skills or []
        args = self._build_claude_args(prompt, cwd, issue_skills)

        # Write MCP config to temp file if needed
        mcp_config = self._build_mcp_config(issue_skills)
        mcp_config_path = None
        if mcp_config:
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, prefix="symphony_mcp_"
            )
            json.dump(mcp_config, tmp)
            tmp.close()
            mcp_config_path = tmp.name
            args.extend(["--mcp-config", mcp_config_path])

        log.info(f"worker: launching {' '.join(args[:4])}... in {cwd}")

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            timeout_s = max(timeout_ms / 1000, 1)
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_s
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return WorkerResult(
                    success=False,
                    output="",
                    error=f"Turn timed out after {timeout_ms}ms",
                    exit_code=-1,
                )

            stdout_str = stdout.decode()
            stderr_str = stderr.decode()

            # Try to parse JSON output
            output_text = stdout_str
            try:
                parsed = json.loads(stdout_str)
                if isinstance(parsed, dict):
                    output_text = parsed.get("result", stdout_str)
                    if parsed.get("is_error"):
                        return WorkerResult(
                            success=False,
                            output=output_text,
                            error=output_text,
                            exit_code=proc.returncode or 1,
                        )
            except (json.JSONDecodeError, ValueError):
                pass

            if proc.returncode != 0:
                return WorkerResult(
                    success=False,
                    output=output_text,
                    error=stderr_str or f"Exit code {proc.returncode}",
                    exit_code=proc.returncode,
                )

            return WorkerResult(
                success=True,
                output=output_text,
                exit_code=0,
            )

        except FileNotFoundError:
            return WorkerResult(
                success=False,
                output="",
                error=f"Command not found: {self.config.agent_command}",
                exit_code=-1,
            )
        finally:
            if mcp_config_path:
                try:
                    os.unlink(mcp_config_path)
                except OSError:
                    pass
