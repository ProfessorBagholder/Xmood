# Xmood

Xmood is a local web app that pulls recent X posts — and optionally Reddit posts via [SocialCrawl](https://www.socialcrawl.dev) — for a ticker or a Yahoo industry, then scores each post with the `grok` command on the same machine.

## Requirements

- Python 3.11+
- An [X API](https://console.x.com) bearer token with recent search access, and/or a SocialCrawl API key for Reddit
- The [Grok CLI](https://grok.com) installed and signed in (`grok` on `PATH`, or set `GROK_BIN`)

Reddit search is optional. Set `SOCIALCRAWL_API_KEY` to include one page of `GET /v1/reddit/search` (1 credit per search page). Scoring still uses local `grok` on the post text. This app does not call SocialCrawl `/v1/content_analysis/sentiment`.

If `X_BEARER_TOKEN` is set, Pull works with X only. If `SOCIALCRAWL_API_KEY` is also set, Pull merges Reddit into the same Grok pass. If only the Reddit key and grok are available, Pull is Reddit-only. If neither source key is set, Pull returns a clear error.

## Setup

```bash
git clone https://github.com/ProfessorBagholder/Xmood.git
cd Xmood
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set `X_BEARER_TOKEN` and, if you want Reddit, `SOCIALCRAWL_API_KEY`. Do not commit `.env`.

If the Grok binary is not named `grok` or is not on `PATH`, set `GROK_BIN` to its full path.

```bash
python3 server.py
```

Open [http://127.0.0.1:8787](http://127.0.0.1:8787). Symbol is the default: type a ticker, pick a Yahoo listing, then Pull. Switch to Sector to pick a Yahoo industry, or type a theme Yahoo does not list and Pull that.

After a ticker result, the muted industry line under the gauge runs a sector pull for that Yahoo industry.

To restart after an update, stop the running `python3 server.py` process (Ctrl+C in that terminal) and start it again from the repo:

```bash
source .venv/bin/activate
python3 server.py
```

About $0.50 per 100 posts is billed to the X account that owns the token.
