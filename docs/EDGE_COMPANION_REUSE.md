# Official-source reuse decision — 2026-09-22

| Candidate | Maintenance observed via GitHub API | Reuse and decision |
| --- | --- | --- |
| GoogleChrome/chrome-extensions-samples/api-samples/nativeMessaging | Repo push 2026-09-18; sample changed 2024-02-20; not archived; Apache-2.0 repo, some older BSD headers | MV3/connectNative/stdio/deployment pattern; primary reference, thin adapter, no fork or copied host |
| microsoft/MicrosoftEdge-Extensions/Extension-samples | Repo push 2026-09-08; sample directory changed 2025-08-29; MIT | Popup/content-script/sideloading reference; no native host sample; broad permissions not reused |
| MicrosoftEdge/Demos | Repo push 2026-09-21; MIT | DevTools demos, no Native Messaging executor; not selected |

Repository activity does not imply a sample is recently maintained. None provides reliable purchase intents, challenge recovery, receipt verification or no-charge order semantics. MVP keeps current Core and adds a small official-protocol bridge. Resources: existing Edge, one MV3 worker, one Python host, existing SQLite. No server or added production dependency. Provider selectors still need maintenance.

Sources:

- [Chrome native messaging sample](https://github.com/GoogleChrome/chrome-extensions-samples/tree/main/api-samples/nativeMessaging)
- [Edge extension samples](https://github.com/microsoft/MicrosoftEdge-Extensions/tree/main/Extension-samples)
- [Edge demos](https://github.com/MicrosoftEdge/Demos)
- [Edge Native Messaging](https://learn.microsoft.com/en-us/microsoft-edge/extensions/developer-guide/native-messaging)
- [Chrome Native Messaging](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging)
- [MV3 lifecycle](https://developer.chrome.com/docs/extensions/develop/concepts/service-workers/lifecycle)
- [Edge sideloading](https://learn.microsoft.com/en-us/microsoft-edge/extensions/getting-started/extension-sideloading)
- [macOS sample installer](https://github.com/GoogleChrome/chrome-extensions-samples/blob/main/api-samples/nativeMessaging/host/install_host.sh)

Improvements over examples: UTF-8 byte-length framing; 64 KiB bound; exact extension origin; strict schema/correlation; textContent popup; no arbitrary native command/URL/selector; durable pre-dispatch markers; no replay after disconnect. Public extension key stabilizes ID, grants no session access.
