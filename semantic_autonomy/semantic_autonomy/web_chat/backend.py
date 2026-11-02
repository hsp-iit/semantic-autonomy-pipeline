# SPDX-FileCopyrightText: 2024 Humanoid Sensing and Perception, Istituto Italiano di Tecnologia
# SPDX-License-Identifier: BSD-3-Clause
# Author: Simone Micheletti

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String  # Using String messages for chat
from ros2_vlmaps_interfaces.srv import LlmChat
from flask import Flask, render_template
from flask_socketio import SocketIO
import base64
import threading

# Initialize Flask
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")
ros_node = None

class RosChatBackend(Node):
    def __init__(self):
        super().__init__("chat_backend_subscriber")
        self.subscription = self.create_subscription(
            String, "llm_reasoning", self.callback, 10
        )
        self.image_subscription = self.create_subscription(
            CompressedImage, "chat_image", self.image_callback, 10
        )
        self.chat_client = self.create_client(LlmChat, "/chat_agent/user_text")
        self.chat_client_lock = threading.Lock()

    def callback(self, msg : String):
        print(f"Received message: {msg.data}")
        socketio.emit("chat_message", {"message": msg.data})  # Send to frontend

    def image_callback(self, msg : CompressedImage):
        # index.html shows a message with this prefix as an image
        data = base64.b64encode(bytes(msg.data)).decode()
        socketio.emit("chat_message", {"message": f"robot_image:data:image/jpeg;base64,{data}"})

    def send_user_message(self, text: str):
        request = LlmChat.Request()
        request.text = text

        with self.chat_client_lock:
            if not self.chat_client.wait_for_service(timeout_sec=10.0):
                return False, "Service /chat_agent/user_text is not available"

            response = self.chat_client.call(request, timeout_sec=30.0)

        if response is None:
            return False, "Timeout while waiting for LLMChat response"
        if not response.is_ok:
            return False, response.error_msg
        return True, ""

def run_ros2_node():
    global ros_node
    rclpy.init()
    ros_node = RosChatBackend()
    rclpy.spin(ros_node)
    ros_node.destroy_node()
    rclpy.shutdown()

# Update the html page
@app.route("/")
def index():
    return render_template("index.html")


@socketio.on("user_message")
def handle_user_message(data):
    text = str(data.get("message", "")).strip() if data else ""
    if not text:
        return

    socketio.emit("request_status", {"loading": True})

    if ros_node is None:
        socketio.emit("chat_message", {"message": "robot: Backend is not connected to ROS2 yet."})
        socketio.emit("request_status", {"loading": False})
        return

    ok, error_msg = ros_node.send_user_message(text)
    if not ok:
        socketio.emit("chat_message", {"message": f"robot: {error_msg}"})
    socketio.emit("request_status", {"loading": False})

def main():
    # Start ROS 2 node in a separate process
    ros2_thread = threading.Thread(target=run_ros2_node, daemon=True)
    ros2_thread.start()

    # Start Flask app with WebSocket support
    # No debug mode: on 0.0.0.0 its debugger would run code for anyone on the network.
    # allow_unsafe_werkzeug: without it the server only starts from a terminal, not from a script or Docker.
    socketio.run(app, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
