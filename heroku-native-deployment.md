# Deploying PR-Agent to Heroku Without Docker

This guide explains how to switch from Docker-based deployment to native Heroku buildpack deployment, which may resolve memory and stability issues.

## Why Switch?

- **Lower memory overhead**: No Docker container overhead
- **Better resource utilization**: Direct Python process execution
- **Easier debugging**: Standard Heroku logs and metrics
- **Faster deployments**: No Docker image building required

## Prerequisites

- Heroku CLI installed
- Git repository cloned
- Existing Heroku app (or create a new one)

## Steps to Switch

### 1. Update Heroku Stack (Remove Container)

```bash
cd ~/nodejs/pr-agent

# Check current stack
heroku stack -a pr-agent-app

# Switch from container to heroku-24
heroku stack:set heroku-24 -a pr-agent-app
```

### 2. Verify Required Files

The following files should now be in your repository:
- ✅ `Procfile` - Tells Heroku how to run your app
- ✅ `.python-version` - Python version (upstream, `3.12`)
- ✅ `pyproject.toml` + `uv.lock` - dependencies; the Heroku Python buildpack installs them with uv

Do not add `runtime.txt` or `requirements.txt`: the buildpack rejects more than one package manager file.
The buildpack bundles its own uv version; upstream's `[tool.uv] required-version` (`>=0.12.10`) accepts it.
Re-check this after each upstream sync.

The buildpack runs `uv sync --locked --no-default-groups`, which installs **no extras**. Upstream moved git-provider
clients into extras (`pr-agent[github]`), so the fork lists `PyGithub` in the base `dependencies` of `pyproject.toml`.
Without it every webhook fails with "module 'github' is not installed". After each upstream sync, check with:

```bash
UV_PROJECT_ENVIRONMENT=/tmp/hk uv sync --locked --no-default-groups
/tmp/hk/bin/python -c 'import pr_agent.git_providers as g; print(g._GIT_PROVIDERS["github"])'
```

### Use each PR author's Claude subscription

This setup is for an internal, self-hosted GitHub App whose authors opt in with their own Claude subscriptions.
The Heroku Python build runs `bin/post_compile` to install a pinned, checksum-verified Claude Code binary
and the existing `codecommit` extra, which includes `boto3`.

Set these deployment config vars. Keep the existing OpenAI credentials and model configuration. The author's
Claude model is tried first, then PR-Agent uses its regular model chain.

| Config var | Example value |
| --- | --- |
| `GITHUB_APP__AUTHOR_CLAUDE_TOKENS` | `true` |
| `GITHUB_APP__AUTHOR_CLAUDE_MODEL` | `claude-sonnet-4-6` |
| `GITHUB_APP__PR_COMMANDS` | `["/review"]` |

The GitHub App must subscribe to Pull request events and have a configured webhook secret. The automatic
commands run when the author opens their PR, subject to the existing repository, draft, and action filters.
Other configured PR actions can use the token when the author also triggered the event. PR-Agent uses the
configured OpenAI fallback if a token is missing or Claude Code cannot complete the request. Manual, push,
and submitted-review commands use the regular backend and its configured model chain.
The **Reviewed with** line in the review identifies the model and whether it used a Claude Code setup token
or an OpenAI API key.

#### Enable the internal token screen

1. In the GitHub App settings, add `https://<app-host>/claude-token/callback` as a user authorization callback
   URL. Grant the app **Members: Read-only** organization permission and approve the updated permissions on
   the installation.
2. Give the app AWS credentials with `secretsmanager:GetSecretValue`, `secretsmanager:PutSecretValue`, and
   `secretsmanager:CreateSecret` scoped to `pr-agent/claude-code/<installation_id>/*`.
3. Configure the screen with these deployment settings.

   | Config var | Value |
   | --- | --- |
   | `GITHUB_APP__CLAUDE_TOKEN_ORG` | Your GitHub organization login |
   | `GITHUB_APP__CLAUDE_TOKEN_INSTALLATION_ID` | The organization's numeric GitHub App installation ID |
   | `GITHUB_APP__CLAUDE_TOKEN_CALLBACK_URL` | The HTTPS callback URL from step 1 |
   | `GITHUB_APP__CLAUDE_TOKEN_CLIENT_ID` | The GitHub App's Client ID |
   | `GITHUB_APP__CLAUDE_TOKEN_CLIENT_SECRET` | The GitHub App's Client secret |
   | `GITHUB_APP__CLAUDE_TOKEN_SESSION_SECRET` | A dedicated random secret, generated with `openssl rand -hex 32` |
   | `AWS_SECRETS_MANAGER__REGION_NAME` | The AWS region that stores the tokens |

Members visit `https://<app-host>/claude-token`, sign in to GitHub, and paste the token they generated with
`claude setup-token`. The app verifies active membership and stores the token under that member's numeric
GitHub ID. Members can replace or remove it from the same screen. Removing it clears the current stored value.
AWS Secrets Manager can retain older versions, so revoke the token in Claude when you want to invalidate it.

With the screen enabled, the app reads tokens only from AWS Secrets Manager for the configured installation.
Authors with existing `PR_AGENT_CLAUDE_TOKEN_<installation_id>_<user_id>` config vars need to register through
the screen. Administrators can remove those config vars after migration. Deployments without the screen can
still provision tokens using the config vars.

[Claude Code setup-token documentation](https://code.claude.com/docs/en/authentication#generate-a-long-lived-token)

### 3. Deploy to Heroku

```bash
# Deploy using Git
git push heroku main

# Or if your Heroku remote has a different name:
# git push heroku main:main
```

### 4. Verify Deployment

```bash
# Check app status
heroku ps -a pr-agent-app

# View logs
heroku logs --tail -a pr-agent-app

# Check config vars (should be the same as before)
heroku config -a pr-agent-app
```

## Configuration

Your existing environment variables should work without changes:
- `GITHUB.APP_ID`
- `GITHUB.PRIVATE_KEY`
- `GITHUB.WEBHOOK_SECRET`
- `OPENAI.KEY`
- `CONFIG.DEPLOYMENT_TYPE=app`
- `GIT_PYTHON_REFRESH=quiet`
- `GUNICORN_WORKERS` - optional; unset lets `gunicorn_config.py` pick 2-4 workers from the dyno's CPU limit

## What Changed?

1. **Procfile**: Added to tell Heroku to run gunicorn with the GitHub app server

Upstream `gunicorn_config.py` already binds to Heroku's `PORT`.

## Troubleshooting

### If deployment fails:

1. **Check build logs**:
   ```bash
   heroku logs --tail -a pr-agent-app
   ```

2. **Verify Python version**:
   ```bash
   heroku run python --version -a pr-agent-app
   ```

3. **Check if dependencies install correctly**:
   ```bash
   heroku run 'uv pip list' -a pr-agent-app
   ```

### If app still crashes:

1. **Check memory usage**:
   ```bash
   heroku ps:exec -a pr-agent-app
   # Then run: free -h
   ```

2. **Review dyno metrics**:
   - Go to Heroku dashboard → Metrics tab
   - Check memory and CPU usage

3. **Try different worker settings**:
   ```bash
   heroku config:set GUNICORN_WORKERS=1 -a pr-agent-app
   heroku restart -a pr-agent-app
   ```

## Rollback to Docker

If you need to rollback:

```bash
heroku stack:set container -a pr-agent-app
# Then redeploy with Docker
```

## Expected Benefits

- ✅ Lower memory footprint
- ✅ Faster startup time
- ✅ Better integration with Heroku's monitoring
- ✅ Easier to debug issues
- ✅ No Docker layer overhead

## Next Steps

After successful deployment:

1. Monitor the app for stability
2. Test webhook functionality
3. Check if the ~50 second crash issue is resolved
4. Adjust dyno size if needed (may be able to downgrade from Performance-M)
