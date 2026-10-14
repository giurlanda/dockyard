# The Git Credential is readable by Claude inside the Project

Claude Code must use `gh` and `glab` (issues, PRs, pipelines) and run `git push`, so the Project's Git Credential enters its environment: at startup Dockyard writes it into the `gh`/`glab` config files on a `tmpfs` and registers them as git credential helpers. We accept that Claude, and any repository code running with its permissions, can read the token; the defence is a fine-grained token with minimal scopes, flagged in the UI.

## Considered Options

- **Environment variables** (`GH_TOKEN`, `GITLAB_TOKEN`): rejected because the token stays in clear text in `docker inspect` and in the container config on disk, and rotating it requires recreating the container.
- **Egress proxy injecting the `Authorization` header**: the token would never enter the container, but it is too complex for the MVP and some tools fail without a local token.
