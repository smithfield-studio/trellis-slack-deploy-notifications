# Trellis Slack Deploy Notifications

Posts one Slack message per [Trellis](https://github.com/roots/trellis) deploy and updates it as the deploy succeeds, fails or is cancelled. Forked from [trellis-slack-webhook-notify-during-deploy](https://github.com/ItinerisLtd/trellis-slack-webhook-notify-during-deploy).

It runs as an Ansible callback plugin rather than deploy hooks, so it sees every task in `deploy.yml`. A failure anywhere (connection test, build, your own hooks, an unreachable host) marks the message as failed and replies in its thread with the task and error.

```
🟡 Mike Sheppard deploying `main` to example.com production · run
✅ Mike Sheppard deployed `main` to example.com production · 2m 14s · 02c8ce6 · run
❌ Mike Sheppard failed deploying `main` to example.com production · 48s · run
     ↳ Failed at deploy : Run composer install (also sent to the channel)
⚪ Mike Sheppard cancelled deploying `main` to example.com production · 12s
```

## Requirements

- Trellis with ansible-core 2.16 or later (tested on 2.16 to 2.21)
- Trellis CLI
- A Slack app with a bot token

## Slack app

1. Open your app on the ["Your Apps"](https://api.slack.com/apps) page, or create one
2. Under OAuth & Permissions, add the bot token scopes `chat:write` and `chat:write.public` (posts to public channels without inviting the bot)
3. Install (or reinstall) the app to the workspace and copy the Bot User OAuth Token (`xoxb-...`)
4. For a private channel, invite the bot: `/invite @your-bot`

One token covers every project and channel in the workspace.

## Installation

Add the role to `galaxy.yml` + [check for the latest version](https://github.com/smithfield-studio/trellis-slack-deploy-notifications/releases).

```yaml
---
roles:
  #...
  - name: trellis-slack-deploy-notifications
    src: https://github.com/smithfield-studio/trellis-slack-deploy-notifications.git
    version: 2.0.0 # Check for latest version on GitHub
```

Install it:

```bash
trellis galaxy install
```

Add the plugin's folder to the end of `callback_plugins` in `ansible.cfg`:

```ini
callback_plugins = ~/.ansible/plugins/callback:/usr/share/ansible/plugins/callback:lib/trellis/plugins/callback:vendor/roles/trellis-slack-deploy-notifications/callback_plugins
```

Set the channel, e.g. in `group_vars/all/main.yml`:

```yaml
slack_deploy:
  channel: "#deploys"
```

Make the bot token available as `SLACK_DEPLOY_TOKEN` wherever deploys run:

- GitHub Actions: add a `SLACK_DEPLOY_TOKEN` secret (organisation or repository) and pass it to the deploy step
- Local deploys: `export SLACK_DEPLOY_TOKEN=xoxb-...` in your shell profile

```yaml
    - name: Deploy
      env:
        SLACK_DEPLOY_TOKEN: ${{ secrets.SLACK_DEPLOY_TOKEN }}
      run: trellis deploy production
```

Or keep it in the project's vault and set `slack_deploy.token: "{{ vault_slack_deploy_token }}"`.

## GitHub Actions / CI

On GitHub Actions the message links to the workflow run automatically. For other CI, set `CI_JOB_URL` to the job's URL.

The name in each message is `git config user.name`, falling back to `$GITHUB_ACTOR`. Set it in the workflow to show who triggered the run:

```yaml
    - name: Setup git user
      run: git config --global user.name "${{ github.triggering_actor }} (via GH)"
```

## Configuration

Set `slack_deploy` in `group_vars` (`all` or an environment) and/or per site in `wordpress_sites.yml`. Site settings win.

| Setting | Value |
| --- | --- |
| `channel` | Channel name or ID, or a list of them |
| `token` | Bot token, defaults to `$SLACK_DEPLOY_TOKEN` |
| `start` | Text of the message posted when the deploy starts |
| `success`, `failed`, `cancelled` | Text the message is updated to when the deploy ends |
| `failed_reply` | Thread reply posted when a deploy fails |

Set any message to `false` to skip it, or `slack_deploy: false` to turn everything off for a site or environment. With `start` off, the end message is posted as a new message.

The failure reply is also sent to the channel when it replies to an earlier start message, as Slack doesn't notify anyone about edits. Put mentions such as `<!here>` in `failed_reply`, not `failed`.

Failures only, on staging:

```yaml
# group_vars/staging/wordpress_sites.yml
wordpress_sites:
  example.com.au:
    slack_deploy:
      start: false
      success: false
      failed_reply: "<!here> {reason}"
```

Quote message text: YAML reads a value starting with `{` or `*` as something else.

### Placeholders

| Placeholder | Value |
| --- | --- |
| `{user}` | `git config user.name`, falling back to `$GITHUB_ACTOR` |
| `{branch}` | Branch being deployed (`project_version`) |
| `{site}`, `{env}` | Site key and environment |
| `{url}` | `https://` + the site's first canonical host |
| `{site_link}` | `{site} {env}` linked to `{url}` |
| `{commit}`, `{commit_url}`, `{commit_link}` | Short SHA, GitHub commit URL, linked SHA. Set once the deploy has cloned the repo |
| `{ci_url}`, `{ci_link}` | CI run URL, "run" link |
| `{duration}` | Time since the deploy started |
| `{details}` | Duration (except on start), commit link and run link, separated by `·` |
| `{task}`, `{error}`, `{host}` | Failed task, its error and host |
| `{reason}` | `{task}` and `{error}` as formatted in the default failure reply |

Jinja (`{{ }}`) is rendered first, so Ansible variables work in message text too.

### Defaults

```yaml
slack_deploy:
  start: "🟡 *{user}* deploying `{branch}` to *{site_link}* {details}"
  success: "✅ *{user}* deployed `{branch}` to *{site_link}* {details}"
  failed: "❌ *{user}* failed deploying `{branch}` to *{site_link}* {details}"
  cancelled: "⚪ *{user}* cancelled deploying `{branch}` to *{site_link}* {details}"
  failed_reply: "{reason}"
```

## Upgrading from 1.x

1. Set `version: 2.0.0` in `galaxy.yml` and run `trellis galaxy install`
2. Remove the `deploy_start.yml` and `deploy_success.yml` lines from `deploy_before` and `deploy_after` in `roles/deploy/defaults/main.yml`
3. Add the `callback_plugins` line to `ansible.cfg`
4. Set `slack_deploy.channel` and make the bot token available (see [Installation](#installation))
5. Remove `slack_deploy_token` from `vault_wordpress_sites` and any `slack_notify` settings: webhooks and the 1.x message settings are no longer used

`CI_JOB_URL` is no longer needed on GitHub Actions, and a separate workflow step for failed deploys can be removed.

## FAQs

### Why did a cancelled deploy say it failed?

ansible-core 2.19 replaces signal handlers each time it starts a task worker, so a cancel reaches Ansible as a task failure. 2.16 to 2.18 and 2.20+ report it as cancelled.

## Tests

Runs the plugin against a Trellis-shaped playbook with a local fake Slack API, once per ansible-core install passed in:

```bash
python3 tests/run.py ~/path/to/site/trellis/.trellis/virtualenv/bin
```

## See Also

- [chat.postMessage](https://api.slack.com/methods/chat.postMessage) and [chat.update](https://api.slack.com/methods/chat.update)
