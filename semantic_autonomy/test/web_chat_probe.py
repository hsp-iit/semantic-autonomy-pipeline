# SPDX-FileCopyrightText: 2026 Humanoid Sensing and Perception, Istituto Italiano di Tecnologia
# SPDX-License-Identifier: BSD-3-Clause
"""Runs the web chat backend with a test client, and prints the first message that the page would show."""
import threading
import time

from semantic_autonomy.web_chat import backend

threading.Thread(target=backend.run_ros2_node, daemon=True).start()
page = backend.socketio.test_client(backend.app)
deadline = time.time() + 10.0
while time.time() < deadline:
    for event in page.get_received():
        if event['name'] == 'chat_message':
            print(event['args'][0]['message'], flush=True)
            raise SystemExit(0)
    time.sleep(0.05)
