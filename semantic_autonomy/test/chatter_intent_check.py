# SPDX-FileCopyrightText: 2026 Humanoid Sensing and Perception, Istituto Italiano di Tecnologia
# SPDX-License-Identifier: BSD-3-Clause
"""Asks a real chatter model what it would do with messages that are easy to get wrong. No robot, no servers.

Each message is a new conversation, with the real prompt and the tools of the chatter. Nothing is executed.
    python3 chatter_intent_check.py azure                 # with AZURE_API_KEY, AZURE_ENDPOINT, DEPLOYMENT_ID set
    python3 chatter_intent_check.py ollama <url> <model>  # url ending in /v1, and a model that supports tools
"""
import asyncio
import json
import sys
import time

from semantic_autonomy.chatter import DEFAULT_PROMPT, make_backend

TOOLS = [
    {"type": "function", "function": {
        "name": "start_mission",
        "description": "Hand what the user wants the robot to do to the orchestrator. Returns at once, with the "
                       "mission running in the background. Refused while another mission runs.",
        "parameters": {"type": "object", "properties": {"request": {"type": "string"}}, "required": ["request"]}}},
    {"type": "function", "function": {
        "name": "stop_all",
        "description": "Stop the running mission and the robot.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "list_tracked_operations",
        "description": "List all operations tracked during this host session.",
        "parameters": {"type": "object", "properties": {}}}},
]
CASES = [
    # message, what the model should call (None = no tool)
    ("hi, who are you?", None),
    ("what can you do?", None),
    ("grazie, sei stato bravo", None),
    ("go to the kitchen table", "start_mission"),
    ("go to place X, stop there, take the photo, and then behave accordingly", "start_mission"),
    ("stop at the table and take a photo", "start_mission"),
    ("wait for me at the table", "start_mission"),
    ("take a photo", "start_mission"),
    ("what's around you?", "start_mission"),
    ("vai al tavolo e fermati lì", "start_mission"),
    ("get close to the red mug", "start_mission"),
    ("grab the red mug", None),
    ("forget it", "stop_all"),
    ("that's enough, leave it", "stop_all"),
    ("no no, not there, give up", "stop_all"),
    ("robot, please stop right there", "stop_all"),
    ("lascia perdere", "stop_all"),
    ("is the robot doing anything right now?", "list_tracked_operations"),
]


async def main():
    backend = make_backend(sys.argv[1], *(sys.argv[2:4] if len(sys.argv) > 2 else ('', '')), 500)
    await backend.initialize()
    backend.client = backend.client.with_options(timeout=40.0, max_retries=0)
    prompt = open(DEFAULT_PROMPT, encoding='utf-8').read().strip()
    right, times = 0, []
    for text, expected in CASES:
        start = time.time()
        try:
            response = await backend.chat_completion(
                messages=[{"role": "system", "content": prompt}, {"role": "user", "content": text}], tools=TOOLS)
        except Exception as ex:
            print(f"ERROR {text!r}: {type(ex).__name__}: {str(ex)[:160]}")
            continue
        took = time.time() - start
        times.append(took)
        message = response.choices[0].message
        calls = message.tool_calls or []
        called = calls[0].function.name if calls else None
        ok = called == expected
        right += ok
        detail = json.loads(calls[0].function.arguments or '{}').get('request', '') if calls else (message.content or '<empty>')
        print(f"{'ok ' if ok else 'BAD'} {took:4.1f}s {text!r}\n      -> {called or 'text'}: {detail[:140]}", flush=True)
    if times:
        print(f"{right}/{len(CASES)} as expected, {min(times):.1f}-{max(times):.1f} s per call")


if __name__ == '__main__':
    asyncio.run(main())
