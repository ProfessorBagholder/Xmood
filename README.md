# Xmood

Xmood is a local web app that pulls recent X posts for a ticker or a Yahoo industry and scores each post with the `grok` command on the same machine.

## Requirements

- Python 3.11+
- An [X API](https://console.x.com) bearer token with recent search access
- The [Grok CLI](https://grok.com) installed and signed in (`grok` on `PATH`, or set `GROK_BIN`)

## Setup

```bash
git clone https://github.com/ProfessorBagholder/Xmood.git
cd Xmood
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set `X_BEARER_TOKEN`. Do not commit `.env`.

If the Grok binary is not named `grok` or is not on `PATH`, set `GROK_BIN` to its full path.

```bash
python3 server.py
```

Open [http://127.0.0.1:8787](http://127.0.0.1:8787). Symbol is the default: type a ticker, pick a Yahoo listing, then Pull. Switch to Sector to pick a Yahoo industry (no free text) and Pull that.

After a ticker result, the muted industry line under the gauge runs a sector pull for that Yahoo industry.

To restart after an update, stop the running `python3 server.py` process (Ctrl+C in that terminal) and start it again from the repo:

```bash
source .venv/bin/activate
python3 server.py
```

About $0.50 per 100 posts is billed to the X account that owns the token.
