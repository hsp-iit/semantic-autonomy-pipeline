# SPDX-FileCopyrightText: 2026 Humanoid Sensing and Perception, Istituto Italiano di Tecnologia
# SPDX-License-Identifier: BSD-3-Clause
"""
The voice of the robot: an MCP host (yarp-mcpHosts) whose only server is the mission server.

Pipeline:
1. LlmChatInputMode serves chat_agent/user_text. The LLM is a small model, on Azure or on an Ollama server, and
   its tools only hand work to the mission server, so a turn ends quickly and the person can always talk to it.
2. The LLM decides when a message means to give up, and calls stop_all. Only a bare command ("stop") calls it on
   the ROS thread of the request, with no LLM: it has nothing to interpret, and it must work while the host is
   blocked on a slow or lost LLM server.
3. The host follows each mission as an operation. When one ends, the chatter runs one more LLM turn to tell the
   person, or reads out the report of the orchestrator if the LLM is not available. A stopped mission needs
   neither: the stop already answered.
"""

import asyncio
import re
import signal
import threading
from pathlib import Path
from typing import Any, Dict, List

import rclpy
from mcp import Client
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.node import Node
from rclpy.signals import SignalHandlerOptions
from yarp_mcpServer_core import yarp
from yarp_mcpHost_core.cores.mcp_server_sources import ServerSpec, StaticServerSource
from yarp_mcpHost_core.llm_backends.llm_backend_azure import AzureOpenAIBackend
from yarp_mcpHost_core.llm_backends.llm_backend_ollama import OllamaBackend
from yarp_mcpHost_core.llm_backends.llm_backend_openai_compatible import OpenAICompatibleBackend
from yarp_mcpHost_generalPurpose.cores.Yarp_mcpHost_GeneralCheckerCore import Yarp_mcpHost_GeneralCheckerCore

from semantic_autonomy.llmchat_input_mode import LlmChatInputMode

# Only a bare command: anything longer may be a request ("stop at the table and take a photo"), and the LLM decides
STOP_COMMAND = re.compile(r"^\W*(please\W+)?(robot\W+)?(stop|halt|freeze|wait|ferma|fermati|basta|alt)"
                          r"(\W+(now|please|robot|subito|ora))*\W*$", re.IGNORECASE)
NEW_CONVERSATION = re.compile(r"^\s*(new conversation|reset conversation|nuova conversazione)\s*$", re.IGNORECASE)
ALLOWED_TOOLS = {'start_mission', 'stop_all', 'get_tracked_operation', 'list_tracked_operations'}
BACKENDS = {'azure': AzureOpenAIBackend, 'ollama': OllamaBackend, 'openai-compat': OpenAICompatibleBackend}
LLM_ERROR_PREFIX = "Error communicating with LLM"   # how McpHost_BaseCore reports a failed LLM call
LLM_UNAVAILABLE = "Sorry, I cannot think right now, but you can still tell me to stop."
DEFAULT_PROMPT = str(Path(__file__).resolve().parents[2] / 'agents' / 'chatter' / 'prompt.md')
STOP_TIMEOUT = 15.0  # [s], longer than the time stop_all gives to each server


class MissionControl:
    """Calls to the mission server that must not depend on the host: its own event loop, thread and client."""

    def __init__(self, url):
        self.url = url
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    async def _call(self, tool):
        async with Client(self.url) as client:
            result = await client.call_tool(tool, {})
            if result.is_error:
                raise RuntimeError(" ".join(item.text for item in result.content if hasattr(item, 'text')))
            return result.structured_content

    def call(self, tool, timeout):
        return asyncio.run_coroutine_threadsafe(self._call(tool), self.loop).result(timeout=timeout)


class Chatter(Yarp_mcpHost_GeneralCheckerCore):
    def __init__(self, node, backend, mission_url, prompt_file, service_name, output_topic, turn_timeout):
        self.node = node
        self.mission = MissionControl(mission_url)
        input_mode = LlmChatInputMode(node, service_name, output_topic, turn_timeout, self.intercept, LLM_UNAVAILABLE)
        super().__init__(input_mode, backend, custom_prompt_file=prompt_file,
                         server_source=StaticServerSource([ServerSpec('mission', mission_url)]))
        if not self.custom_prompt_text:
            raise RuntimeError(f"No chatter prompt in {prompt_file}")

    def _build_system_prompt(self) -> str:
        # The default one repeats the tools and adds rules about YARP: a small model does better with less
        return self.custom_prompt_text

    def get_available_tools(self) -> List[Dict[str, Any]]:
        return [tool for tool in super().get_available_tools() if tool["function"]["name"] in ALLOWED_TOOLS]

    async def _handle_tool_call(self, tool_call: Any) -> Dict[str, Any]:
        # A model can call a tool it was not offered
        if tool_call.function.name not in ALLOWED_TOOLS:
            return {"success": False, "error": f"Unknown tool '{tool_call.function.name}'"}
        return await super()._handle_tool_call(tool_call)

    async def process_user_message(self, user_input: str) -> str:
        response = await super().process_user_message(user_input)
        if response and response.startswith(LLM_ERROR_PREFIX):
            self.node.get_logger().error(response)
            return LLM_UNAVAILABLE
        return response

    async def _deliver_operation_events(self) -> None:
        while True:
            event = await self._operation_events.get()
            try:
                if event is None:
                    return
                await self._deliver_outcome(event[0])
            except Exception as ex:
                self.node.get_logger().error(f"Could not deliver a mission outcome: {ex}")
            finally:
                self._operation_events.task_done()

    async def _deliver_outcome(self, operation) -> None:
        snapshot = operation.snapshot
        async with self._conversation_lock:
            if operation.status == "cancelled":
                self.conversation_history.append({"role": "system", "content": "[mission update] The mission was stopped."})
                return
            if operation.status == "completed":
                report = (snapshot.get("result") or {}).get("answer", "")
                update = f"[mission update] The mission ended. Its report: {report}"
            else:
                report = "I could not finish that."
                update = f"[mission update] The mission failed: {(snapshot.get('error') or {}).get('message')}"
            answer = await self.process_user_message(update)
        # An outcome reaches the person even without the LLM
        self.input_mode.announce(report if answer == LLM_UNAVAILABLE or not answer else answer)

    def intercept(self, text):
        """What needs no LLM: runs on a ROS thread, while the host may be blocked."""
        if STOP_COMMAND.match(text):
            return self.stop_robot()
        if NEW_CONVERSATION.match(text):
            return self.new_conversation()
        return None

    def stop_robot(self):
        """Stops mission and robot, and returns what to tell the person. Never says stopped without a confirmation."""
        try:
            result = self.mission.call('stop_all', STOP_TIMEOUT)
            if result["stopped"]:
                return "Stopped."
            error = "; ".join(result["errors"])
        except Exception as ex:
            error = f"{type(ex).__name__}: {ex}"
        self.node.get_logger().error(f"Could not stop the robot: {error}")
        return "I could not confirm the stop. Use the emergency stop if the robot is moving."

    def new_conversation(self):
        try:
            self.mission.call('new_conversation', 5.0)
        except Exception as ex:
            self.node.get_logger().warning(f"Could not start a new conversation: {ex}")
            return "A mission is still running."

        async def clear():
            async with self._conversation_lock:
                self.conversation_history.clear()
        asyncio.run_coroutine_threadsafe(clear(), self.input_mode.loop).result(timeout=30.0)
        return "Ok, starting a new conversation."


async def wait_for_server(node, url):
    """The host connects once, at the start: without the mission server it would have no tools for good."""
    while True:
        try:
            async with Client(url):
                return
        except Exception:
            node.get_logger().warning(f"Waiting for the mission server at {url}", throttle_duration_sec=10.0)
            await asyncio.sleep(1.0)


def make_backend(name, url, model, max_tokens):
    if name not in BACKENDS:
        raise ValueError(f"backend must be one of {sorted(BACKENDS)}")
    config = yarp.Property()
    config.put('backend-url', url)
    config.put('backend-model', model)
    config.put('backend-max-tokens', max_tokens)
    backend = BACKENDS[name]()
    if not backend.checkConfig(config):
        raise ValueError(f"Incomplete configuration for the '{name}' backend")
    return backend


async def run(node, chatter, backend, timeout, url):
    await wait_for_server(node, url)
    await backend.initialize()
    # The default is 10 minutes with retries: a lost server would hang the chat
    backend.client = backend.client.with_options(timeout=timeout, max_retries=0)
    await chatter.run_loop()


def on_signal(_signum, _frame):
    raise KeyboardInterrupt


def main():
    # Explicit: as a background job of a script SIGINT starts ignored, and SIGTERM would skip the stop at the end
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
    node = Node('chatter')
    node.declare_parameters(
        namespace='',
        parameters=[
            ('user_chat_service', 'chat_agent/user_text'),
            ('output_topic', 'llm_reasoning'),
            ('mcp_url', 'http://127.0.0.1:8767/mcp'),   # mission MCP server
            ('prompt_file', DEFAULT_PROMPT),
            ('backend', 'azure'),                       # azure, ollama, openai-compat
            ('backend_url', ''),                        # ollama and openai-compat, for example http://<host>:11434/v1
            ('backend_model', ''),                      # ollama and openai-compat. On Azure: DEPLOYMENT_ID
            ('max_tokens', 500),
            ('llm_timeout', 12.0),                      # s, one LLM call. A turn with a tool makes two of them
            ('turn_timeout', 28.0),                     # s, below the 30 s of the web chat
        ])
    p = lambda name: node.get_parameter(name).value  # noqa: E731
    backend = make_backend(p('backend'), p('backend_url'), p('backend_model'), p('max_tokens'))
    chatter = Chatter(node, backend, p('mcp_url'), p('prompt_file'), p('user_chat_service'), p('output_topic'),
                      p('turn_timeout'))
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    threading.Thread(target=executor.spin, daemon=True).start()
    try:
        asyncio.run(run(node, chatter, backend, p('llm_timeout'), p('mcp_url')))
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        # Leaving the chat must not leave the robot working with nobody to talk to
        chatter.stop_robot()
        executor.shutdown()
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == '__main__':
    main()
