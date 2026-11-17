# EVO HUB — Updated build

This build keeps the control panel and backend in Python/Flask. It includes per-account one-server enforcement, per-server ownership checks, path traversal protection, safe ZIP extraction, file upload, browser editing, search, custom domain storage, EVO console branding, a dedicated workspace Terminal, and optional Node.js project runtime support.

## Workspace Terminal

The sidebar now includes a dedicated `/terminal` page styled like a focused Termux-style workspace. Select a server, inspect its files with commands such as `pwd` and `ls -la`, check the Python runtime, create or use `.venv`, and install project dependencies with `pip install -r requirements.txt`.

Terminal commands execute inside the selected server's own `storage/instances/<folder>` directory. The working directory is kept per browser session and server, output is capped at 16 KB, and each request has a 90-second timeout. When a pip command is used, EVO automatically prepares or prefers that server's isolated `.venv`, so `pip --version`, `pip install ...`, and `python3 -m pip ...` use the correct project environment rather than the host's uv wrapper. A bare package spec such as `pyTelegramBotAPI==4.22.0` is also recognized and run as `pip install pyTelegramBotAPI==4.22.0`. System-level commands such as `sudo`, `shutdown`, `mount`, and destructive root commands are disabled. Use the server START/STOP controls for long-running application processes.

## Run

Install the Python dependencies used by the project, then run:

```bash
PORT=13506 ADMIN_USERNAME=CHANGE_THIS_ADMIN_USERNAME ADMIN_PASSWORD='ضع_كلمة_مرور_قوية_هنا' python3 main.py
```

The application reads `PORT` from the hosting platform. If it is not provided, the default is `13506`. Set `ADMIN_EMAIL` optionally. Do not put passwords or tokens directly in source files.

## Runtimes

The panel itself remains Python. A newly created Python server uses `main.py` and `python3`; a Node.js project server uses `index.js` and `node` when the Node.js option is selected.

## Custom domains

Enter a domain you own during server creation. Point its DNS record to the server's public address. The application stores and displays the domain; DNS and reverse-proxy configuration must be completed on the hosting machine.

## Docker isolation

Docker is not installed or available in the current sandbox, so this package does not pretend to provide Docker isolation. The application includes account ownership isolation and path safety at the application layer. For production-grade container isolation, deploy on a host with Docker enabled and add per-server containers, non-root users, CPU/memory/PID limits, a read-only base image, an isolated network policy, and an explicit allowlist for outbound traffic before exposing untrusted projects.


## Public server identity and isolation

Each server receives a unique eight-digit `public_id`. The Dashboard exposes a public route such as `https://your-host/server/95287424`, while the legacy `/server-status/<folder>` route remains available for compatibility. The public route identifies one server and displays its EVO HUB service page; it does not expose another account's files.

At the application layer, ownership checks restrict server, file, and terminal operations to the authenticated owner, and Terminal commands are kept inside the selected server workspace. A truly hostile multi-tenant deployment still requires OS-level isolation, so production deployments should run each user server in a separate non-root container or sandbox with CPU, memory, process, filesystem, network, and seccomp limits. A per-folder path is not a substitute for a container boundary.


## Inline Terminal and administrator credentials

The Terminal uses an inline shell prompt inside the output area. Users can type commands after `$`, edit them with Backspace, clear the current line with the X control or `Escape`/`Ctrl+U`, and run with Enter. `Clear` removes the visible output and `History` removes the browser-side command history.

Set administrator credentials at deployment time instead of placing a password in `main.py`:

```bash
export ADMIN_EMAIL='your-admin-email@example.com'
export ADMIN_PASSWORD='use-a-long-random-password'
python3 main.py
```

The public server route does not require a login, but `/dashboard`, `/terminal`, `/my-files`, server controls, and administrator routes remain protected. A public route is intended to expose the selected server application/status, not the control plane.


## Public server URL versus owner workspace

The short URL `/server/<public_id>` is the permanent server address. When the authenticated owner opens it, EVO HUB redirects to `/server/<public_id>/workspace`, where Files, Terminal, Console, upload, editing, and Start/Stop controls are available. An anonymous visitor remains on the public application/status view and cannot upload files or execute commands. This separation is intentional: exposing upload and arbitrary command execution without authentication would allow anyone who discovers the URL to take over the server.
