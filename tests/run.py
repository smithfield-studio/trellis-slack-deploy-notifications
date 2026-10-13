#!/usr/bin/env python3
"""Runs the slack_deploy callback against a Trellis-shaped playbook and checks the calls a fake Slack API receives.

Usage: python3 tests/run.py <dir with ansible-playbook> [<another dir> ...]
e.g.   python3 tests/run.py ~/git/some-site/trellis/.trellis/virtualenv/bin
"""

import itertools
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGINS = HERE.parent / 'callback_plugins'
TOKEN = 'xoxb-test-token'
CALLS = []
SHAS = {}
TIMESTAMPS = itertools.count(1)


class FakeSlack(BaseHTTPRequestHandler):
    def do_POST(self):
        method = self.path.rsplit('/', 1)[-1]
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.headers.get('Authorization') != f'Bearer {TOKEN}':
            response = {'ok': False, 'error': 'invalid_auth'}
        elif body['channel'] == '#missing':
            response = {'ok': False, 'error': 'channel_not_found'}
        elif body['channel'] == '#no-scope':
            response = {'ok': False, 'error': 'missing_scope', 'needed': 'chat:write', 'provided': 'incoming-webhook'}
        elif method == 'chat.update':
            response = {'ok': True, 'channel': body['channel'], 'ts': body['ts']}
        else:
            channel = body['channel'] if body['channel'].startswith('C') else 'C' + body['channel'].lstrip('#').upper()
            response = {'ok': True, 'channel': channel, 'ts': f'{next(TIMESTAMPS)}.000100'}
        CALLS.append((method, body, response))
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())

    def log_message(self, *args):
        pass


server = HTTPServer(('127.0.0.1', 0), FakeSlack)
threading.Thread(target=server.serve_forever, daemon=True).start()
API = f'http://127.0.0.1:{server.server_port}/api/'

START = '🟡 *Test Deployer* deploying `main` to *<https://example.com|example.com production>*'
COMMIT = '<https://github.com/smithfield-studio/example/commit/'
OP_REF = 'op://Smithfield/Slack Deploy Bot/credential'
# Stands in for the 1Password CLI: reads OP_REF only.
FAKE_OP = f'''#!{sys.executable}
import sys
if sys.argv[1:] == ['read', '--no-newline', {OP_REF!r}]:
    sys.stdout.write({TOKEN!r})
else:
    sys.exit(f'[ERROR] could not read secret {{sys.argv[-1]}}: no item found')
'''
ROLLBACK_START = '🟡 *Test Deployer* rolling back *<https://example.com|example.com production>*'


def site(**extra):
    return {
        'site_hosts': [{'canonical': 'example.com'}],
        'repo': 'git@github.com:smithfield-studio/example.git',
        'branch': 'main',
        'env': {'db_password': '{{ vault_slack_deploy_token }}'},
        **extra,
    }


# Expected calls are (kind, channel, [substrings]); kind is post, update, reply or broadcast.
SCENARIOS = [
    {'name': 'success', 'calls': [
        ('post', '#deploys', [START]),
        ('update', 'CDEPLOYS', ['✅ *Test Deployer* deployed `main` to *<https://example.com|example.com production>* · ', COMMIT]),
    ]},
    {'name': 'branch', 'args': ['-e', 'branch=feature'], 'calls': [
        ('post', '#deploys', ['deploying `feature`']),
        ('update', 'CDEPLOYS', ['deployed `feature`']),
    ]},
    {'name': 'task failure', 'args': ['-e', 'scenario_fail=true'], 'calls': [
        ('post', '#deploys', [START]),
        ('update', 'CDEPLOYS', ['❌ *Test Deployer* failed deploying `main`']),
        ('broadcast', 'CDEPLOYS', ['Failed at *deploy : Run composer install*', '```Your requirements could not be resolved']),
    ]},
    {'name': 'rescued and ignored failures', 'args': ['-e', 'scenario_rescue=true', '-e', 'scenario_ignore=true'], 'calls': [
        ('post', '#deploys', [START]),
        ('update', 'CDEPLOYS', ['✅']),
    ]},
    {'name': 'loop failure', 'args': ['-e', 'scenario_loop_fail=true'], 'calls': [
        ('post', '#deploys', [START]),
        ('update', 'CDEPLOYS', ['❌']),
        ('broadcast', 'CDEPLOYS', ['Failed at *deploy : Build assets*', 'non-zero return code']),
    ]},
    {'name': 'unreachable', 'args': ['-e', 'ansible_connection=ssh', '-e', 'ansible_host=127.0.0.1', '-e', 'ansible_port=1'], 'calls': [
        ('post', '#deploys', [START]),
        ('update', 'CDEPLOYS', ['❌']),
        ('broadcast', 'CDEPLOYS', ['Failed at *connection : Test connection*', 'Failed to connect']),
    ]},
    {'name': 'ctrl-c', 'args': ['-e', 'scenario_sleep=true'], 'cancel': signal.SIGINT, 'calls': [
        ('post', '#deploys', [START]),
        ('update', 'CDEPLOYS', ['⚪ *Test Deployer* cancelled deploying `main`']),
    ]},
    {'name': 'sigterm', 'args': ['-e', 'scenario_sleep=true'], 'cancel': signal.SIGTERM, 'calls': [
        ('post', '#deploys', [START]),
        ('update', 'CDEPLOYS', ['⚪ *Test Deployer* cancelled deploying `main`']),
    ]},
    {'name': 'failures only, failure', 'args': ['-e', 'scenario_fail=true'],
     'config': {'channel': '#deploys', 'start': False, 'success': False, 'failed_reply': '<!here> {reason}'}, 'calls': [
        ('post', '#deploys', ['❌ *Test Deployer* failed deploying `main`']),
        ('reply', 'CDEPLOYS', ['<!here> Failed at *deploy : Run composer install*']),
    ]},
    {'name': 'failures only, success', 'config': {'channel': '#deploys', 'start': False, 'success': False}, 'calls': []},
    {'name': 'site override and vault token', 'env': {'SLACK_DEPLOY_TOKEN': ''},
     'config': {'channel': '#deploys', 'token': '{{ vault_slack_deploy_token }}'},
     'site': {'channel': '#site', 'success': 'Shipped {branch} to {url} {commit}'}, 'calls': [
        ('post', '#site', [START]),
        ('update', 'CSITE', ['Shipped main to https://example.com ']),
    ]},
    {'name': 'vault token beats the environment', 'env': {'SLACK_DEPLOY_TOKEN': 'xoxb-other'},
     'config': {'channel': '#deploys', 'token': '{{ vault_slack_deploy_token }}'}, 'calls': [
        ('post', '#deploys', [START]),
        ('update', 'CDEPLOYS', ['✅']),
    ]},
    {'name': 'token from a .env above the project', 'env': {'SLACK_DEPLOY_TOKEN': ''},
     'dotenv': f'APP=x\nexport SLACK_DEPLOY_TOKEN="{TOKEN}"\n', 'calls': [
        ('post', '#deploys', [START]),
        ('update', 'CDEPLOYS', ['✅']),
    ]},
    {'name': 'token from 1Password', 'env': {'SLACK_DEPLOY_TOKEN': ''}, 'op': True,
     'dotenv': f'SLACK_DEPLOY_TOKEN="{OP_REF}"\n', 'calls': [
        ('post', '#deploys', [START]),
        ('update', 'CDEPLOYS', ['✅']),
    ]},
    {'name': 'missing 1Password item', 'env': {'SLACK_DEPLOY_TOKEN': 'op://Smithfield/Nope/credential'}, 'op': True,
     'warning': 'could not read op://Smithfield/Nope/credential from 1Password ([ERROR] could not read secret', 'calls': []},
    {'name': 'no 1Password CLI', 'env': {'SLACK_DEPLOY_TOKEN': OP_REF}, 'op': False,
     'warning': 'the 1Password CLI (op) is not installed', 'calls': []},
    {'name': 'environment beats a .env', 'dotenv': 'SLACK_DEPLOY_TOKEN=xoxb-other\n', 'calls': [
        ('post', '#deploys', [START]),
        ('update', 'CDEPLOYS', ['✅']),
    ]},
    {'name': 'two channels', 'config': {'channel': ['#one', '#two']}, 'calls': [
        ('post', '#one', [START]),
        ('post', '#two', [START]),
        ('update', 'CONE', ['✅']),
        ('update', 'CTWO', ['✅']),
    ]},
    {'name': 'GitHub Actions', 'env': {
        'GITHUB_ACTIONS': 'true', 'GITHUB_SERVER_URL': 'https://github.com',
        'GITHUB_REPOSITORY': 'smithfield-studio/example', 'GITHUB_RUN_ID': '123',
    }, 'calls': [
        ('post', '#deploys', [START + ' · <https://github.com/smithfield-studio/example/actions/runs/123|run>']),
        ('update', 'CDEPLOYS', [COMMIT, '|run>']),
    ]},
    {'name': 'hook clones another repo', 'args': ['-e', 'scenario_other_clone=true'], 'calls': [
        ('post', '#deploys', [START]),
        ('update', 'CDEPLOYS', ['/commit/{main_sha}|']),
    ]},
    {'name': 'multi-host failure', 'hosts': 2,
     'args': ['-e', 'scenario_fail=true', '-e', 'fail_host=web1', '-e', 'scenario_late_rescue=true'], 'calls': [
        ('post', '#deploys', [START]),
        ('update', 'CDEPLOYS', ['❌']),
        ('broadcast', 'CDEPLOYS', ['Failed at *deploy : Run composer install*']),
    ]},
    {'name': 'failed update off, reply on', 'args': ['-e', 'scenario_fail=true'],
     'config': {'channel': '#deploys', 'failed': False}, 'calls': [
        ('post', '#deploys', [START]),
        ('broadcast', 'CDEPLOYS', ['Failed at *deploy : Run composer install*']),
    ]},
    {'name': 'rollback', 'playbook': 'rollback.yml', 'calls': [
        ('post', '#deploys', [ROLLBACK_START]),
        ('update', 'CDEPLOYS', ['↩️ *Test Deployer* rolled back *<https://example.com|example.com production>* to `20261012090000` · ']),
    ]},
    {'name': 'rollback to a release', 'playbook': 'rollback.yml', 'args': ['-e', 'release=20261011090000'], 'calls': [
        ('post', '#deploys', [ROLLBACK_START]),
        ('update', 'CDEPLOYS', ['to `20261011090000`']),
    ]},
    {'name': 'rollback failure', 'playbook': 'rollback.yml', 'args': ['-e', 'scenario_oldest=true'], 'calls': [
        ('post', '#deploys', [ROLLBACK_START]),
        ('update', 'CDEPLOYS', ['❌ *Test Deployer* failed rolling back']),
        ('broadcast', 'CDEPLOYS', ['Failed at *rollback : Fail if current release is the oldest available release*',
                                   '```Currently symlinked to earliest available release']),
    ]},
    {'name': 'rollback cancelled', 'playbook': 'rollback.yml', 'args': ['-e', 'scenario_sleep=true'], 'cancel': signal.SIGINT, 'calls': [
        ('post', '#deploys', [ROLLBACK_START]),
        ('update', 'CDEPLOYS', ['⚪ *Test Deployer* cancelled rolling back']),
    ]},
    {'name': 'rollback messages', 'playbook': 'rollback.yml',
     'config': {'channel': '#deploys', 'success': 'Deployed', 'rollback': {'success': 'Back on {release}'}}, 'calls': [
        ('post', '#deploys', [ROLLBACK_START]),
        ('update', 'CDEPLOYS', ['Back on 20261012090000']),
    ]},
    {'name': 'rollbacks off', 'playbook': 'rollback.yml', 'config': {'channel': '#deploys', 'rollback': False}, 'calls': []},
    {'name': 'rollbacks off, deploy', 'config': {'channel': '#deploys', 'rollback': False}, 'calls': [
        ('post', '#deploys', [START]),
        ('update', 'CDEPLOYS', ['✅']),
    ]},
    {'name': 'disabled for site', 'site': False, 'calls': []},
    {'name': 'no token', 'env': {'SLACK_DEPLOY_TOKEN': ''}, 'warning': 'no Slack bot token', 'calls': []},
    {'name': 'webhook token', 'env': {'SLACK_DEPLOY_TOKEN': 'T000/B000/XXXX'}, 'warning': 'no Slack bot token', 'calls': []},
    {'name': '1.x config only', 'env': {'SLACK_DEPLOY_TOKEN': ''}, 'config': None, 'warning': 'no longer used', 'calls': []},
    {'name': 'Slack error', 'config': {'channel': '#missing'}, 'warning': 'channel_not_found', 'calls': [
        ('post', '#missing', [START]),
        ('post', '#missing', ['✅']),
    ]},
    {'name': 'token without chat:write', 'config': {'channel': '#no-scope'},
     'warning': 'missing_scope, needs chat:write, token has incoming-webhook', 'calls': [
        ('post', '#no-scope', [START]),
        ('post', '#no-scope', ['✅']),
    ]},
    {'name': 'invalid site', 'args': ['-e', 'site=nope.com'], 'calls': []},
    {'name': 'other playbook', 'playbook': 'server.yml', 'calls': []},
]


def make_repo(root, name):
    repo = root / f'{name}.git'
    work = root / f'{name}-work'
    git = ['git', '-c', 'user.name=x', '-c', 'user.email=x@x', '-c', 'init.defaultBranch=main']
    subprocess.run(git + ['init', '--bare', '-q', str(repo)], check=True)
    subprocess.run(git + ['init', '-q', str(work)], check=True)
    (work / 'README').write_text(name)
    for cmd in (['add', '.'], ['commit', '-qm', 'init'], ['branch', 'feature'], ['push', '-q', str(repo), 'main', 'feature']):
        subprocess.run(git + ['-C', str(work)] + cmd, check=True)
    return subprocess.run(['git', '-C', str(work), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()


def write_yaml(path, data):
    # JSON is valid YAML.
    path.write_text(json.dumps(data, indent=2))


def kind(method, body):
    if method == 'chat.update':
        return 'update'
    if 'thread_ts' in body:
        return 'broadcast' if body.get('reply_broadcast') else 'reply'
    return 'post'


def check_calls(expected):
    problems = []
    if len(CALLS) != len(expected):
        problems.append(f'expected {len(expected)} calls, got {len(CALLS)}')
    posted = {}
    for (method, body, response), (want_kind, want_channel, substrings) in zip(CALLS, expected):
        got_kind = kind(method, body)
        if (got_kind, body['channel']) != (want_kind, want_channel):
            problems.append(f'got {got_kind} {body["channel"]}, expected {want_kind} {want_channel}')
        substrings = [s.replace('{main_sha}', SHAS['repo']) for s in substrings]
        problems += [f'{got_kind} {body["channel"]}: missing {s!r}' for s in substrings if s not in body['text']]
        ts = body.get('ts') or body.get('thread_ts')
        if ts and posted.get(body['channel']) != ts:
            problems.append(f'{got_kind} {body["channel"]}: ts {ts} is not the posted message')
        if got_kind == 'post' and response['ok']:
            posted[response['channel']] = response['ts']
    return problems


def run(bindir, scenario, root, version):
    expected = scenario['calls']
    if scenario.get('cancel') and 'core 2.19.' in version:
        # 2.19 replaces signal handlers whenever it starts a worker, so the cancel reaches Ansible as a task failure.
        expected = expected[:1] + [('update', 'CDEPLOYS', ['❌']), ('broadcast', 'CDEPLOYS', ['Failed at'])]

    slug = scenario['name'].replace(' ', '-').replace(',', '').replace('.', '')
    project = root / slug
    if 'dotenv' in scenario:
        # A .env two folders up, as one in the folder holding every project would be.
        project.mkdir()
        (project / '.env').write_text(scenario['dotenv'])
        project = project / 'site' / 'trellis'
    shutil.copytree(HERE / 'fixture', project)
    if scenario.get('hosts') == 2:
        (project / 'hosts/production').write_text(
            '[production]\n'
            'web1 ansible_connection=local ansible_python_interpreter="{{ ansible_playbook_python }}"\n'
            'web2 ansible_connection=local ansible_python_interpreter="{{ ansible_playbook_python }}"\n\n'
            '[web]\nweb1\nweb2\n'
        )

    wordpress_site = site(**({} if 'site' not in scenario else {'slack_deploy': scenario['site']}))
    write_yaml(project / 'group_vars/production/wordpress_sites.yml', {'wordpress_sites': {'example.com': wordpress_site}})
    write_yaml(project / 'group_vars/production/vault.yml', {
        'vault_slack_deploy_token': TOKEN,
        'vault_wordpress_sites': {'example.com': {'slack_deploy_token': ['T000/B000/XXXX']}},
    })
    subprocess.run([str(bindir / 'ansible-vault'), 'encrypt', 'group_vars/production/vault.yml'],
                   cwd=project, check=True, capture_output=True)
    config = scenario.get('config', {'channel': '#deploys'})
    if config is not None:
        write_yaml(project / 'group_vars/all/slack.yml', {'slack_deploy': config})

    gitconfig = root / 'gitconfig'
    gitconfig.write_text('[user]\n\tname = Test Deployer\n')
    env = {k: v for k, v in os.environ.items() if not k.startswith(('GITHUB_', 'CI_', 'SLACK_', 'ANSIBLE_'))}
    env.update({'SLACK_DEPLOY_TOKEN': TOKEN}, **scenario.get('env', {}))
    env.update(ANSIBLE_CALLBACK_PLUGINS=str(PLUGINS), SLACK_DEPLOY_API_URL=API, PYTHONDONTWRITEBYTECODE='1',
               GIT_CONFIG_GLOBAL=str(gitconfig), GIT_CONFIG_NOSYSTEM='1')
    if 'op' in scenario:
        # A PATH without the real op, which would ask for Touch ID.
        env['PATH'] = '/usr/bin:/bin'
        if scenario['op']:
            fake_bin = root / f'{slug}-bin'
            fake_bin.mkdir()
            (fake_bin / 'op').write_text(FAKE_OP)
            (fake_bin / 'op').chmod(0o755)
            env['PATH'] = f'{fake_bin}:/usr/bin:/bin'

    CALLS.clear()
    cmd = [str(bindir / 'ansible-playbook'), scenario.get('playbook', 'deploy.yml'), '-e', 'env=production',
           '-e', 'site=example.com', '-e', f'fixture_repo={root / "repo.git"}',
           '-e', f'fixture_other_repo={root / "other.git"}'] + scenario.get('args', [])
    proc = subprocess.Popen(cmd, cwd=project, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if scenario.get('cancel'):
        deadline = time.time() + 60
        while not CALLS and time.time() < deadline:
            time.sleep(0.2)
        time.sleep(3)
        os.kill(proc.pid, scenario['cancel'])
    output = proc.communicate(timeout=180)[0]
    time.sleep(0.2)

    problems = check_calls(expected)
    warning = scenario.get('warning')
    # ansible-core 2.16 to 2.18 wrap warnings at the terminal width.
    if warning and warning not in ' '.join(output.split()):
        problems.append(f'output is missing the warning {warning!r}')
    for marker in ('Slack deploy notifications', 'Failure using method', 'DEPRECATION'):
        if marker in output and not (warning and marker == 'Slack deploy notifications'):
            problems.append(f'output contains {marker!r}')
    return problems, output


def main():
    bindirs = [Path(p).expanduser() for p in sys.argv[1:]]
    if not bindirs:
        sys.exit(__doc__)

    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        SHAS.update(repo=make_repo(root, 'repo'), other=make_repo(root, 'other'))
        for bindir in bindirs:
            version = subprocess.run([str(bindir / 'ansible'), '--version'], capture_output=True, text=True).stdout.splitlines()[0]
            print(f'\n{version}')
            version_root = root / version.replace(' ', '_')
            version_root.mkdir()
            for repo in ('repo.git', 'other.git'):
                (version_root / repo).symlink_to(root / repo)
            for scenario in SCENARIOS:
                problems, output = run(bindir, scenario, version_root, version)
                print(f'  {"FAIL" if problems else "ok  "} {scenario["name"]}')
                if problems or os.environ.get('VERBOSE'):
                    failures += bool(problems)
                    for problem in problems:
                        print(f'       {problem}')
                    for method, body, _ in CALLS:
                        print(f'       -> {kind(method, body)} {body["channel"]} {body["text"]!r}')
                    if problems:
                        print('       ' + '\n       '.join(output.strip().splitlines()[-25:]))
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
