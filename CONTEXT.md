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
An Administrator registering a person's email; without an Invitation nobody becomes a User, even if the authentication proxy lets them through. It is used up when the person first reaches the Instance and becomes a User, through the authentication proxy or by opening their Activation link, whichever comes first.

**Claude Account** (Account Claude):
The claude.ai subscription a User connects, shared by all of that User's Projects and never visible to other Users.
_Avoid_: Claude login, Claude credentials, API key

**Git Credential** (Credenziale Git):
A GitHub or GitLab access token stored in a User's profile and reusable across that User's Projects; Claude can read it inside the Projects that use it.
_Avoid_: token (alone), secret, key

**Local sign-in** (Accesso locale):
A User proving who they are to the Instance with their email, their password and, if they turned it on, their Second factor, without the authentication proxy. Accepted only from the Local networks, never from the internet. The same person is the same User whichever way they come in.
_Avoid_: local login, password login, LAN mode

**Local networks** (Reti locali):
The networks (the LAN, a VPN) from which the Instance accepts Local sign-in, set in the Instance configuration.
_Avoid_: trusted networks, whitelist

**Activation link** (Link di attivazione):
A single-use, expiring link an Administrator hands to a person so they can set the password of their Local sign-in: with a new Invitation, or later to reset a forgotten password. Making a new one voids the previous one.
_Avoid_: reset token, magic link

**Second factor** (Secondo fattore):
A code from an authenticator app that a User may choose to require at their Local sign-in, together with the single-use recovery codes that stand in for it. An Administrator can remove it from a User, who can then turn it on again.
_Avoid_: 2FA, MFA, OTP (alone)

**Code step** (Passaggio del codice):
The part of a Local sign-in between the right password and the code of the User's Second factor, for a User who has one. It lasts a few minutes and works once: when a code is accepted it becomes a Sign-in session; when it runs out the person starts again from the password. Not a Sign-in session.
_Avoid_: challenge, second step, MFA prompt

**Sign-in session** (Sessione di accesso):
The time a browser stays signed in to the Instance after a Local sign-in, until the User signs out, stops using it for a while, or loses it when their password changes or they are removed. Not a Session.
_Avoid_: session (alone), login session

### Working on code

**Project** (Progetto):
A repository cloned by a User, with its own dedicated environment where Claude Code runs.
_Avoid_: repo, workspace, container, instance

**Active Project** (Progetto attivo):
A Project that is not stopped: being cloned, running, waiting for its owner's Claude Account, or failing. A User may have only as many as the Instance limits allow.

**Instance limits** (Limiti dell'Istanza):
The most CPU and RAM one Project may use (also what it gets unless its User chooses less) and the most Active Projects per User, set in the Instance configuration.
_Avoid_: quota

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
