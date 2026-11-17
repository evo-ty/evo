# EVO final interface update

## What changed

- Added a dedicated `Terminal` item to the authenticated Dashboard sidebar.
- Added `/terminal`, a Termux-inspired workspace shell with server selection, quick commands, command history, prompt path, output area, and responsive layout.
- Added `POST /terminal/execute/<folder>` in `main.py` for commands scoped to the selected server workspace.
- Terminal supports shell pipelines, `cd` within the workspace, Python and pip tooling, `.venv` PATH preference, package installation, and file inspection.
- Terminal output is limited to 16 KB per request and execution is limited to 90 seconds. System-level commands are blocked; long-running application processes should use the server START control.
- Updated the Dashboard Console to use the new terminal endpoint and improved its visual treatment.
- Re-skinned Dashboard, Guide, Signup, Account Settings, Admin Login, Admin Panel, and Admin User Management with the Kinetic Grid charcoal/lime/violet system.
- Existing Python/Flask routes, templates, file APIs, authentication, server controls, and Jinja structure remain in place.

## Validation

`main.py` passed Python compilation. The Terminal JavaScript passed Node syntax validation. Authenticated tests passed for `/`, `/login`, `/signup`, `/dashboard`, `/my-files`, `/terminal`, `/guide`, `/account-settings`, `/server-status/<folder>`, `/admin`, `/admin/panel`, and a live command execution returning `EVO_FINAL_OK`.


## EVO HUB server identity update

Each server now has a unique eight-digit `public_id` and an independent public route such as `/server/95287424`. The Dashboard exposes this route for each server, and the public server page is branded `EVO HUB` with route, runtime, and status details. Server startup now passes its own public port plus `EVO_HUB_SERVER_ID` and `EVO_HUB_SERVER_ROOT` to the child process. The legacy `/server-status/<folder>` path remains available for compatibility.

The Terminal and Dashboard Console surfaces now display `EVO HUB` branding. Application-level ownership and path checks remain active. Full OS/container isolation is documented as a production requirement when running mutually untrusted code; a folder boundary alone is not treated as a complete security boundary.
