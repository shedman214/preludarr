# Security policy

## Reporting a vulnerability

**Please do not report security problems in public Issues, Discussions or pull requests.**

Report them privately through GitHub:

1. Open the repository's **Security** tab.
2. Choose **Report a vulnerability**.
3. Describe the problem, the version (System → Releases), how to reproduce it, and what an attacker could do.

Leave out real passwords, API keys, tokens, cookies, backup files and private paths. If a log is needed, remove those first.

You should get a first answer within 7 days. We will tell you when a fix is planned, credit you in the release notes if you want, and publish a security advisory once a fixed version is available.

If the **Report a vulnerability** button is missing, open a public issue that says only "I would like to report a security problem privately", with no details, and a maintainer will contact you.

## Supported versions

Preludarr is in beta. Only the **newest release** gets security fixes; please update before reporting.

| Version | Security fixes |
|---|---|
| Newest 1.12.x beta | Yes |
| Anything older | No |

## Scope

Examples of problems we want to hear about:
- Using Preludarr without signing in, or bypassing the first-run setup code.
- Cross-site request forgery or cross-site scripting in the web interface.
- Reading or writing files outside the folders Preludarr is meant to use (path traversal, symbolic links, backup archives).
- Making Preludarr fetch internal network addresses it should not (server-side request forgery).
- Running commands through file names, URLs or uploaded files.
- Leaking API keys, tokens or passwords in pages, logs or error messages.

Not in scope: problems that need an attacker who is already signed in as the owner, attacks that need physical or root access to the server, and denial of service by sending very large amounts of traffic from many machines.

## How Preludarr limits damage
- Every page needs sign-in. A new install is locked until the owner enters the one-time setup code from the container log.
- The API key is read-only by default and accepted only in the `X-Api-Key` header.
- The container runs as a normal user (`PUID`/`PGID`), not root.
- Preludarr only writes `theme.mp3` and its own data; see "What Preludarr changes on disk" in the README.

These measures reduce risk. They are not a guarantee, and Preludarr has not had an independent security audit.
