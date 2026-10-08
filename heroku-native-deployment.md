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
`pyproject.toml` sets `[tool.uv] required-version` to a range (upstream pins an exact uv)
because the buildpack bundles its own uv version. Re-check this after each upstream sync.

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
2. **pyproject.toml**: uv `required-version` relaxed to a range so the buildpack's uv can build

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













