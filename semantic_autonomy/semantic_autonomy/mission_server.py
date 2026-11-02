# SPDX-FileCopyrightText: 2026 Humanoid Sensing and Perception, Istituto Italiano di Tecnologia
# SPDX-License-Identifier: BSD-3-Clause
"""
YARP MCP server that runs missions: one headless Claude Code orchestrator at a time.

Pipeline:
1. start_mission spawns `claude -p <request>` in agents/orchestrator and returns an operation, so the chatter
   never waits for the robot. The orchestrator session is resumed from mission to mission.
2. The process has its own process group, so that all of it can be killed at once.
3. stop_all kills the orchestrator first, because it could otherwise send a new goal, then cancels the
   approach and the navigation. Both are needed: the robot server only knows the goals it sent itself.
4. A mission that ends badly (timeout, crash) stops the robot too: nobody watches its goal any more.
"""

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError
from typing_extensions import TypedDict
from yarp_mcpServer_core import yarp
from yarp_mcpServer_core.lib_server.YARP_mcpServer_Operations import Yarp_mcpServer_Operations
from yarp_mcpServer_core.lib_server.mcp_tools import mcp_tool
from yarp_mcpServer_core.lib_server.operation_models import OperationSnapshot, StartOperationResult
from yarp_mcpServer_core.lib_server.result_models import SuccessResult

DEFAULT_AGENT_DIR = str(Path(__file__).resolve().parents[2] / 'agents' / 'orchestrator')
# Configuration keys (XML <param>), with their defaults. The value type gives the type read from YARP.
DEFAULT_PARAMS = {
    'agent_dir': DEFAULT_AGENT_DIR,     # Claude Code workspace with the orchestrator prompt
    'claude_bin': 'claude',
    'model': 'sonnet',
    'robot_mcp_url': 'http://127.0.0.1:8765/mcp',
    'approach_mcp_url': 'http://127.0.0.1:8766/mcp',
    'camera_mcp_url': 'http://127.0.0.1:8768/mcp',
    'mission_timeout': 900.0,           # [s]
}
# Settings files allow nothing for a headless turn: the permissions have to be on the command line.
# mcp__robot cannot be denied here, it would be denied to the navigator too.
ALLOWED_TOOLS = ['Agent', 'mcp__approach', 'mcp__camera', 'mcp__robot']
DISALLOWED_TOOLS = ['Bash', 'Edit', 'Write', 'NotebookEdit', 'Read', 'Glob', 'Grep', 'WebFetch', 'WebSearch']
STOP_TIMEOUT = 5.0  # [s], for each server


class StopAllResult(TypedDict):
    stopped: bool
    errors: list[str]


class Yarp_mcpServer_Mission(Yarp_mcpServer_Operations):
    """MCP server for the orchestrator. See the module docstring."""

    def __init__(self, conf=None):
        self.session_id = None
        self.process = None
        self._stopping = False
        self._cleaned_up = False
        self.params = dict(DEFAULT_PARAMS)
        if conf:
            conf.setDefault("server_name", "mission")
            conf.setDefault("mcp_port", 8767)
            for name, default in DEFAULT_PARAMS.items():
                if conf.check(name):
                    value = conf.find(name)
                    self.params[name] = (value.asString() if isinstance(default, str)
                                         else type(default)(value.asFloat64()))
        super().__init__(conf)

    def _initialize(self) -> bool:
        if not super()._initialize():
            return False
        self._start_info_port()
        return True

    async def cleanup(self) -> None:
        # Reached twice on a signal: from the launcher, then from the MCP lifespan
        if self._cleaned_up:
            return
        self._cleaned_up = True
        self._kill_process()
        await self._cleanup_operations()
        self._cleanup_base_resources()

    def _register_internal_tools(self) -> None:
        """Tools are declared as methods with @mcp_tool."""

    def _build_system_prompt_addendum(self) -> str:
        return (
            "MISSION SERVER INSTRUCTIONS:\n"
            "Runs what the user asks the robot to do, in the background, one mission at a time.\n"
            "- start_mission returns at once: the outcome is reported on a later turn.\n"
            "- stop_all stops the mission and the robot.")

    # ---------------------------------------------------------------- orchestrator process
    def _command(self, request):
        mcp_config = json.dumps({"mcpServers": {
            "robot": {"type": "http", "url": self.params['robot_mcp_url']},
            "approach": {"type": "http", "url": self.params['approach_mcp_url']},
            "camera": {"type": "http", "url": self.params['camera_mcp_url']}}})
        # As a system prompt: from CLAUDE.md alone it is background context, and the final check gets skipped
        cmd = [self.params['claude_bin'], '-p', request, '--output-format', 'json',
               '--system-prompt-file', os.path.join(self.params['agent_dir'], 'CLAUDE.md'),
               '--mcp-config', mcp_config, '--strict-mcp-config',
               '--allowedTools', *ALLOWED_TOOLS, '--disallowedTools', *DISALLOWED_TOOLS]
        if self.session_id:
            cmd += ['--resume', self.session_id]
        if self.params['model']:
            cmd += ['--model', self.params['model']]
        return cmd

    def _kill_process(self):
        process = self.process
        if process is None or process.returncode is not None:
            return False
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return False
        return True

    async def _stop_motion(self):
        """Cancel the approach and the navigation. Returns the errors of the servers that did not confirm."""
        async def call(url, tool):
            async with Client(url) as client:
                result = await client.call_tool(tool, {})
                if result.is_error:
                    raise RuntimeError(" ".join(item.text for item in result.content if hasattr(item, 'text')))

        async def stop(url, tool):
            try:
                await asyncio.wait_for(call(url, tool), STOP_TIMEOUT)
                return None
            except Exception as ex:
                return f"{tool}: {type(ex).__name__}: {ex}"

        results = await asyncio.gather(stop(self.params['approach_mcp_url'], 'cancel_approach'),
                                       stop(self.params['robot_mcp_url'], 'cancel_navigation'))
        return [error for error in results if error]

    async def _mission_loop(self, operation_id, request) -> None:
        """Run the orchestrator until it ends, and store its answer in the operation."""
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self._command(request), cwd=self.params['agent_dir'], start_new_session=True,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                stdout, stderr = await asyncio.wait_for(self.process.communicate(), self.params['mission_timeout'])
            except asyncio.TimeoutError:
                self._kill_process()
                await self.process.wait()
                raise RuntimeError(f"no result after {self.params['mission_timeout']:.0f} s") from None
            if self._stopping:      # stop_all commits the operation
                return
            try:
                out = json.loads(stdout)
            except json.JSONDecodeError:
                error = (stderr or stdout).decode(errors='replace')[-500:]
                raise RuntimeError(f"claude exited with {self.process.returncode}: {error}") from None
            if out.get("session_id"):
                self.session_id = out["session_id"]
            if out.get("is_error"):
                raise RuntimeError(f"claude turn failed: {out.get('result') or out.get('subtype')}")
            answer = (out.get("result") or "").strip()
            await self.operation_registry.update(
                operation_id, status="completed", progress=1.0, result={"answer": answer},
                status_message="Mission finished")
        except asyncio.CancelledError:
            self._kill_process()
            await self.operation_registry.update(
                operation_id, status="cancelled", status_message="Mission cancelled")
            raise
        except Exception as ex:
            self.log.error(f"Mission {operation_id} failed: {ex}")
            errors = await self._stop_motion()
            message = f"{ex}" + (f" (could not stop the robot: {'; '.join(errors)})" if errors else "")
            await self.operation_registry.update(
                operation_id, status="failed", error={"message": message}, status_message=message)

    async def _active_missions(self):
        return await self.operation_registry.list(active_only=True)

    async def _cancel_operation(self, operation_id: str) -> OperationSnapshot:
        """A mission has no safe half: cancelling it stops the robot too."""
        operation = await self.operation_registry.get(operation_id)
        if operation.status == "working":
            result = await self.stop_all()
            if result["errors"]:
                raise ToolError(f"The robot may still be moving: {'; '.join(result['errors'])}")
            return await self.operation_registry.get(operation_id)
        return operation

    # ---------------------------------------------------------------- tools
    @mcp_tool(structured_output=True)
    async def start_mission(self, request: str) -> StartOperationResult:
        """
        Hand what the user wants the robot to do to the orchestrator. Returns at once, with the mission
        running in the background. Refused while another mission runs.

        :param request: the request in plain words, with everything said so far that it depends on
        """
        if await self._active_missions():
            raise ToolError("A mission is already running: wait for it or call stop_all")
        self._stopping = False
        operation = await self.operation_registry.create(
            "mission", status_message="Mission running", details={"request": request}, poll_interval_ms=1000)
        await self._start_operation_task(operation, self._mission_loop(operation.operation_id, request))
        return StartOperationResult(
            operation_id=operation.operation_id, operation_type=operation.operation_type,
            status_uri=operation.status_uri, poll_interval_ms=operation.poll_interval_ms,
            message="Mission started")

    @mcp_tool()
    async def stop_all(self) -> StopAllResult:
        """Stop the running mission and the robot. stopped is false if a server did not confirm: see errors."""
        self._stopping = True
        self._kill_process()        # first, and before any await: it could send a new goal
        for operation in await self._active_missions():
            await self.operation_registry.cancel(operation.operation_id)
        errors = await self._stop_motion()
        return {"stopped": not errors, "errors": errors}

    @mcp_tool()
    async def new_conversation(self) -> SuccessResult:
        """Forget the orchestrator session, so the next mission starts with no memory of the previous ones."""
        if await self._active_missions():
            raise ToolError("A mission is running: stop it first")
        self.session_id = None
        return {"success": True}


def main():
    """Run this server alone. yarp_mcpServer_launcher is the usual way, see scripts/run_all.sh."""
    config = yarp.ResourceFinder()
    config.configure(sys.argv)
    Yarp_mcpServer_Mission(config).run()


if __name__ == '__main__':
    main()
