# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 1.0.1   | :white_check_mark: |
| < 1.0.1 | :x:                |

## Security Architecture & Threat Model

Codex Liquid Glass injects custom theme styling into the official macOS Codex desktop application using external runtime automation and the local Chromium/Electron debugging protocol (CDP).

### 1. Local Loopback CDP Port
- **No Remote Exposure:** The debugging interface is bound strictly to loopback (`127.0.0.1:9236`). Never bind, expose, forward, or proxy this port to public networks or untrusted devices.
- **Local Client Trust:** The local CDP interface does not perform client authentication. Any local process under the user session that can reach loopback TCP could interact with the debugging session. Run only in trusted local workstation environments.
- **Port Termination:** Stopping the theme daemon unregisters injected hooks, but closing the debug listener requires quitting the Codex application normally and launching the official application directly.

### 2. Process Verification & Isolation
- **UID & Process Matching:** Runtime scripts verify that the listener on the debug port belongs to the current user UID and matches the verified official application executable path and expected PID.
- **Document Guards:** Injections into the renderer verify document URL boundaries (`app://-/index.html` and `app://-/detached-window.html`) to prevent executing styles or scripts if a window navigates to external web content.
- **Symbolic Link Rejection:** Release packaging scripts strictly reject symbolic links, broken links, and directory links in packageable assets to prevent accidental inclusion of external files in distribution archives.

## Reporting a Vulnerability

If you discover a security vulnerability or boundary defect in this project:

1. **Do not open a public issue.**
2. Report the vulnerability privately via [GitHub Security Advisories](https://github.com/QuinnFreeze/codex-theme/security/advisories) or contact the project maintainer directly.
3. Include detailed steps to reproduce the issue, the affected version, and minimal proof-of-concept evidence.
4. Allow reasonable time for remediation before any public disclosure.
