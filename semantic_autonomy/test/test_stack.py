# SPDX-FileCopyrightText: 2026 Humanoid Sensing and Perception, Istituto Italiano di Tecnologia
# SPDX-License-Identifier: BSD-3-Clause
"""Integration test of the approach server, the mission server and the chatter. No LLM and no robot.

Each server runs in its own process, as in the real pipeline, with a scripted claude (fake_claude.py), a fake
/reach_object, a fake Nav2 status topic and a fake robot MCP server. Isolated from the rest of the system:
  - ROS domain AUTONOMY_TEST_DOMAIN_ID, default 85
  - a private YARP name server on 127.0.0.1, with its configuration in a temporary folder
  - MCP ports starting at AUTONOMY_TEST_PORT, default 18870
"""
import asyncio
import base64
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback

import cv2
import numpy as np

os.environ['ROS_DOMAIN_ID'] = os.environ.get('AUTONOMY_TEST_DOMAIN_ID', '85')
os.environ['ROS_AUTOMATIC_DISCOVERY_RANGE'] = 'LOCALHOST'
TMP = tempfile.mkdtemp(prefix='test_autonomy_')
os.environ['YARP_CONFIG_HOME'] = TMP
os.environ['YARP_DATA_HOME'] = TMP
os.environ['FAKE_CLAUDE_LOG'] = os.path.join(TMP, 'claude.log')
YARP_SOCKET = os.environ.get('AUTONOMY_TEST_YARP_SOCKET', '10323')
PORT = int(os.environ.get('AUTONOMY_TEST_PORT', '18870'))
ROBOT_URL, APPROACH_URL, MISSION_URL = (f'http://127.0.0.1:{PORT + i}/mcp' for i in range(3))
CAMERA_URL = f'http://127.0.0.1:{PORT + 4}/mcp'

import rclpy
from action_msgs.msg import GoalInfo, GoalStatus, GoalStatusArray
from mcp import Client
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import qos_profile_action_status_default
from ros2_vlmaps_interfaces.srv import LlmChat
from sensor_msgs.msg import CompressedImage, Image
from std_msgs.msg import String
from surface_detector_interfaces.action import ReachObject

from fake_llm import FakeLlm

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ALLOWED = ['get_tracked_operation', 'list_tracked_operations', 'start_mission', 'stop_all']
LLM_TIMEOUT = 2.0
FAKE_CLAUDE = os.path.join(TMP, 'claude')
ok_all = True


def check(name, cond, info=''):
    global ok_all
    ok_all &= bool(cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f'  ({info})' if info != '' and not cond else ''), flush=True)


def wait(cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return bool(cond())


class FakeRos(Node):
    """/reach_object, the Nav2 status topic, the chat client and the announcements."""
    def __init__(self):
        super().__init__('fake_ros')
        self.mode = 'hold'          # succeed, fail, hold
        self.goals = []
        self.cancelled = 0
        self.announced = []
        self.cb = ReentrantCallbackGroup()
        self.reach = ActionServer(self, ReachObject, '/reach_object', execute_callback=self.execute,
                                  goal_callback=self.on_goal, cancel_callback=lambda _: CancelResponse.ACCEPT,
                                  callback_group=self.cb)
        self.status_pub = self.create_publisher(GoalStatusArray, 'navigate_to_pose/_action/status',
                                                qos_profile_action_status_default)
        self.chat = self.create_client(LlmChat, 'chat_agent/user_text', callback_group=self.cb)
        self.create_subscription(String, 'llm_reasoning', lambda m: self.announced.append(m.data), 10,
                                 callback_group=self.cb)
        self.image_pub = self.create_publisher(Image, '/test_camera/img', 10)
        self.chat_images = []
        self.create_subscription(CompressedImage, 'chat_image', lambda m: self.chat_images.append(bytes(m.data)), 10,
                                 callback_group=self.cb)
        self.publish_nav(False)

    def publish_image(self, bgr, encoding='bgr8'):
        pixels = bgr[:, :, ::-1] if encoding == 'rgb8' else bgr
        channels = 1 if pixels.ndim == 2 else 3
        self.image_pub.publish(Image(height=pixels.shape[0], width=pixels.shape[1], encoding=encoding,
                                     step=pixels.shape[1] * channels, data=pixels.tobytes()))

    def on_goal(self, goal):
        self.goals.append(goal.object_string)
        return GoalResponse.ACCEPT

    def execute(self, gh):
        distance = 0.8
        while True:
            gh.publish_feedback(ReachObject.Feedback(distance_remaining=distance))
            time.sleep(0.05)
            if gh.is_cancel_requested:
                self.cancelled += 1
                gh.canceled()
                return ReachObject.Result(reached=False, error_msg='cancelled')
            if self.mode == 'succeed':
                gh.publish_feedback(ReachObject.Feedback(distance_remaining=0.0))
                gh.succeed()
                return ReachObject.Result(reached=True)
            if self.mode == 'fail':
                gh.abort()
                return ReachObject.Result(reached=False, error_msg='no surface found')

    def publish_nav(self, active):
        msg = GoalStatusArray()
        if active:
            msg.status_list.append(GoalStatus(goal_info=GoalInfo(), status=GoalStatus.STATUS_EXECUTING))
        self.status_pub.publish(msg)

    def say(self, text, timeout=20.0):
        """Calls the chat service, returns (reply, seconds)."""
        request = LlmChat.Request(text=text)
        start = time.time()
        future = self.chat.call_async(request)
        done = threading.Event()
        future.add_done_callback(lambda _: done.set())
        assert done.wait(timeout), f'no reply to {text}'
        return future.result(), time.time() - start


def run_fake_robot():
    stops = os.path.join(TMP, 'robot_stops')
    code = f"""
from mcp.server.mcpserver import MCPServer
mcp = MCPServer(name='robot')
@mcp.tool()
def cancel_navigation() -> dict:
    open({stops!r}, 'a').write('x')
    return {{'success': True}}
mcp.run(transport='streamable-http', host='127.0.0.1', port={PORT})
"""
    process = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return process, stops


def tool(url, name, args=None):
    async def call():
        async with Client(url) as client:
            return await client.call_tool(name, args or {})
    return asyncio.run(call())


def text_of(result):
    return ' '.join(item.text for item in result.content if hasattr(item, 'text'))


def operations(url):
    return tool(url, 'list_operations', {'active_only': False}).structured_content['operations']


def op_status(url, operation_id):
    return tool(url, 'get_operation_status', {'operation_id': operation_id}).structured_content['status']


def claude_calls():
    if not os.path.exists(os.environ['FAKE_CLAUDE_LOG']):
        return []
    return [json.loads(line) for line in open(os.environ['FAKE_CLAUDE_LOG'])]


def alive(pid):
    try:
        with open(f'/proc/{pid}/stat') as stat:
            return stat.read().rsplit(')', 1)[1].split()[0] != 'Z'
    except OSError:
        return False


def count(path):
    return os.path.getsize(path) if os.path.exists(path) else 0


def test_approach(ros):
    # ---- success
    ros.mode = 'hold'
    result = tool(APPROACH_URL, 'approach_object', {'object_description': 'red mug'})
    check('approach accepted', not result.is_error, text_of(result))
    operation_id = result.structured_content['operation_id']
    check('goal has the description', wait(lambda: ros.goals == ['red mug']), ros.goals)
    second = tool(APPROACH_URL, 'approach_object', {'object_description': 'cup'})
    check('second approach refused', second.is_error and 'already running' in text_of(second), text_of(second))
    still = tool(APPROACH_URL, 'wait_for_approach', {'timeout_s': 0.3}).structured_content
    check('wait times out as working', still['status'] == 'working', still)
    check('feedback mirrored', wait(lambda: tool(APPROACH_URL, 'get_operation_status', {'operation_id': operation_id})
                                    .structured_content['details'].get('distance_remaining') == 0.8))
    ros.mode = 'succeed'
    ended = tool(APPROACH_URL, 'wait_for_approach', {'timeout_s': 5.0}).structured_content
    check('reached is completed', ended['status'] == 'completed', ended)

    # ---- failure carries the reason
    ros.mode = 'fail'
    tool(APPROACH_URL, 'approach_object', {'object_description': 'red mug'})
    ended = tool(APPROACH_URL, 'wait_for_approach', {'timeout_s': 5.0}).structured_content
    check('failure carries the reason', ended['status'] == 'failed'
          and ended['error']['message'] == 'no surface found', ended)

    # ---- cancel
    ros.mode = 'hold'
    n = ros.cancelled
    tool(APPROACH_URL, 'approach_object', {'object_description': 'red mug'})
    result = tool(APPROACH_URL, 'cancel_approach')
    check('cancel confirmed', not result.is_error, text_of(result))
    check('goal cancelled on the action server', wait(lambda: ros.cancelled == n + 1))
    ended = tool(APPROACH_URL, 'wait_for_approach', {'timeout_s': 3.0}).structured_content
    check('cancel is cancelled', ended['status'] == 'cancelled', ended)
    check('cancel with nothing running is fine', not tool(APPROACH_URL, 'cancel_approach').is_error)

    # ---- Nav2 busy
    n = len(ros.goals)
    ros.publish_nav(True)
    time.sleep(0.4)
    result = tool(APPROACH_URL, 'approach_object', {'object_description': 'red mug'})
    check('refused during navigation', result.is_error and 'navigation is still running' in text_of(result), text_of(result))
    check('no goal sent during navigation', len(ros.goals) == n)
    ros.publish_nav(False)
    time.sleep(0.4)
    ros.mode = 'succeed'
    result = tool(APPROACH_URL, 'approach_object', {'object_description': 'red mug'})
    check('accepted after navigation', not result.is_error, text_of(result))
    tool(APPROACH_URL, 'wait_for_approach', {'timeout_s': 5.0})


def look_image(result):
    data = next(item.data for item in result.content if item.type == 'image')
    return cv2.imdecode(np.frombuffer(base64.b64decode(data), np.uint8), cv2.IMREAD_COLOR), base64.b64decode(data)


def test_camera(ros):
    result = tool(CAMERA_URL, 'look')
    check('no frame yet is reported', result.is_error and 'no frame received' in text_of(result), text_of(result))
    check('nothing for the chat', not ros.chat_images)

    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    frame[:, :, 2] = 200    # red in BGR
    time.sleep(0.5)         # the subscription of the server
    for encoding in ('bgr8', 'rgb8'):
        ros.publish_image(frame, encoding)
        time.sleep(0.2)
        result = tool(CAMERA_URL, 'look')
        check(f'{encoding} returned', not result.is_error, text_of(result))
        image, jpeg = look_image(result)
        check(f'{encoding} keeps its colors', image[..., 2].mean() > 150 and image[..., 0].mean() < 50, image.mean(axis=(0, 1)).tolist())
        check(f'{encoding} is downscaled', image.shape[:2] == (240, 320), image.shape)
    check('same frame for the chat', wait(lambda: len(ros.chat_images) == 2 and ros.chat_images[-1] == jpeg), len(ros.chat_images))
    ros.publish_image(np.full((48, 64), 120, dtype=np.uint8), 'mono8')
    time.sleep(0.2)
    image, _ = look_image(tool(CAMERA_URL, 'look'))
    check('mono8 returned', image.shape[:2] == (48, 64) and abs(image.mean() - 120) < 3, image.shape)
    time.sleep(0.2)
    n = len(ros.chat_images)
    time.sleep(0.8)
    result = tool(CAMERA_URL, 'look')
    check('an old frame is refused', result.is_error and 's old' in text_of(result), text_of(result))
    time.sleep(0.2)
    check('an old frame does not reach the chat', len(ros.chat_images) == n, len(ros.chat_images) - n)

    # ---- the web chat shows it
    probe = subprocess.Popen([sys.executable, os.path.join(HERE, 'web_chat_probe.py')], env=env(), stdout=subprocess.PIPE, text=True)
    deadline = time.time() + 12.0
    while time.time() < deadline and probe.poll() is None:
        ros.publish_image(frame)
        time.sleep(0.15)
        tool(CAMERA_URL, 'look')
    try:
        out = probe.communicate(timeout=3.0)[0]
    except subprocess.TimeoutExpired:
        probe.kill()
        out = probe.communicate()[0]
    check('the web chat gets the image', out.startswith('robot_image:data:image/jpeg;base64,'), out[:80])


def test_mission(ros, stops):
    # ---- result, session, refusal
    result = tool(MISSION_URL, 'start_mission', {'request': 'go there'})
    check('mission started', not result.is_error, text_of(result))
    first = result.structured_content['operation_id']
    check('mission completed', wait(lambda: op_status(MISSION_URL, first) == 'completed'))
    call = claude_calls()[-1]
    check('orchestrator workspace', call['cwd'].endswith('agents/orchestrator'), call['cwd'])
    argv = call['argv']
    check('all servers configured', set(json.loads(argv[argv.index('--mcp-config') + 1])['mcpServers']) == {'robot', 'approach', 'camera'})
    check('camera allowed', 'mcp__camera' in argv[argv.index('--allowedTools'):argv.index('--disallowedTools')])
    check('no session on the first mission', '--resume' not in argv)
    check('model passed', argv[argv.index('--model') + 1] == 'sonnet')
    check('prompt is the system prompt', argv[argv.index('--system-prompt-file') + 1].endswith('orchestrator/CLAUDE.md'))
    check('bash denied', 'Bash' in argv[argv.index('--disallowedTools'):])
    result = tool(MISSION_URL, 'start_mission', {'request': 'go back'})
    second = result.structured_content['operation_id']
    wait(lambda: op_status(MISSION_URL, second) == 'completed')
    check('session resumed', '--resume' in claude_calls()[-1]['argv'])
    check('new conversation', not tool(MISSION_URL, 'new_conversation').is_error)
    result = tool(MISSION_URL, 'start_mission', {'request': 'go again'})
    third = result.structured_content['operation_id']
    wait(lambda: op_status(MISSION_URL, third) == 'completed')
    check('session forgotten', '--resume' not in claude_calls()[-1]['argv'])

    # ---- one at a time
    result = tool(MISSION_URL, 'start_mission', {'request': 'sleep 60'})
    running = result.structured_content['operation_id']
    check('orchestrator running', wait(lambda: any('child' in c for c in claude_calls())))
    child = [c['child'] for c in claude_calls() if 'child' in c][-1]
    pid = [c['pid'] for c in claude_calls() if 'pid' in c][-1]
    result = tool(MISSION_URL, 'start_mission', {'request': 'go'})
    check('second mission refused', result.is_error and 'already running' in text_of(result), text_of(result))
    check('new conversation refused during a mission', tool(MISSION_URL, 'new_conversation').is_error)

    # ---- stop_all, with an approach running
    ros.mode = 'hold'
    tool(APPROACH_URL, 'approach_object', {'object_description': 'red mug'})
    n_robot, n_reach = count(stops), ros.cancelled
    start = time.time()
    result = tool(MISSION_URL, 'stop_all').structured_content
    took = time.time() - start
    check('stop_all confirmed', result == {'stopped': True, 'errors': []}, result)
    check('stop_all is quick', took < 1.0, took)
    check('orchestrator killed', wait(lambda: not alive(pid), 2.0))
    check('its child killed too', wait(lambda: not alive(child), 2.0))
    check('navigation cancelled', count(stops) == n_robot + 1)
    check('approach cancelled', wait(lambda: ros.cancelled == n_reach + 1))
    check('mission cancelled', op_status(MISSION_URL, running) == 'cancelled', op_status(MISSION_URL, running))

    # ---- cancelling a mission also stops the robot
    result = tool(MISSION_URL, 'start_mission', {'request': 'sleep 60'})
    running = result.structured_content['operation_id']
    n_children = len([c for c in claude_calls() if 'child' in c])
    wait(lambda: len([c for c in claude_calls() if 'child' in c]) >= n_children)
    n_robot = count(stops)
    result = tool(MISSION_URL, 'cancel_operation', {'operation_id': running})
    check('cancel_operation works', not result.is_error, text_of(result))
    check('cancel_operation stops the robot', count(stops) == n_robot + 1)

    # ---- crash
    n_robot = count(stops)
    result = tool(MISSION_URL, 'start_mission', {'request': 'crash'})
    crash = result.structured_content['operation_id']
    check('crash is failed', wait(lambda: op_status(MISSION_URL, crash) == 'failed'))
    snapshot = tool(MISSION_URL, 'get_operation_status', {'operation_id': crash}).structured_content
    check('crash reason kept', 'boom' in snapshot['error']['message'], snapshot)
    check('crash stops the robot', count(stops) == n_robot + 1, count(stops) - n_robot)


def test_timeout(ros, stops):
    server = start_mission_server(PORT + 3, timeout=1.0)
    url = f'http://127.0.0.1:{PORT + 3}/mcp'
    try:
        assert wait(lambda: reachable(url), 15.0), 'second mission server not up'
        n_robot = count(stops)
        result = tool(url, 'start_mission', {'request': 'sleep 60'})
        operation_id = result.structured_content['operation_id']
        check('timeout is failed', wait(lambda: op_status(url, operation_id) == 'failed'))
        pid = [c['pid'] for c in claude_calls() if 'pid' in c][-1]
        check('timeout kills the orchestrator', wait(lambda: not alive(pid), 2.0))
        check('timeout stops the robot', count(stops) == n_robot + 1, count(stops) - n_robot)
    finally:
        server.send_signal(signal.SIGINT)
        server.wait(timeout=10)


def llm_tools(llm):
    return sorted(llm.calls[-1]['tools'])


def test_chatter(ros, llm, stops):
    assert ros.chat.wait_for_service(20.0), 'chatter not up'

    # ---- plain chat: no tool, the real Ollama backend
    reply, _ = ros.say('hello there')
    check('chat is answered', reply.is_ok and reply.answer == 'reply: hello there', reply)
    check('prompt is only the chatter prompt', llm.calls[-1]['system'].startswith('You are the voice of') and 'YARP' not in llm.calls[-1]['system'])
    check('only the allowed tools are offered', llm_tools(llm) == ALLOWED, llm_tools(llm))
    check('answer published', wait(lambda: 'reply: hello there' in ros.announced), ros.announced)
    n = len(ros.announced)
    reply, _ = ros.say('how are you')
    check('history kept', any(m.get('content') == 'hello there' for m in llm.calls[-1]['messages']))
    check('each answer published once', wait(lambda: len(ros.announced) == n + 1), ros.announced)

    # ---- a tool it was not offered
    reply, _ = ros.say('hidden')
    result = next(m for m in llm.calls[-1]['messages'][::-1] if m.get('role') == 'tool')
    check('a hidden tool is refused', 'Unknown tool' in result['content'], result)

    # ---- the chat stays free during a mission, and stop needs no LLM
    n_missions = len(operations(MISSION_URL))
    reply, took = ros.say('go: sleep 60')
    check('reply while the mission runs', reply.is_ok and reply.answer == 'On it.' and took < 3.0, (reply, took))
    check('mission running', wait(lambda: any(op['status'] == 'working' for op in operations(MISSION_URL))))
    check('mission started by the tool', len(operations(MISSION_URL)) == n_missions + 1)
    reply, took = ros.say('still there?')
    check('chat free during a mission', reply.is_ok and took < 3.0, took)
    n_llm = len(llm.calls)
    n_robot = count(stops)
    reply, took = ros.say('stop')
    check('stop confirmed', reply.is_ok and reply.answer == 'Stopped.', reply)
    check('stop within a second', took < 1.0, took)
    check('robot stopped', count(stops) == n_robot + 1)
    check('stop is announced', wait(lambda: 'Stopped.' in ros.announced))
    check('nothing left running', not any(op['status'] == 'working' for op in operations(MISSION_URL)))
    time.sleep(1.5)
    check('a stopped mission does not resume the LLM', len(llm.calls) == n_llm, len(llm.calls) - n_llm)
    reply, _ = ros.say('what happened')
    check('the stop is in the history', any('was stopped' in str(m.get('content')) for m in llm.calls[-1]['messages']))

    # ---- only a bare command skips the LLM
    n_robot = count(stops)
    for text in ('stop there and take a photo', 'wait for me at the table', 'go to X, stop there, take the photo'):
        n_llm = len(llm.calls)
        reply, _ = ros.say(text)
        check(f'the LLM decides: {text}', reply.answer == f'reply: {text}' and len(llm.calls) == n_llm + 1, reply)
    check('those did not stop the robot', count(stops) == n_robot, count(stops) - n_robot)
    for text in ('Stop!', 'please stop', 'wait', 'ferma', 'robot, stop now', 'Stop, please.'):
        n_llm, n_robot = len(llm.calls), count(stops)
        reply, _ = ros.say(text)
        check(f'bare command: {text}', reply.answer == 'Stopped.' and len(llm.calls) == n_llm and count(stops) == n_robot + 1, reply)

    # ---- the LLM recognises the intent and stops through its tool
    ros.say('go: sleep 60')
    running = wait(lambda: any(op['status'] == 'working' for op in operations(MISSION_URL)))
    n_robot = count(stops)
    reply, took = ros.say('forget it')
    check('the LLM stops through stop_all', running and reply.answer == 'On it.' and count(stops) == n_robot + 1, reply)
    check('no mission left after the LLM stop', not any(op['status'] == 'working' for op in operations(MISSION_URL)))

    # ---- a finished mission reaches the person once, in the words of the LLM
    ros.announced.clear()
    n_llm = len(llm.calls)
    ros.say('go: fetch report')
    check('outcome announced', wait(lambda: any('The mission ended' in a and 'done: fetch report' in a for a in ros.announced)), ros.announced)
    time.sleep(1.0)
    check('outcome announced once', len([a for a in ros.announced if 'mission ended' in a]) == 1, ros.announced)
    check('one turn for the outcome', len(llm.calls) == n_llm + 3, len(llm.calls) - n_llm)

    # ---- a failed mission
    ros.announced.clear()
    ros.say('go: crash')
    check('failure reaches the LLM', wait(lambda: any('The mission failed' in a and 'boom' in a for a in ros.announced)), ros.announced)

    # ---- stop while the LLM is thinking
    results = {}

    def slow():
        results['slow'] = ros.say('slow 1.2', 20.0)
    thread = threading.Thread(target=slow)
    thread.start()
    time.sleep(0.4)
    reply, took = ros.say('halt')
    check('stop during an LLM call', reply.answer == 'Stopped.' and took < 1.0, (reply, took))
    thread.join()
    check('the slow turn still ends', results['slow'][0].is_ok, results['slow'])

    # ---- LLM server failing
    ros.announced.clear()
    llm.mode = 'down'
    reply, took = ros.say('hello?')
    check('LLM down is reported', not reply.is_ok and 'cannot think' in reply.answer, reply)
    check('no exception text is spoken', not any('Error' in a or '500' in a for a in ros.announced), ros.announced)
    reply, took = ros.say('stop')
    check('stop works with the LLM down', reply.answer == 'Stopped.', reply)
    llm.mode = 'ok'

    # ---- a mission that ends with the LLM down is still told, through its own report
    ros.say('go: final report')
    llm.mode = 'down'
    ros.announced.clear()
    check('report read out without the LLM', wait(lambda: 'done: final report' in ros.announced), ros.announced)
    llm.mode = 'ok'

    # ---- LLM server hanging
    llm.mode = 'hang'
    ros.announced.clear()
    start = time.time()
    results = {}

    def hanging():
        results['turn'] = ros.say('anyone?', 15.0)
    thread = threading.Thread(target=hanging)
    thread.start()
    time.sleep(0.5)
    reply, took = ros.say('stop')
    check('stop works with the LLM hanging', reply.answer == 'Stopped.' and took < 1.0, (reply, took))
    thread.join()
    check('a hanging LLM ends within the timeout', not results['turn'][0].is_ok and time.time() - start < LLM_TIMEOUT + 2.0, time.time() - start)
    llm.mode = 'ok'

    # ---- new conversation
    n_llm = len(llm.calls)
    reply, _ = ros.say('new conversation')
    check('new conversation uses no LLM', reply.is_ok and len(llm.calls) == n_llm, reply)
    ros.say('hello again')
    check('history forgotten', not any(m.get('content') == 'hello there' for m in llm.calls[-1]['messages']))


def test_azure(ros, llm, stops):
    """The Azure backend, on the wire format of the Responses API."""
    n_llm = len(llm.calls)
    reply, _ = ros.say('hello azure')
    check('azure answers', reply.is_ok and reply.answer == 'reply: hello azure', reply)
    check('azure uses the Responses route', llm.calls[-1]['path'] == '/openai/v1/responses', llm.calls[-1]['path'])
    check('azure offers the allowed tools', llm_tools(llm) == ALLOWED, llm_tools(llm))
    reply, _ = ros.say('go: azure mission')
    check('azure calls a tool', reply.is_ok and reply.answer == 'On it.', reply)
    check('azure resumes after the mission', wait(lambda: any('done: azure mission' in a for a in ros.announced)), ros.announced)


def test_unconfirmed_stop(ros):
    """A stop that cannot be confirmed is not reported as a stop."""
    robot.kill()
    robot.wait()
    reply, _ = ros.say('stop now')
    check('unconfirmed stop is said plainly', 'could not confirm' in reply.answer, reply)


def reachable(url, name='list_operations'):
    try:
        tool(url, name)
        return True
    except Exception:
        return False


def env():
    root = os.pathsep.join([REPO, os.environ.get('PYTHONPATH', '')])
    return dict(os.environ, PYTHONPATH=root)


def start_mission_server(port, timeout=900.0):
    return subprocess.Popen([sys.executable, '-m', 'semantic_autonomy.mission_server', '--server_name', f'mission{port}',
                             '--mcp_port', str(port), '--claude_bin', FAKE_CLAUDE, '--robot_mcp_url', ROBOT_URL,
                             '--approach_mcp_url', APPROACH_URL, '--camera_mcp_url', CAMERA_URL,
                             '--mission_timeout', str(timeout)],
                            env=env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_camera_server(log):
    return subprocess.Popen([sys.executable, '-m', 'semantic_autonomy.camera_server', '--server_name', 'camera_test',
                             '--mcp_port', str(PORT + 4), '--image_topic', '/test_camera/img',
                             '--camera_max_width', '320', '--max_image_age', '0.5'], env=env(), stdout=log, stderr=log)


def start_chatter(backend, *args, log=None, environ=None):
    return subprocess.Popen([sys.executable, '-m', 'semantic_autonomy.chatter', '--ros-args', '-p', f'backend:={backend}',
                             '-p', f'mcp_url:={MISSION_URL}', '-p', f'llm_timeout:={LLM_TIMEOUT}', *args],
                            env={**env(), **(environ or {})}, stdout=log, stderr=log)


def stop_process(process):
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()


def main():
    global robot
    with open(FAKE_CLAUDE, 'w') as script:
        script.write(f'#!/bin/sh\nexec {sys.executable} {HERE}/fake_claude.py "$@"\n')
    os.chmod(FAKE_CLAUDE, 0o755)
    log = open(os.path.join(TMP, 'yarpserver.log'), 'w')
    yarpserver = subprocess.Popen(['yarpserver', '--ip', '127.0.0.1', '--socket', YARP_SOCKET, '--write',
                                   '--portdb', os.path.join(TMP, 'ports.db'),
                                   '--subdb', os.path.join(TMP, 'subs.db')], stdout=log, stderr=log)
    time.sleep(2.0)
    rclpy.init()
    ros = FakeRos()
    exe = MultiThreadedExecutor(num_threads=8)
    exe.add_node(ros)
    threading.Thread(target=exe.spin, daemon=True).start()
    robot, stops = run_fake_robot()
    subprocess.run(['yarp', 'conf', '127.0.0.1', YARP_SOCKET], stdout=subprocess.DEVNULL)
    servers = [subprocess.Popen([sys.executable, '-m', 'semantic_autonomy.approach_server', '--server_name', 'approach_test',
                                 '--mcp_port', str(PORT + 1)], env=env(), stdout=log, stderr=log),
               start_mission_server(PORT + 2),
               start_camera_server(log)]
    llm = FakeLlm(PORT + 5)
    chatter = start_chatter('ollama', '-p', f'backend_url:=http://127.0.0.1:{PORT + 5}/v1', '-p', 'backend_model:=fake', log=log)
    try:
        assert wait(lambda: reachable(APPROACH_URL) and reachable(MISSION_URL) and reachable(CAMERA_URL, 'look'), 20.0), 'servers not up'
        ros.chat_images.clear()
        test_camera(ros)
        test_approach(ros)
        test_mission(ros, stops)
        test_timeout(ros, stops)
        test_chatter(ros, llm, stops)
        stop_process(chatter)
        chatter = start_chatter('azure', log=log, environ={'AZURE_API_KEY': 'x', 'AZURE_ENDPOINT': f'http://127.0.0.1:{PORT + 5}',
                                                          'DEPLOYMENT_ID': 'fake'})
        assert wait(lambda: ros.chat.service_is_ready(), 20.0), 'azure chatter not up'
        time.sleep(1.0)
        test_azure(ros, llm, stops)
        test_unconfirmed_stop(ros)
    except Exception:
        traceback.print_exc()
        check('no exception', False)
    print('ALL PASSED' if ok_all else 'SOME FAILED', flush=True)
    for process in [chatter, *servers]:
        stop_process(process)
    robot.kill()
    yarpserver.terminate()
    os._exit(0 if ok_all else 1)


if __name__ == '__main__':
    main()
