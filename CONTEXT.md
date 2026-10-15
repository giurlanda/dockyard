# Dockyard

Open source, self-hosted, multi-user product that takes the URL of a GitHub or GitLab repository and prepares an isolated environment where Claude Code works on the code and can be driven remotely (phone, browser).

Canonical terms are English; the Italian UI label is given in brackets.

## Language

### People and access

**Instance** (Istanza):
One installation of Dockyard on the hardware of a team or a person, with its own Users and Projects.
_Avoid_: server, deployment, tenant

**User** (Utente):
A person registered on an Instance, owner of their own Projects and Git Credentials. Removing a User deletes everything they own on the Instance, their Claude Account included.
_Avoid_: account, customer, owner

**Administrator** (Amministratore):
A User who manages the Instance: Users, limits and dangerous settings. Whoever installs the Instance is the first Administrator.
_Avoid_: admin, superuser, root

**Invitation** (Invito):
An Administrator registering a person's email; without an Invitation nobody becomes a User, even if the authentication proxy lets them through. It is used up when the person first reaches the Instance and becomes a User.

**Claude Account** (Account Claude):
The claude.ai subscription a User connects, shared by all of that User's Projects and never visible to other Users.
_Avoid_: Claude login, Claude credentials, API key

**Git Credential** (Credenziale Git):
A GitHub or GitLab access token stored in a User's profile and reusable across that User's Projects; Claude can read it inside the Projects that use it.
_Avoid_: token (alone), secret, key

### Working on code

**Project** (Progetto):
A repository cloned by a User, with its own dedicated environment where Claude Code runs.
_Avoid_: repo, workspace, container, instance

**Visibility** (Visibilità):
Whether a Project's repository is public or private. A private Project requires a Git Credential; a public one may have one (to open issues and PRs). Either way at most one, from the same provider as the repository.

**Image** (Immagine):
A predefined environment for Projects, with a label, a description and a Dockerfile, created by an Administrator; the User picks one when creating a Project.
_Avoid_: template, environment, stack

**Base Image** (Immagine base):
The Image shipped with Dockyard (Claude Code, git, `gh`, `glab`, Node, Python) from which every other Image derives.

**Privileged Project** (Progetto privilegiato):
A Project whose environment has full access to the host (equivalent to root on the machine); only a User granted this permission by an Administrator can create one.
_Avoid_: root project, admin project

**Remote Control Server** (Server Remote Control):
The always-running process of a Project that accepts requests from the phone or browser and opens a new Session for each.

**Terminal** (Terminale):
An interactive shell in a Project's environment, opened from the browser, where the User runs git, `gh`/`glab` or Claude Code interactively.
_Avoid_: console, web shell

**Session** (Sessione):
A Remote Control conversation with Claude Code inside a Project, driven from the Claude app or the browser; a Project can have several in parallel, each in its own worktree.
_Avoid_: chat, run, job
