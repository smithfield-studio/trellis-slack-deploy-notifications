# Trellis Slack Deploy Notifications

Sends notifications to Slack before & after deployments via [Trellis](https://github.com/roots/trellis). Forked from [trellis-slack-webhook-notify-during-deploy](https://github.com/ItinerisLtd/trellis-slack-webhook-notify-during-deploy).

## Requirements

- Trellis v1.0.0 or later
- Trellis CLI
- Ansible v2.6 or later
- Slack webhook token

## Screenshots

![Deployment Screenshot](./screenshot.jpg)

## Installation

Add the role to `galaxy.yml` + [check for the latest version](https://github.com/smithfield-studio/trellis-slack-deploy-notifications/releases).

```yaml
---
roles:
  #...
  - name: trellis-slack-deploy-notifications
    src: https://github.com/smithfield-studio/trellis-slack-deploy-notifications
    version: 1.0.1
```

Ensure you have [Trellis CLI](https://github.com/roots/trellis-cli) installed, then run:

```bash
trellis galaxy install
```

## Update Deploy Hooks

Add the start & success tasks to the `deploy_before` & `deploy_after` [deploy hooks](https://roots.io/trellis/docs/deploys/#hooks) in `roles/deploy/defaults/main.yml`.

```yaml
deploy_before:
  - '{{ playbook_dir }}/vendor/roles/trellis-slack-deploy-notifications/tasks/deploy_start.yml'

#...

deploy_after:
  - '{{ playbook_dir }}/vendor/roles/trellis-slack-deploy-notifications/tasks/deploy_success.yml'
```

Add your Slack webhook token(s) (end of the webhook URL) and channel into `group_vars/{environment}/vault.yml`

```yaml
vault_wordpress_sites:
  example.com.au:
    slack_deploy_token:
      - xxx/xxx/xxxxx
      - xxx/xxx/xxxxx
    env: #...
```

## Running via GitHub Actions / CI
See [setup-trellis-cli](https://github.com/roots/setup-trellis-cli) to get the workflow setup. Then add the `CI_JOB_URL` to env var to your deploy workflow to include a link back to the running GitHub Action in the start notification.

```yaml
name: Deploy site
env:
  CI_JOB_URL: ${{ github.event.repository.html_url }}/actions/runs/${{ github.run_id }}
```

## Customise deploy messages

You can choose to customise the deploy messages globally or per project. If you set both, the project config will take precedence. For each task (deploy start or deploy success) you can customise two elements: `text` and `colour`.

To set globally, in `group_vars/all/helpers.yml`:

```yaml
slack_notify:
  deploy_start_text: "has started deploying 🌱"
  deploy_start_colour: "normal" # see https://docs.ansible.com/ansible/latest/collections/community/general/slack_module.html#parameter-color for accepted colours.
  deploy_success_text: "has successfully deployed 🌿"
  deploy_success_colour: "good"
```

To set per project, in `group_vars/{environment}/wordpress_sites.yml`

```yaml
wordpress_sites:
  example.com.au:
    slack_notify:
      deploy_start_text: "is kicking up code from"
```

With both of the above examples set, the deploy start message would be taken from the project config:

> **Dale Grant** is kicking up code from `master` to **example.com.au staging**

## FAQs

### How do I get a Slack Webhook URL?

1. Visit the ["Your Apps"](https://api.slack.com/apps) page on Slack
2. Create an App (e.g. "Deployment alerts") and enable "Incoming Webhooks"
3. Setup a Webhook for your desired Workspace
4. Copy the token from the Webhook URL for use in [Installation](#installation)

Note: Each Webhook can only post to one channel since Slack changed their API. You will need to set up a webhook per channel you wish to notify.

## See Also

- [Sending messages using incoming webhooks](https://api.slack.com/messaging/webhooks)
