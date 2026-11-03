# A Project's Skills are mounted over the user-level skills folder

A Project's Skills reach its environment as a read-only volume mounted at `~/.claude/skills`, on top of the Claude config volume that all of the owner's Projects share. That is the only place Claude Code reads skills from that is outside the repository, follows every worktree a Session spawns, belongs to one Project, and keeps Skills invoked by their bare name (`/name`). We accept that personal skills a User puts in the shared `~/.claude/skills` are hidden inside every Project: Skills reach a Project only through the Instance's catalog, or through the repository's own `.claude/skills/`, which Claude Code ranks below user-level skills, so a Project's Skill wins over a repository skill of the same name.

## Considered Options

- **The repository's `.claude/skills/`, kept out of git** (`.git/info/exclude`): rejected because untracked files do not reach the worktrees Remote Control spawns a Session in, and Dockyard would write into the User's code.
- **The shared Claude config volume itself**: rejected because every Skill would reach all of the owner's Projects.
- **A local plugin loaded with `--plugin-dir`**: rejected because `claude remote-control` does not take the flag, so spawned Sessions are not known to load it, and plugin skills are invoked with a prefix (`/plugin:name`).

## Consequences

Docker leaves an empty, root-owned `skills` mount point in the shared Claude config volume, which other environments mounting that volume (the login environment) see as an empty folder.
