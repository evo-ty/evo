## Kinetic UI verification

The project now uses a substantially different visual direction called Kinetic Grid: charcoal-black surfaces, warm white typography, acid-lime action states, violet secondary accents, mono labels, grid texture and compact operational cards.

The login page was rebuilt as `EVO | Access` with a private-workspace split layout, stronger hierarchy, new copy, lime action button, and the same `/login` POST flow and form IDs.

The files page was rebuilt as `EVO | File System` at `/my-files`, with an active-project side rail, project-file workspace, search, new file/folder actions, upload/drop zone, file actions, and editor modal. Existing `/servers`, `/files/list`, `/files/upload`, `/files/save`, `/files/rename`, `/files/download`, `/files/unzip`, and delete endpoints remain in use.

The dashboard and server management view now use the same Kinetic Grid palette. The Console has a darker terminal surface, lime prompt/accent states, terminal scan texture, clearer command input, and the EVO brand below the console remains supported by existing logic.

Validation completed: `main.py` compiled successfully; all 11 Jinja templates parsed successfully; `/` and `/login` returned HTTP 200; `/dashboard` correctly redirected unauthenticated users; `/my-files` rendered for the logged-in session; the dashboard management view and Console rendered in the browser.
