# SPDX-FileCopyrightText: 2026 Humanoid Sensing and Perception, Istituto Italiano di Tecnologia
# SPDX-License-Identifier: BSD-3-Clause
"""End to end with the real orchestrator and navigator: spends Claude usage, moves nothing.

The robot is scripted: fake_robot_mcp.py for the robot server and a fake /reach_object. Prints the tools in the
order they were called, the spoken outcome, and how long a stop took during a running mission. Isolated like
test_stack.py, on ROS domain 84. The chatter uses the scripted LLM by default. For a real model:
    python3 live_dry_run.py azure                  # with AZURE_API_KEY, AZURE_ENDPOINT, DEPLOYMENT_ID set
    python3 live_dry_run.py ollama <url> <model>   # url ending in /v1, and a model that supports tools
"""
import json
import os
import subprocess
import sys
import threading
import time

os.environ['AUTONOMY_TEST_DOMAIN_ID'] = '84'
os.environ['AUTONOMY_TEST_YARP_SOCKET'] = '10324'
os.environ['AUTONOMY_TEST_PORT'] = '18880'

import rclpy
from rclpy.executors import MultiThreadedExecutor

import test_stack as t

LOG = os.path.join(t.TMP, 'robot.log')
BACKEND = sys.argv[1] if len(sys.argv) > 1 else 'fake'
t.LLM_TIMEOUT = 12.0


def show(since):
    for line in open(LOG):
        call = json.loads(line)
        if call['t'] >= since:
            print(f"  robot: {call['tool']} {call['args'] or ''}", flush=True)


def wait_mission(timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        operations = t.operations(t.MISSION_URL)
        if operations and not any(op['status'] == 'working' for op in operations):
            return operations[-1]
        time.sleep(1.0)
    return None


def chatter_args():
    if BACKEND == 'fake':
        t.FakeLlm(t.PORT + 5)
        return 'ollama', '-p', f'backend_url:=http://127.0.0.1:{t.PORT + 5}/v1', '-p', 'backend_model:=fake'
    if BACKEND == 'ollama':
        return 'ollama', '-p', f'backend_url:={sys.argv[2]}', '-p', f'backend_model:={sys.argv[3]}'
    return (BACKEND,)


def request(text):
    return f'go: {text}' if BACKEND == 'fake' else text


def main():
    log = open(os.path.join(t.TMP, 'servers.log'), 'w')
    yarpserver = subprocess.Popen(['yarpserver', '--ip', '127.0.0.1', '--socket', t.YARP_SOCKET, '--write',
                                   '--portdb', os.path.join(t.TMP, 'ports.db'),
                                   '--subdb', os.path.join(t.TMP, 'subs.db')], stdout=log, stderr=log)
    time.sleep(2.0)
    subprocess.run(['yarp', 'conf', '127.0.0.1', t.YARP_SOCKET], stdout=subprocess.DEVNULL)
    rclpy.init()
    ros = t.FakeRos()
    ros.mode = 'succeed'
    exe = MultiThreadedExecutor(num_threads=8)
    exe.add_node(ros)
    threading.Thread(target=exe.spin, daemon=True).start()
    robot = subprocess.Popen([sys.executable, os.path.join(t.HERE, 'fake_robot_mcp.py'), LOG, str(t.PORT)],
                             stdout=log, stderr=log)
    processes = [
        robot,
        subprocess.Popen([sys.executable, '-m', 'semantic_autonomy.approach_server', '--server_name', 'approach_live',
                          '--mcp_port', str(t.PORT + 1)], env=t.env(), stdout=log, stderr=log),
        subprocess.Popen([sys.executable, '-m', 'semantic_autonomy.mission_server', '--server_name', 'mission_live',
                          '--mcp_port', str(t.PORT + 2), '--robot_mcp_url', t.ROBOT_URL,
                          '--approach_mcp_url', t.APPROACH_URL, '--mission_timeout', '300'],
                         env=t.env(), stdout=log, stderr=log),
        t.start_chatter(*chatter_args(), log=log)]
    try:
        assert t.wait(lambda: t.reachable(t.APPROACH_URL) and t.reachable(t.MISSION_URL), 20.0), 'servers not up'
        assert ros.chat.wait_for_service(15.0), 'chatter not up'

        print('--- chat only', flush=True)
        reply, took = ros.say('hi, who are you?', 40.0)
        print(f'  said in {took:.1f} s: {reply.answer}', flush=True)
        check_none = t.operations(t.MISSION_URL)
        print(f'  missions started: {len(check_none)}', flush=True)

        print('--- mission', flush=True)
        since = time.time()
        reply, took = ros.say(request('go to the table and get close to the red mug'), 40.0)
        print(f'  said in {took:.1f} s: {reply.answer}', flush=True)
        start = time.time()
        operation = wait_mission(280.0)
        print(f"  mission {operation and operation['status']} after {time.time() - start:.0f} s", flush=True)
        print(f"  outcome: {(operation or {}).get('result') or (operation or {}).get('error')}", flush=True)
        show(since)
        print(f'  /reach_object goals: {ros.goals}', flush=True)
        time.sleep(3.0)     # the chatter tells the outcome in one more LLM turn
        print(f'  last announcement: {ros.announced[-1]}', flush=True)

        print('--- stop during a mission', flush=True)
        open(LOG + '.hold', 'w').close()
        since = time.time()
        reply, took = ros.say(request('now go back to the table'), 40.0)
        print(f'  said in {took:.1f} s: {reply.answer}', flush=True)
        driving = t.wait(lambda: any(json.loads(line)['tool'] == 'wait_for_navigation' and json.loads(line)['t'] >= since
                                     for line in open(LOG)), 120.0)
        running = [op['operation_id'] for op in t.operations(t.MISSION_URL) if op['status'] == 'working']
        print(f'  driving: {driving}, missions running: {len(running)}', flush=True)
        reply, took = ros.say('stop', 20.0)
        print(f'  said in {took:.2f} s: {reply.answer}', flush=True)
        print(f"  that mission: {[t.op_status(t.MISSION_URL, op) for op in running]}", flush=True)
        show(since)
    finally:
        for process in processes:
            process.send_signal(2)
        time.sleep(3.0)
        for process in processes:
            process.kill()
        yarpserver.terminate()
        os._exit(0)


if __name__ == '__main__':
    main()
