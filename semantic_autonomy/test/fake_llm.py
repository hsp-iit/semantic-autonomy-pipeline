# SPDX-FileCopyrightText: 2026 Humanoid Sensing and Perception, Istituto Italiano di Tecnologia
# SPDX-License-Identifier: BSD-3-Clause
"""Scripted LLM server, so the real backends of yarp-mcpHosts run with no model behind them.

Speaks both Chat Completions (/v1/chat/completions: Ollama and OpenAI compatible servers) and Azure Responses
(/openai/v1/responses). The last user message decides the reply:
  go: <text>     calls start_mission, then answers "On it."
  hidden         calls new_conversation, which is not offered to the chatter
  forget it      calls stop_all
  slow <s>       answers after s seconds
  anything else  answers "reply: <text>"
mode: 'ok', 'down' (HTTP 500) or 'hang' (no answer for a minute).
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeLlm:
    def __init__(self, port):
        self.mode = 'ok'
        self.calls = []     # one per request: {'path', 'tools', 'last', 'system', 'messages'}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if fake.mode == 'hang':
                    time.sleep(60.0)
                if fake.mode == 'down':
                    self.send_error(500)
                    return
                responses = self.path.endswith('/responses')
                reply = fake.reply(self.path, body, responses)
                data = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except BrokenPipeError:     # the client gave up, as it should on a slow server
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def reply(self, path, body, responses):
        items = body.get('input') if responses else body['messages']
        tools = [(t.get('name') or t['function']['name']) for t in body.get('tools') or []]
        after_tool = (items[-1].get('type') == 'function_call_output') if responses else items[-1].get('role') == 'tool'
        last = next(m.get('content') for m in reversed(items) if m.get('role') == 'user')
        system = next((m.get('content') for m in items if m.get('role') == 'system'), '')
        self.calls.append({'path': path, 'tools': tools, 'last': last, 'system': system, 'after_tool': after_tool,
                           'messages': items})
        if after_tool:
            return self.text('On it.', responses)
        if last.startswith('go: '):
            return self.tool('start_mission', {'request': last[4:]}, responses)
        if last == 'forget it':
            return self.tool('stop_all', {}, responses)
        if last == 'hidden':
            return self.tool('new_conversation', {}, responses)
        if last.startswith('slow '):
            time.sleep(float(last.split()[1]))
        return self.text(f'reply: {last}', responses)

    @staticmethod
    def text(content, responses):
        if responses:
            return {'id': 'r', 'object': 'response', 'status': 'completed', 'output': [
                {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': content}]}]}
        return {'id': 'c', 'object': 'chat.completion', 'choices': [
            {'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': content}}]}

    @staticmethod
    def tool(name, args, responses):
        if responses:
            return {'id': 'r', 'object': 'response', 'status': 'completed', 'output': [
                {'type': 'function_call', 'id': 'fc_1', 'call_id': 'call_1', 'name': name, 'arguments': json.dumps(args)}]}
        return {'id': 'c', 'object': 'chat.completion', 'choices': [
            {'index': 0, 'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'call_1', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}}]}

    def close(self):
        self.server.shutdown()
