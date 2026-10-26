# Automatic project directories: remaining implementation boundary

Status: proposed, not implemented. Existing user terminal sessions stay running.

Session.cwd is assigned at terminal creation. WinPty records the identity of the
spawned process, which can be a shell or launcher. An owned experiment proved a
venv launcher stayed in the synthetic home while its Python child changed to a
project directory. This disproves treating that launcher's OS directory as the
active CLI project.

source_path: `src/supermonoagent/pty.py`, `src/supermonoagent/hub.py`,
`src/supermonoagent/file_companion.py`, main workspace
`.omx/artifacts/six-cli/project-cwd-owned-probe.json` and
`.omx/artifacts/six-cli/project-cwd-investigation.md`.

## First implementation lane

Add explicit managed launch recipes for Codex, Kimi, Grok, Claude CLI, pi.dev and
Cursor. The host knows which recipe it creates and retains the actual process
identity, parent, creation time and PTY generation. Wrapper implementations need
separate verification; executable names and the deepest descendant are not
authority. Keep ordinary shell startup and byte transport unchanged.

If a later PowerShell location hook is needed, use an authenticated local-only
out-of-band channel. Verify the OS-reported client PID and its bound generation,
not a PID supplied in the message. Do not trust OSC text to authorize filesystem
destinations, edit global profiles, or assume prompt-only reporting covers
`cd A; codex` before another prompt appears. PowerShell provider locations are
also distinct from process CWD.

Primary references: [PowerShell locations](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_locations?view=powershell-7.5),
[GetNamedPipeClientProcessId](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getnamedpipeclientprocessid).

## Existing sessions and limits

There is no verified general mechanism here to identify the semantic workspace
of every already-running nested CLI without cooperation. Do not restart those
sessions or claim manual directory selection completes automatic detection.
Conservative process-family CWD agreement may provide advisory OS-directory
evidence; it is not proof of an application's internal workspace.

Unknown identity, multiple plausible projects, unsupported architecture and
non-filesystem providers require an explicit unavailable status. Never silently
fall back to home/tmp.

## File safety gate

Automatic and explicitly selected projects both need a captured host-issued
project binding. Bind canonical directories and their filesystem identity to
the session and daemon generation, then recheck before download and final
publication. A settings revision alone cannot detect default project A changing
to B while both use revision zero. This binding is being implemented separately;
it must not be described as implementing automatic process/project detection.

Required verification includes wrapper PID differences, provider locations,
same-command directory changes, stale identities, parallel CLI branches,
process exits, old-daemon behavior, A-to-B uploads and unchanged PTY byte streams.
