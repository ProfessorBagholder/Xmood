# Xmood

Local ticker mood from X posts. Run on this computer. Score with the `grok` command.

## Setup

```bash
cd ~/dev/xmood
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Put `X_BEARER_TOKEN` in `.env` (https://console.x.com). Never commit `.env`. Only `.env.example` belongs on GitHub.

Git in this folder uses the ProfessorBagholder GitHub user.

If `grok` is not on PATH, set `GROK_BIN` in `.env` to that file.

```bash
python3 server.py
```

Open http://127.0.0.1:8787
