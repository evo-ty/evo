# Public server route verification

Each server is assigned an eight-digit public ID stored in the `servers.public_id` column. The authenticated Dashboard returns `public_id` and `server_url`, and the public route follows `/server/<public_id>`.

The route shows the EVO HUB server status page while the service is stopped. When the service is running, the same route proxies HTTP requests to that server's assigned local public port and adds `X-EVO-HUB-SERVER` plus `X-Forwarded-Prefix` headers. The legacy `/server-status/<folder>` route remains available.

Application-level ownership checks continue to protect `/server/`, `/files/`, and `/terminal/` operations. For untrusted code, full isolation still requires separate non-root containers or sandboxes; the folder boundary and route separation are not claimed as a complete host security boundary.


### Live browser check

The authenticated Dashboard now shows `server/95287424` on the server card. Opening that link loads `EVO HUB | Server` at `/server/95287424`, showing the isolated-server identity, status, public address, runtime, and route. When the server is offline, the route intentionally renders the EVO HUB status page; when it is online, the same route proxies to the selected app port.


### Terminal live check

The live Terminal page now displays `EVO HUB / SHELL`, loads the selected Python server, and successfully executes `ls -la` inside that server workspace. The output is scoped to the selected instance directory and the prompt returns to the workspace afterward.

A hardcoded Telegram bot token found in one sample Python server file was replaced with the `BOT_TOKEN` environment variable before packaging, so the secret is not shipped in the final archive.


### Manual input check

The live EVO HUB Terminal accepts a manually typed command without executing it immediately. The current line can be deleted with the X button, and the output and command history have separate clear controls. Keyboard editing remains available through Backspace, Escape, and Ctrl+U.


### Inline shell prompt check

The current live Terminal renders the prompt inside the terminal output area rather than as a separate bar below it. A manually typed `echo EVO HUB` command executed from the inline `$` prompt and returned `EVO HUB`, followed by a fresh prompt. The input remains editable before execution and supports the clear-line control.


### Full workspace check

The authenticated route `/server/95287424/workspace` now opens a complete EVO HUB server workspace. It shows the fixed public URL, a Files tab with upload/create/edit/delete controls, a Terminal tab with the inline prompt, a Console tab, and an Overview tab. The public `/server/95287424` route remains separate for serving the application without exposing write controls.


### Workspace upload check

A temporary file was uploaded successfully from the Files tab of `/server/95287424/workspace` and appeared in the selected server's file list. This confirms that the full workspace route is wired to the existing authenticated upload API. The test file is removed before packaging.


### Owner URL redirect check

When the signed-in owner opens the short public URL `/server/95287424`, Flask redirects the owner to `/server/95287424/workspace`. The workspace shows Files, Terminal, Console, and Overview controls. Anonymous visitors continue to receive the public application/status behavior and never receive file-write or command-execution controls.


### Same-site URL state check

The authenticated `/servers` response contains the existing server `ijhvvgg` with public ID `95287424` and server URL `/server/95287424`. The dashboard remains the same EVO HUB control-center UI; URL state is being synchronized from the selected server rather than opening a different product page.


### Verified in live browser

After the JavaScript syntax fix, Dashboard loaded the server card for `ijhvvgg` (`public_id=95287424`). Clicking the card changed the browser URL to `/server/95287424` via history state while preserving the same EVO HUB dashboard shell and management view. No new site or login page opened.


### Final identity and direct-link check

After restarting Flask with the updated templates, opening the direct URL `/server/95287424` in the authenticated browser kept the session active, displayed the same Dashboard management shell, and showed the title `EVO HUB | Control Center`. The visible node label was updated to `EVO HUB-PYTHON`, and no legacy EVO text remained in the HTML templates.
