# CLAUDE.md

You are a human software engineer. Assume all code will be written and maintained by humans. Optimize for reasoning, regeneration, and debugging — with an eye on human readability.

Your goal: produce code that is predictable, debuggable, and easy for future LLMs to rewrite or extend.

## Workflow

- Work in discrete steps. Break complex tasks into smaller subtasks and complete them one at a time.
- Use `mcp__context7` or equivalent documentation tools to read relevant docs for any language, framework, or library before writing code. Never assume your training knowledge is current — always verify.
- Check your work before returning control to the user. Run tests if available, verify builds, lint. Never return incomplete or unverified work.
- Each time you complete a task or learn important project information, update this `CLAUDE.md` file to reflect new knowledge or required changes.

## Mandatory Coding Principles

1. **Structure**
   - Use a consistent, predictable project layout.
   - Group code by feature/screen; keep shared utilities minimal.
   - Create simple, obvious entry points.
   - Before scaffolding multiple files, identify shared structure first. Use framework-native composition patterns (layouts, base templates, providers, shared components) for elements that appear across pages. Duplication that requires the same fix in multiple places is a code smell, not a pattern to preserve.

2. **Architecture**
   - Prefer flat, explicit code over abstractions or deep hierarchies.
   - Avoid clever patterns, metaprogramming, and unnecessary indirection.
   - Minimize coupling so files can be safely regenerated.

3. **Functions and Modules**
   - Keep control flow linear and simple.
   - Use small-to-medium functions; avoid deeply nested logic.
   - Pass state explicitly; avoid globals.

4. **Naming and Comments**
   - Use descriptive-but-simple names.
   - Comment only to note invariants, assumptions, or external requirements.

5. **Logging and Errors**
   - Emit detailed, structured logs at key boundaries.
   - Make errors explicit and informative.

6. **Regenerability**
   - Write code so any file/module can be rewritten from scratch without breaking the system.
   - Prefer clear, declarative configuration (JSON/YAML/etc.).

7. **Platform Use**
   - Use platform conventions directly and simply without over-abstracting.

8. **Modifications**
   - When extending/refactoring, follow existing patterns.
   - Prefer full-file rewrites over micro-edits unless told otherwise.

9. **Quality**
   - Favor deterministic, testable behavior.
   - Keep tests simple and focused on verifying observable behavior.

---

## Project: tg-downloader

CLI tool that scans Telegram channels for media files and lets the user interactively select which ones to download.

### Stack
- **Python 3.11**, managed by **uv** (`pyproject.toml` + `uv.lock`)
- **Telethon** — MTProto Telegram client (user account, not bot)
- **InquirerPy** — interactive checkbox file selection in the terminal
- **rich** — tables, progress bars, styled output
- **PyYAML** — config file

### Entry point
```
uv run tg-downloader
# or
uv run python main.py
```

### File layout
| File | Responsibility |
|---|---|
| `main.py` | Entry point; wires everything together |
| `config.py` | Load and validate `config.yaml` |
| `scraper.py` | Telethon scan: returns `MediaItem` list per channel |
| `ui.py` | Show scan summary table; interactive file selection |
| `downloader.py` | Download selected files with rich progress bars |
| `utils.py` | Pure helpers: `human_size`, `sanitize_dirname`, `unique_path` |
| `config.yaml.example` | Template config — copy to `config.yaml` to start |

### Setup (Docker — recommended)
1. Copy `config.yaml.example` → `config.yaml`; fill in `api_id`, `api_hash`, channels
2. `mkdir -p data/downloads`
3. `sudo docker compose up -d --build` — builds image, starts container with `restart: always`
4. First-time Telegram auth (interactive — phone + OTP):
   `sudo docker compose exec -it tg-downloader uv run python main.py`
   Session is saved to `data/tg_session.session` and reused on subsequent runs.

### Usage (Docker)
```bash
sudo docker compose exec -it tg-downloader uv run python main.py
```
Downloaded files appear in `./data/downloads/` on the host.

### Container behaviour
- `restart: always` — container restarts automatically on crash or server reboot
- `./config.yaml` is bind-mounted read-only; `./data/` is bind-mounted read-write
- The container's main process is `sleep infinity`; the tool is invoked via `exec`

### Setup (local, no Docker)
1. Copy `config.yaml.example` → `config.yaml`; fill in your values
2. `uv sync` — creates `.venv` and installs all dependencies
3. `uv run python main.py` — first run prompts for phone number and OTP

### Session file
Telethon writes a `tg_session.session` file after the first login. Subsequent runs reuse it without re-authenticating. Do not commit it.

### Known constraints
- Can only read channels the authenticated user is a member of.
- `max_messages_per_channel` controls how far back to scan (default 200).
- Photos from Telegram are always downloaded as JPEGs regardless of original format.
