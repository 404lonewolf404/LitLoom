# Security Notes

The application binds to `127.0.0.1` by default and is designed for a single local research workspace. API keys are accepted through the OpenAI-compatible settings panel, a local secret file, or environment variables and are deliberately kept out of SQLite, audit events, request bodies in logs, and backup manifests.

When the settings panel saves a new key, it stores it in `<LITERATURE_DATA_DIR>/model-secret.json` by default. Set `LIT_AGENT_API_KEY_FILE` to use another local JSON or plain-text file. The file is written atomically and uses restrictive permissions where the operating system supports them; protect the containing directory and do not commit it. The runtime resolves keys in this order: current process, local file, then `LIT_AGENT_API_KEY`. The settings response reports only the source and whether a key exists. Use “清除本地密钥” in the model settings panel to remove the local file.

The model configuration accepts `https` for remote providers and `http` only for loopback services. Before enabling an external provider, review its retention policy. The current AI workflow sends only the explicitly consented paper title, authors, year, venue, and abstract; local notes and PDF attachments are not uploaded.

Backups contain paper metadata, notes, audit history, and local PDF attachments. Treat backup ZIP files as sensitive research material. Store them in a protected directory and do not commit `data/` or backup archives to source control.

Future encrypted backup and system credential-store support must preserve the same constraints: secrets must not enter logs, exports, or model prompts without an explicit user action.
