# Infrastructure and private research

This repository distributes software, general instructions, templates and
synthetic tests. Fresh installation creates empty research catalogs. It does
not distribute a personal hub's research claims, papers, notes, conversations,
workspace identifiers, native session IDs, absolute home paths or model caches.

Your own hub can contain unpublished mathematics and private conversation
summaries. Keep it separate from the public kit repository. `.gitignore` is a
convenience, not a security boundary: already tracked files remain tracked and
research notes/records may still be included in a local backup. Review the exact
files before any upload. Do not publish your whole hub as a software update.

The local semantic provider accepts only an uncredentialed loopback endpoint and
refuses redirects. Native adapters read bounded local metadata and selected
excerpts; they do not read credential stores. Research sent to an AI assistant
still follows the account and permissions you choose for that service.

If you publish your own infrastructure changes, start from a clean copy of this
kit and copy only reviewed code/template changes. Exclude `.hub/`, `.env*`, keys,
catalogs, library files, investigations, archives, native histories and personal
assistant configuration. Scan the complete proposed upload, including tests and
documentation. The project makes no claim that a filename filter alone detects
every possible secret.
