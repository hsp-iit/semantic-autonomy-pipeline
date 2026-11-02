# SPDX-FileCopyrightText: 2026 Humanoid Sensing and Perception, Istituto Italiano di Tecnologia
# SPDX-License-Identifier: BSD-3-Clause
"""
Input mode of the MCP host for the LlmChat service, which the web chat and the speech pipeline call.

Service callbacks run on ROS threads and the host loop on asyncio. A request waits in a queue for the host,
except for what `intercept` answers on the ROS thread: that keeps working while the host is busy with the LLM.
Every reply is also published on the output topic, which is where the web chat and the voice read it.
"""

import asyncio
import threading
from typing import Callable, Optional

from ros2_vlmaps_interfaces.srv import LlmChat
from rclpy.callback_groups import ReentrantCallbackGroup
from std_msgs.msg import String
from yarp_mcpHost_core.input_modes.input_mode_base import InputMode


class Turn:
    def __init__(self, text):
        self.text = text
        self.done = threading.Event()
        self.answer = ''
        self.abandoned = False


class LlmChatInputMode(InputMode):
    def __init__(self, node, service_name: str, output_topic: str, turn_timeout: float,
                 intercept: Callable[[str], Optional[str]], error_answer: str):
        super().__init__()
        self.node = node
        self.service_name = service_name
        self.output_topic = output_topic
        self.turn_timeout = turn_timeout
        self.intercept = intercept          # returns the answer to a message that needs no LLM, else None
        self.error_answer = error_answer    # the host answers this when the LLM failed
        self.loop = None
        self.queue = None
        self.current = None
        self.service = None
        self.publisher = None

    async def initialize(self):
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue()
        self.publisher = self.node.create_publisher(String, self.output_topic, 10)
        self.service = self.node.create_service(LlmChat, self.service_name, self.chat_callback,
                                                callback_group=ReentrantCallbackGroup())

    def announce(self, text):
        self.publisher.publish(String(data=text))

    def chat_callback(self, request: LlmChat.Request, response: LlmChat.Response):
        text = request.text.strip()
        self.node.get_logger().info(f"user: {text}")
        answer = self.intercept(text)
        if answer is not None:
            response.is_ok, response.error_msg, response.answer = True, '', answer
            self.announce(answer)
            return response
        if not text:
            response.is_ok, response.error_msg, response.answer = False, 'Empty message', ''
            return response
        turn = Turn(text)
        self.loop.call_soon_threadsafe(self.queue.put_nowait, turn)
        if not turn.done.wait(self.turn_timeout):
            turn.abandoned = True
            response.is_ok, response.error_msg = False, 'Timeout while waiting for the chat turn'
            response.answer = self.error_answer
            self.announce(response.answer)
            return response
        response.is_ok = turn.answer != self.error_answer
        response.error_msg = '' if response.is_ok else 'The LLM did not answer: see the chatter log'
        response.answer = turn.answer
        return response

    async def get_input(self) -> Optional[str]:
        while True:
            turn = await self.queue.get()
            if turn is None:
                return None
            if not turn.abandoned:
                self.current = turn
                return turn.text

    async def send_response(self, response: str):
        turn, self.current = self.current, None
        text = response or ''
        if turn is None or not turn.abandoned:      # an abandoned turn has already told the user
            self.announce(text)
        if turn is not None:
            turn.answer = text
            turn.done.set()

    async def cleanup(self):
        if self.service is not None:
            self.node.destroy_service(self.service)
        await self.queue.put(None)
