# Slack notifications for Trellis deploys: one message per deploy, updated as it starts, succeeds, fails or is cancelled.
# https://github.com/smithfield-studio/trellis-slack-deploy-notifications

import atexit
import getpass
import json
import os
import re
import signal
import subprocess
import time
import urllib.request
from collections.abc import Mapping

from ansible.plugins.callback import CallbackBase
from ansible.template import Templar

PLAYBOOKS = ('deploy.yml',)
# Overridden by tests/run.py to point at a fake Slack.
API_URL = os.environ.get('SLACK_DEPLOY_API_URL', 'https://slack.com/api/')

DEFAULTS = {
    'start': '🟡 *{user}* deploying `{branch}` to *{site_link}* {details}',
    'success': '✅ *{user}* deployed `{branch}` to *{site_link}* {details}',
    'failed': '❌ *{user}* failed deploying `{branch}` to *{site_link}* {details}',
    'cancelled': '⚪ *{user}* cancelled deploying `{branch}` to *{site_link}* {details}',
    'failed_reply': '{reason}',
}

# Ansible's own messages for these say nothing useful, the detail is in stderr/stdout.
GENERIC_ERRORS = ('non-zero return code', 'MODULE FAILURE', 'One or more items failed')
ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
GITHUB_REPO = re.compile(r'^(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)(.+?)(?:\.git)?/?$')


def _get(obj, name):
    # ansible-core 2.19 added public result/task/host, the underscored ones are deprecated from 2.23.
    try:
        return getattr(obj, name)
    except AttributeError:
        return getattr(obj, '_' + name)


def _escape(value):
    return str(value).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _as_list(value):
    if not value:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(',') if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


def _duration(seconds):
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f'{hours}h {minutes:02d}m'
    return f'{minutes}m {seconds:02d}s' if minutes else f'{seconds}s'


def _deployer():
    try:
        name = subprocess.run(['git', 'config', 'user.name'], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        name = ''
    return name or os.environ.get('GITHUB_ACTOR') or getpass.getuser()


def _ci_url():
    if os.environ.get('CI_JOB_URL'):
        return os.environ['CI_JOB_URL']
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        return '{GITHUB_SERVER_URL}/{GITHUB_REPOSITORY}/actions/runs/{GITHUB_RUN_ID}'.format(**os.environ)
    return ''


def _error(result):
    """Most useful few lines of a failed task result."""
    # Recent ansible-core versions leave `failed` out of the loop item results passed to callbacks.
    for item in result.get('results') or []:
        if isinstance(item, Mapping) and (item.get('failed') or 'exception' in item or item.get('rc') not in (None, 0)):
            return _error(item)

    msg = str(result.get('msg') or result.get('reason') or '').strip()
    detail = ''
    for key in ('stderr', 'module_stderr', 'stdout', 'module_stdout'):
        if str(result.get(key) or '').strip():
            detail = str(result[key])
            break

    if detail and (not msg or any(generic in msg for generic in GENERIC_ERRORS)):
        msg = '\n'.join(detail.strip().splitlines()[-8:])
    msg = ANSI.sub('', msg).strip() or 'Unknown error'
    return msg if len(msg) <= 1500 else '…' + msg[-1500:]


class CallbackModule(CallbackBase):
    """Posts a Slack message when deploy.yml starts and updates it with the result."""

    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = 'notification'
    CALLBACK_NAME = 'slack_deploy'

    def __init__(self, display=None):
        super().__init__(display)
        self._pid = os.getpid()
        self._active = False
        self._started_at = None
        self._play = None
        self._clone_host = None
        self._ctx = None
        self._config = None
        self._messages = {}  # configured channel: (channel ID, message ts)
        self._failures = {}  # host: last failure
        self._failure = None
        self._interrupted = False
        self._finished = False

    def v2_playbook_on_start(self, playbook):
        self._active = os.path.basename(playbook._file_name) in PLAYBOOKS
        self._started_at = time.time()

    def v2_playbook_on_play_start(self, play):
        if not self._active:
            return
        self._play = play
        try:
            self._load(play)
        except Exception as e:
            self._warn(f'could not read config ({e})')

    def v2_runner_on_ok(self, result):
        if not self._ctx:
            return
        # Trellis registers its project clone as git_clone; hooks may clone other repos.
        # ansible-core 2.21 holds register as a mapping of names, earlier versions as a string.
        register = _get(result, 'task').register
        if 'git_clone' in (register if isinstance(register, Mapping) else [register]):
            self._clone_host = _get(result, 'host')

    def v2_runner_on_failed(self, result, ignore_errors=False):
        if self._active and not ignore_errors:
            self._record_failure(result)

    def v2_runner_on_unreachable(self, result):
        if self._active:
            self._record_failure(result)

    def v2_playbook_on_stats(self, stats):
        if not self._ctx:
            return
        failed_hosts = [h for h in stats.processed if stats.failures.get(h) or stats.dark.get(h)]
        self._failure = next((self._failures[h] for h in failed_hosts if h in self._failures), None)
        self._finish('failed' if failed_hosts else 'success')

    def _record_failure(self, result):
        # Keep the last per host: an earlier failure may have been rescued, the last is the one that stopped that host.
        host = _get(result, 'host').get_name()
        self._failures[host] = {
            'task': _get(result, 'task').get_name().strip(),
            'host': host,
            'error': _error(_get(result, 'result')),
        }

    def _load(self, play):
        """Resolve site, env and config from the first host of the first play that targets the site."""
        vm = play.get_variable_manager()
        hosts = vm._inventory.get_hosts(play.hosts)
        if not hosts:
            return
        hostvars = vm.get_vars(play=play, host=hosts[0])
        templar = Templar(loader=play._loader, variables=hostvars)

        def lookup(*path):
            node = hostvars
            try:
                for key in path:
                    if isinstance(node, str):
                        node = templar.template(node)
                    if not isinstance(node, Mapping) or key not in node:
                        return None
                    node = node[key]
                return templar.template(node)
            except Exception:
                return None

        site, env = lookup('site'), lookup('env')
        if not site or not env or lookup('wordpress_sites', site, 'site_hosts') is None:
            return

        # project_version is a deploy role default, so it only resolves once the deploy play starts.
        branch = lookup('project_version') or lookup('branch') or lookup('wordpress_sites', site, 'branch') or 'master'
        if self._ctx:
            self._ctx['branch'] = str(branch)
            return

        site_hosts = lookup('wordpress_sites', site, 'site_hosts') or []
        canonical = site_hosts[0].get('canonical') if site_hosts and isinstance(site_hosts[0], Mapping) else None
        repo = GITHUB_REPO.match(str(lookup('wordpress_sites', site, 'repo') or ''))

        self._ctx = {
            'user': _deployer(),
            'site': str(site),
            'env': str(env),
            'branch': str(branch),
            'url': f'https://{canonical}' if canonical else '',
            'repo_url': f'https://github.com/{repo.group(1)}' if repo else '',
            'ci_url': _ci_url(),
            'commit': '',
        }
        self._config = self._build_config(
            lookup('slack_deploy'),
            lookup('wordpress_sites', site, 'slack_deploy'),
            legacy_token=lookup('vault_wordpress_sites', site, 'slack_deploy_token'),
        )
        if not self._config:
            return

        atexit.register(self._on_exit)
        # ansible-core 2.19 swaps in its own handlers each time it starts a worker, so there a cancel reports as failed.
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous = signal.getsignal(signum)
            signal.signal(signum, lambda s, f, previous=previous: self._on_signal(s, f, previous))

        self._notify('start')

    def _build_config(self, *layers, legacy_token=None):
        config = dict(DEFAULTS, token=None, channel=None)
        for layer in layers:
            if layer is False:
                config.update(dict.fromkeys(DEFAULTS, False))
            elif isinstance(layer, Mapping):
                config.update({key: value for key, value in layer.items() if key in config and value is not None})

        config['token'] = str(config['token'] or os.environ.get('SLACK_DEPLOY_TOKEN') or '').strip()
        config['channels'] = _as_list(config.pop('channel'))
        if not config['token'] and not config['channels']:
            if legacy_token:
                self._warn('webhook tokens (slack_deploy_token) are no longer used, see "Upgrading from 1.x" in the README')
            return None
        if not config['channels']:
            self._warn('no channel set, add slack_deploy.channel')
            return None
        if not config['token'].startswith('xox'):
            self._warn('no Slack bot token (xoxb-...), set SLACK_DEPLOY_TOKEN or slack_deploy.token')
            return None
        return config

    def _on_signal(self, signum, frame, previous):
        if os.getpid() == self._pid:
            self._interrupted = True
            self._finish('cancelled')
        if callable(previous):
            return previous(signum, frame)
        if previous != signal.SIG_IGN:
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

    def _on_exit(self):
        # Ansible exited without reaching the stats callback, e.g. a fatal Ansible error.
        if os.getpid() == self._pid:
            if not self._interrupted:
                recorded = list(self._failures.values())
                self._failure = recorded[-1] if recorded else {'task': '', 'host': '', 'error': 'Ansible stopped before the deploy finished'}
            self._finish('cancelled' if self._interrupted else 'failed')

    def _finish(self, event):
        if self._finished or not self._config:
            return
        self._finished = True
        self._ctx['commit'] = self._commit()
        self._notify(event)

    def _commit(self):
        # Read from the registered variable at the end: the clone task is no_log, which hides its result from
        # callbacks, and ansible-core 2.19+ only registers the variable after the task's callbacks have run.
        if not self._clone_host:
            return ''
        try:
            hostvars = self._play.get_variable_manager().get_vars(play=self._play, host=self._clone_host)
            return str((hostvars.get('git_clone') or {}).get('after') or '')
        except Exception:
            return ''

    def _notify(self, event):
        """Post the deploy's message, or update it if the start was posted, plus a thread reply for failures."""
        message = self._config[event]
        reply = self._config['failed_reply'] if event == 'failed' else False
        if not message and not reply:
            return
        try:
            text = self._render(str(message), event) if message else ''
            reply = self._render(str(reply), event) if reply else ''
        except Exception as e:
            self._warn(f'could not build the {event} message ({e})')
            return

        for channel in self._config['channels']:
            posted = self._messages.get(channel)
            if text and posted:
                self._api('chat.update', channel=posted[0], ts=posted[1], text=text)
            elif text:
                response = self._api('chat.postMessage', channel=channel, text=text, unfurl_links=False, unfurl_media=False)
                if response:
                    self._messages[channel] = (response['channel'], response['ts'])
            if reply and channel in self._messages:
                channel_id, ts = self._messages[channel]
                # Edits don't notify anyone, so a failure on an earlier message also goes to the channel.
                self._api('chat.postMessage', channel=channel_id, thread_ts=ts, text=reply, reply_broadcast=bool(posted),
                          unfurl_links=False, unfurl_media=False)

    def _api(self, method, **payload):
        request = urllib.request.Request(API_URL + method, data=json.dumps(payload).encode(), headers={
            'Content-Type': 'application/json; charset=utf-8',
            'Authorization': f"Bearer {self._config['token']}",
        })
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = json.loads(response.read())
        except Exception as e:
            self._warn(f'{method} to {payload["channel"]} failed ({e})')
            return None
        if not body.get('ok'):
            error = body.get('error')
            if body.get('needed'):
                error += f", needs {body['needed']}, token has {body.get('provided') or 'none'}"
            self._warn(f'{method} to {payload["channel"]} failed ({error})')
            return None
        return body

    def _warn(self, message):
        self._display.warning(f'Slack deploy notifications: {message}')

    def _render(self, text, event):
        ctx = self._ctx
        failure = self._failure or {}
        values = {key: _escape(value) for key, value in ctx.items()}
        values.update({
            'task': _escape(failure.get('task', '')),
            'host': _escape(failure.get('host', '')),
            'error': _escape(failure.get('error', '')),
            'duration': _duration(time.time() - self._started_at),
            'commit': ctx['commit'][:7],
            'commit_url': f"{ctx['repo_url']}/commit/{ctx['commit']}" if ctx['repo_url'] and ctx['commit'] else '',
        })
        label = f"{values['site']} {values['env']}"
        values['site_link'] = f"<{ctx['url']}|{label}>" if ctx['url'] else label
        values['ci_link'] = f"<{ctx['ci_url']}|run>" if ctx['ci_url'] else ''
        values['commit_link'] = f"<{values['commit_url']}|{values['commit']}>" if values['commit_url'] else values['commit']
        details = [values['commit_link'], values['ci_link']]
        if event != 'start':
            details.insert(0, values['duration'])
        values['details'] = ' · '.join([''] + [d for d in details if d]).strip()
        reason = f"```{values['error']}```" if values['error'] else ''
        values['reason'] = f"Failed at *{values['task']}*\n{reason}" if values['task'] else reason

        text = re.sub(r'\{(\w+)\}', lambda m: values.get(m.group(1), m.group(0)), text)
        return '\n'.join(line.rstrip() for line in text.splitlines()).strip()
