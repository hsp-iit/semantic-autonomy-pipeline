# SPDX-FileCopyrightText: 2026 Humanoid Sensing and Perception, Istituto Italiano di Tecnologia
# SPDX-License-Identifier: BSD-3-Clause
"""Stands in for the orchestrator's claude binary, so the tests spend no LLM usage. The prompt decides:
  sleep <s>         blocks, with a child process in its group
  crash             exits without JSON
Anything else is answered. Each call is appended to FAKE_CLAUDE_LOG as a JSON line."""
import json
import os
import subprocess
import sys
import time


def arg(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def main():
    argv = sys.argv[1:]
    prompt = arg(argv, '-p')
    with open(os.environ['FAKE_CLAUDE_LOG'], 'a') as log:
        log.write(json.dumps({'argv': argv, 'pid': os.getpid(), 'cwd': os.getcwd()}) + '\n')
    session = arg(argv, '--resume') or f'session-{os.getpid()}'
    if prompt.startswith('sleep '):
        seconds = prompt.split()[1]
        child = subprocess.Popen(['sleep', seconds])
        with open(os.environ['FAKE_CLAUDE_LOG'], 'a') as log:
            log.write(json.dumps({'child': child.pid}) + '\n')
        child.wait()
    elif prompt == 'crash':
        print('boom', file=sys.stderr)
        sys.exit(3)
    print(json.dumps({'session_id': session, 'is_error': False, 'result': f'done: {prompt}'}))


if __name__ == '__main__':
    time.sleep(0.05)
    main()
