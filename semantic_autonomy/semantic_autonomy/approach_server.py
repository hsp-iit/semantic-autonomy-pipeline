# SPDX-FileCopyrightText: 2026 Humanoid Sensing and Perception, Istituto Italiano di Tecnologia
# SPDX-License-Identifier: BSD-3-Clause
"""
YARP MCP server that exposes the /reach_object action of manipulation-approach as a long-running operation.

Pipeline:
1. approach_object sends the ReachObject goal and returns as soon as it is accepted. A monitor task mirrors
   the goal into the OperationRegistry, as the robot server does for Nav2 goals.
2. /reach_object drives through the same Nav2 server as the navigator, where a new goal silently preempts
   the running one. So an approach is refused while the Nav2 status topic shows an active goal.
3. reached only means that Nav2 finished: the caller has to verify the object before a grasp.
"""

import asyncio
import sys
import threading
import time

import rclpy
from action_msgs.msg import GoalStatus, GoalStatusArray
from mcp.server.mcpserver.exceptions import ToolError
from rclpy.action import ActionClient
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.qos import qos_profile_action_status_default
from rclpy.signals import SignalHandlerOptions
from surface_detector_interfaces.action import ReachObject
from yarp_mcpServer_core import yarp
from yarp_mcpServer_core.lib_server.YARP_mcpServer_Operations import Yarp_mcpServer_Operations
from yarp_mcpServer_core.lib_server.mcp_tools import mcp_tool
from yarp_mcpServer_core.lib_server.operation_models import OperationSnapshot, StartOperationResult
from yarp_mcpServer_core.lib_server.operation_registry import OperationNotFoundError
from yarp_mcpServer_core.lib_server.result_models import SuccessResult
from yarp_mcpServer_ros2.runtime import Ros2NodeMixin

# Configuration keys (XML <param>), with their defaults. The value type gives the type read from YARP.
DEFAULT_PARAMS = {
    'reach_action': '/reach_object',
    'nav_status_topic': 'navigate_to_pose/_action/status',
}
ACTIVE_GOAL_STATUSES = (GoalStatus.STATUS_ACCEPTED, GoalStatus.STATUS_EXECUTING, GoalStatus.STATUS_CANCELING)


class ApproachBridge:
    """ROS side: one ReachObject goal at a time, and what the Nav2 server is doing."""

    def __init__(self, node, params):
        self.node = node
        self.client = ActionClient(node, ReachObject, params['reach_action'],
                                   callback_group=ReentrantCallbackGroup())
        self.lock = threading.Lock()
        self.count = 0
        self.state = None
        self.goal_handle = None
        self.nav_goals_active = 0
        self.status_sub = node.create_subscription(GoalStatusArray, params['nav_status_topic'], self._on_nav_status,
                                                   qos_profile_action_status_default)

    def _on_nav_status(self, msg):
        with self.lock:
            self.nav_goals_active = sum(s.status in ACTIVE_GOAL_STATUSES for s in msg.status_list)

    @staticmethod
    def _wait(future, timeout):
        done = threading.Event()
        future.add_done_callback(lambda _: done.set())
        if not done.wait(timeout):
            raise TimeoutError("timeout waiting for the action server")
        return future.result()

    def start(self, description):
        with self.lock:
            if self.goal_handle is not None:
                raise RuntimeError("an approach is already running")
            if self.nav_goals_active:
                raise RuntimeError("navigation is still running: wait for it to end or cancel it first")
        if not self.client.wait_for_server(timeout_sec=3.0):
            raise RuntimeError("/reach_object is not available (is the approach pipeline running?)")
        goal = ReachObject.Goal(object_string=description)
        handle = self._wait(self.client.send_goal_async(goal, feedback_callback=self._on_feedback), 10.0)
        if not handle.accepted:
            raise RuntimeError("/reach_object rejected the goal")
        with self.lock:
            self.count += 1
            self.goal_handle = handle
            self.state = {"id": self.count, "object": description, "status": "active", "error": "",
                          "started": time.time()}
            approach_id = self.count
        handle.get_result_async().add_done_callback(lambda fut, h=handle: self._on_result(h, fut))
        return approach_id

    def _on_feedback(self, msg):
        with self.lock:
            if self.state is not None:
                self.state["distance_remaining"] = round(float(msg.feedback.distance_remaining), 3)

    def _on_result(self, handle, future):
        result = future.result()
        with self.lock:
            if self.goal_handle is not handle:
                return
            self.goal_handle = None
            if result.status == GoalStatus.STATUS_SUCCEEDED and result.result.reached:
                self.state["status"] = "reached"
            elif result.status == GoalStatus.STATUS_CANCELED:
                self.state["status"] = "canceled"
            else:
                self.state["status"] = "failed"
            self.state["error"] = result.result.error_msg
            self.state["finished"] = time.time()

    def snapshot(self):
        with self.lock:
            state = dict(self.state)
        state["elapsed_s"] = round((state.pop("finished", None) or time.time()) - state.pop("started"))
        return state

    def cancel(self, wait=False):
        """Cancel the active goal. With wait, returns False if the action server did not confirm the cancel."""
        with self.lock:
            handle = self.goal_handle
        if handle is None:
            return True
        future = handle.cancel_goal_async()
        if wait:
            try:
                self._wait(future, 5.0)
            except TimeoutError:
                return False
        return True


class Yarp_mcpServer_Approach(Ros2NodeMixin, Yarp_mcpServer_Operations):
    """MCP server for the last stretch before a grasp. See the module docstring."""

    poll_interval = 0.25  # [s]

    def __init__(self, conf=None):
        self.bridge: ApproachBridge = None      # created in _initialize(), before tool calls are served
        self._operation_id = None
        self.node = None
        self._executor_thread = None
        self._owns_rclpy = False
        self.params = dict(DEFAULT_PARAMS)
        if conf:
            conf.setDefault("server_name", "approach")
            conf.setDefault("mcp_port", 8766)
            for name in DEFAULT_PARAMS:
                if conf.check(name):
                    self.params[name] = conf.find(name).asString()
        super().__init__(conf)

    def _initialize(self) -> bool:
        if not super()._initialize():
            return False
        # rclpy's own handlers would shut the context down on SIGINT, before cleanup() can cancel the goal
        if not rclpy.ok():
            rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
        self._initialize_ros()
        self._owns_rclpy = True
        self.bridge = ApproachBridge(self.node, self.params)
        self._start_info_port()
        return True

    async def cleanup(self) -> None:
        # Reached twice on a signal: from the launcher, then from the MCP lifespan
        if self.bridge is None:
            return
        # Stopping the server must not leave the robot driving to a goal nobody watches
        await asyncio.to_thread(self.bridge.cancel, True)
        await self._cleanup_operations()
        self.bridge = None
        self._cleanup_ros()
        self._cleanup_base_resources()

    def _register_internal_tools(self) -> None:
        """Tools are declared as methods with @mcp_tool."""

    def _build_system_prompt_addendum(self) -> str:
        return (
            "APPROACH SERVER INSTRUCTIONS:\n"
            "Closes the gap from where navigation stopped to a stance where the arms reach the object.\n"
            "- Call approach_object only when navigation has ended and the object is in view.\n"
            "- The object description is a visual text prompt for a detector, not a candidate id.\n"
            "- A completed approach only means the robot stopped: confirm the object afterwards.\n"
            "- The hands stay raised after an approach, also when it fails or is cancelled.")

    async def _run(self, fn, *args):
        try:
            return await asyncio.to_thread(fn, *args)
        except ToolError:
            raise
        except Exception as ex:
            raise ToolError(f"{type(ex).__name__}: {ex}") from ex

    async def _monitor_loop(self, operation_id) -> None:
        """Mirror the ReachObject goal into the operation until it ends."""
        last_distance = None
        try:
            while True:
                state = self.bridge.snapshot()
                status = state["status"]
                if status == "reached":
                    await self.operation_registry.update(
                        operation_id, status="completed", progress=1.0, details=state, result=state,
                        status_message="Approach finished")
                    return
                if status == "canceled":
                    await self.operation_registry.update(
                        operation_id, status="cancelled", details=state, status_message="Approach canceled")
                    return
                if status == "failed":
                    message = state["error"] or "The approach failed"
                    await self.operation_registry.update(
                        operation_id, status="failed", details=state, status_message=message,
                        error={"message": message})
                    return
                distance = state.get("distance_remaining")
                # Decimeter steps: every commit is an event for each subscribed host
                key = None if distance is None else round(distance * 10)
                if key != last_distance:
                    last_distance = key
                    await self.operation_registry.update(
                        operation_id, status="working", details=state,
                        status_message=f"Approaching {state['object']}")
                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            await self.operation_registry.update(
                operation_id, status="cancelled", status_message="Approach monitor cancelled")
            raise
        except Exception as ex:
            self.log.error(f"Approach monitor {operation_id} failed: {ex}")
            await self.operation_registry.update(
                operation_id, status="failed", error={"message": str(ex)},
                status_message=f"Approach monitor failed: {ex}")

    async def _stop_approach(self) -> None:
        if not await self._run(self.bridge.cancel, True):
            raise ToolError("/reach_object did not confirm the cancel: the robot may still be moving")

    async def _cancel_operation(self, operation_id: str) -> OperationSnapshot:
        """Stop the approach before cancelling the operation that monitors it."""
        operation = await self.operation_registry.get(operation_id)
        if operation.status == "working":
            await self._stop_approach()
        return await super()._cancel_operation(operation_id)

    @mcp_tool(structured_output=True)
    async def approach_object(self, object_description: str) -> StartOperationResult:
        """
        Walk up to an object on a surface, with the hands raised over it, and stop where the arms can reach
        it. Navigation must have ended and the object must be in view. Returns an operation as soon as the
        goal is accepted: wait_for_approach blocks until it ends.

        :param object_description: visual text prompt for the detector, for example "red mug". Not a candidate id.
        """
        await self._run(self.bridge.start, object_description)
        message = f"Approaching {object_description}"
        operation = await self.operation_registry.create(
            "approach", status_message=message, details={"object": object_description},
            poll_interval_ms=round(self.poll_interval * 1000))
        self._operation_id = operation.operation_id
        await self._start_operation_task(operation, self._monitor_loop(operation.operation_id))
        return StartOperationResult(
            operation_id=operation.operation_id, operation_type=operation.operation_type,
            status_uri=operation.status_uri, poll_interval_ms=operation.poll_interval_ms, message=message)

    @mcp_tool()
    async def wait_for_approach(self, timeout_s: float = 120.0) -> OperationSnapshot:
        """
        Block until the last approach ends or timeout_s passes, and return its operation. status is
        "completed", "failed" or "cancelled" when it ended; "working" means it is still running after the
        timeout: call this again or cancel_approach.
        """
        if self._operation_id is None:
            raise ToolError("No approach sent yet")
        deadline = time.monotonic() + timeout_s
        try:
            while True:
                operation = await self.operation_registry.get(self._operation_id)
                if operation.status != "working" or time.monotonic() >= deadline:
                    return operation
                await asyncio.sleep(0.2)
        except OperationNotFoundError as ex:
            raise ToolError(f"Unknown operation: {self._operation_id}") from ex

    @mcp_tool()
    async def cancel_approach(self) -> SuccessResult:
        """Stop the approach: cancels /reach_object, its navigation goal and any motion of the arms."""
        await self._stop_approach()
        for operation in await self.operation_registry.list(active_only=True):
            await self.operation_registry.cancel(operation.operation_id)
        return {"success": True}


def main():
    """Run this server alone. yarp_mcpServer_launcher is the usual way, see scripts/run_all.sh."""
    config = yarp.ResourceFinder()
    config.configure(sys.argv)
    Yarp_mcpServer_Approach(config).run()


if __name__ == '__main__':
    main()
