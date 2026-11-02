# SPDX-FileCopyrightText: 2026 Humanoid Sensing and Perception, Istituto Italiano di Tecnologia
# SPDX-License-Identifier: BSD-3-Clause
"""
YARP MCP server for what the robot sees, used by the orchestrator and the navigator.

Pipeline:
1. It subscribes to the image topic of the camera, so it needs neither the map nor the navigation.
2. look returns the latest frame to the agent, as a JPEG. A frame older than max_image_age is refused: the agents
   use this tool to verify what happened, so an old image would be worse than none.
3. Each frame returned is also published on chat_image_topic, for the web chat. With no chat open it goes nowhere.
   It has its own topic because the speech pipeline reads llm_reasoning aloud. Nothing is saved.
"""

import asyncio
import base64
import json
import os
import sys
import threading
import time

import cv2
import numpy as np
import rclpy
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent
from rclpy.qos import qos_profile_sensor_data
from rclpy.signals import SignalHandlerOptions
from sensor_msgs.msg import CompressedImage, Image
from yarp_mcpServer_core import yarp
from yarp_mcpServer_core.lib_server.YARP_mcpServer_Base import Yarp_mcpServer_Base
from yarp_mcpServer_core.lib_server.mcp_tools import mcp_tool
from yarp_mcpServer_ros2.runtime import Ros2NodeMixin

ROBOT_IMAGE_TOPICS = {
    'ergocub': '/camera/rgbd/img',
    'r1': '/camera/rgbd/color/image_raw',
}
# Configuration keys (XML <param>), with their defaults. The value type gives the type read from YARP.
DEFAULT_PARAMS = {
    'image_topic': '',                  # empty = pick from YARP_ROBOT_NAME
    'chat_image_topic': 'chat_image',
    'camera_max_width': 960,            # px, images are downscaled before being sent to the LLM
    'max_image_age': 1.0,               # s
}


def encode_jpeg(msg, max_width):
    """JPEG of a sensor_msgs/Image. Returns the bytes, the width and the height."""
    h, w = int(msg.height), int(msg.width)
    raw = np.frombuffer(msg.data, dtype=np.uint8)
    if h <= 0 or w <= 0 or raw.size % (h * w) != 0:
        raise RuntimeError("image not available: invalid buffer")
    img = raw.reshape(h, w, raw.size // (h * w))
    if img.shape[2] == 1:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    elif str(msg.encoding).lower().startswith('rgb'):
        img = cv2.cvtColor(img[:, :, :3], cv2.COLOR_RGB2BGR)
    else:
        img = img[:, :, :3]
    if w > max_width:
        img = cv2.resize(img, (max_width, int(h * max_width / w)))
    ok, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        raise RuntimeError("jpeg encoding failed")
    return buf.tobytes(), img.shape[1], img.shape[0]


class CameraBridge:
    """ROS side: the latest frame, and the frames for the chat."""

    def __init__(self, node, params):
        self.node = node
        self.max_width = params['camera_max_width']
        self.max_age = params['max_image_age']
        robot_name = os.environ.get('YARP_ROBOT_NAME', '').lower() or 'ergocub'
        self.image_topic = params['image_topic'] or next(
            (topic for key, topic in ROBOT_IMAGE_TOPICS.items() if key in robot_name), ROBOT_IMAGE_TOPICS['ergocub'])
        self.lock = threading.Lock()
        self.latest = None
        self.received = 0.0
        self.image_sub = node.create_subscription(Image, self.image_topic, self._on_image, qos_profile_sensor_data)
        self.chat_pub = node.create_publisher(CompressedImage, params['chat_image_topic'], 10)
        node.get_logger().info(f"Reading the camera from {self.image_topic}")

    def _on_image(self, msg):
        with self.lock:
            self.latest = msg
            self.received = time.monotonic()    # the camera clock may not match this one

    def look(self):
        with self.lock:
            msg, received = self.latest, self.received
        if msg is None:
            raise RuntimeError(f"image not available: no frame received on {self.image_topic}")
        age = time.monotonic() - received
        if age > self.max_age:
            raise RuntimeError(f"image not available: the last frame on {self.image_topic} is {age:.1f} s old")
        jpeg, width, height = encode_jpeg(msg, self.max_width)
        self.chat_pub.publish(CompressedImage(header=msg.header, format='jpeg', data=jpeg))
        return jpeg, width, height


class Yarp_mcpServer_Camera(Ros2NodeMixin, Yarp_mcpServer_Base):
    """MCP server for the robot camera. See the module docstring."""

    def __init__(self, conf=None):
        self.bridge: CameraBridge = None    # created in _initialize(), before tool calls are served
        self.node = None
        self._executor_thread = None
        self._owns_rclpy = False
        self.params = dict(DEFAULT_PARAMS)
        if conf:
            conf.setDefault("server_name", "camera")
            conf.setDefault("mcp_port", 8768)
            for name, default in DEFAULT_PARAMS.items():
                if conf.check(name):
                    value = conf.find(name)
                    self.params[name] = (value.asString() if isinstance(default, str)
                                         else type(default)(value.asFloat64()))
        super().__init__(conf)

    def _initialize(self) -> bool:
        if not super()._initialize():
            return False
        if not rclpy.ok():
            rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
        self._initialize_ros()
        self._owns_rclpy = True
        self.bridge = CameraBridge(self.node, self.params)
        self._start_info_port()
        return True

    async def cleanup(self) -> None:
        # Reached twice on a signal: from the launcher, then from the MCP lifespan
        if self.bridge is None:
            return
        self.bridge = None
        self._cleanup_ros()
        self._cleanup_base_resources()

    def _register_common_tools(self) -> None:
        """No common tools: this server has no long-running work."""

    def _register_internal_tools(self) -> None:
        """Tools are declared as methods with @mcp_tool."""

    def _build_system_prompt_addendum(self) -> str:
        return ("CAMERA SERVER INSTRUCTIONS:\n"
                "look returns what the robot sees now, and shows it in the chat if one is open.")

    @mcp_tool()
    async def look(self) -> CallToolResult:
        """Current RGB image from the robot camera, also shown in the chat if one is open. Use it to take a photo, to visually confirm an object or to describe the scene."""
        try:
            jpeg, width, height = await asyncio.to_thread(self.bridge.look)
        except Exception as ex:
            raise ToolError(f"{ex}") from ex
        info = {"width": width, "height": height, "image_index": 1}
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(info)),
                     ImageContent(type="image", data=base64.b64encode(jpeg).decode(), mime_type="image/jpeg")],
            structured_content=info)


def main():
    """Run this server alone. yarp_mcpServer_launcher is the usual way, see scripts/run_all.sh."""
    config = yarp.ResourceFinder()
    config.configure(sys.argv)
    Yarp_mcpServer_Camera(config).run()


if __name__ == '__main__':
    main()
