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


## FAQs

### How do I get a Slack Webhook URL?

1. Visit the ["Your Apps"](https://api.slack.com/apps) page on Slack
2. Create an App (e.g. "Deployment alerts") and enable "Incoming Webhooks"
3. Setup a Webhook for your desired Workspace
4. Copy the token from the Webhook URL for use in [Installation](#installation)

Note: Each Webhook can only post to one channel since Slack changed their API. You will need to set up a webhook per channel you wish to notify.

## See Also

- [Sending messages using incoming webhooks](https://api.slack.com/messaging/webhooks)
