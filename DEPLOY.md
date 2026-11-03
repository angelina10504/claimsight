# Deploying ClaimSight (Requirement 11)

This is a click-through. Follow it top to bottom and you never have to guess.
Two things go to the cloud, on free tiers; everything else stays on your laptop
on purpose (see *What is deliberately not deployed* at the end).

```
  your laptop                         the cloud (free tiers)
  -----------                         ----------------------
  ADK Claimant Assistant   --A2A-->   A2A server  (Hugging Face Spaces, Docker)
  (run_assistant.py)                  = the whole CrewAI back office + MCP tools

  (anyone's browser)       ------->   Dashboard   (Streamlit Community Cloud)
                                       presentation mode: saved results, no model
```

The A2A server is one Docker image: it runs `src/a2a_server.py`, which carries
the CrewAI Flow and launches the six MCP tools (`./claimsight_tools`) as a child
process. One container is the entire assessment system. It is built by the
`Dockerfile` at the repo root and reads every secret from the environment -
nothing is baked into the image.

Before you start you need:
- a **Hugging Face** account (huggingface.co) and a **GROQ_API_KEY** (the same
  one in your local `.env`),
- a **GitHub** repo holding this project (for Streamlit),
- optionally a **Langfuse Cloud** key pair if you want traces in the cloud.

Never paste a key into a file, a screenshot, or a chat. Keys go only into the
platforms' secret stores, as below.

---

## Part A — the A2A server on Hugging Face Spaces (Docker SDK, free tier)

### A1. Create the Space
1. huggingface.co → your avatar → **New Space**.
2. **Owner**: you. **Space name**: `claimsight-a2a` (any name; remember it).
3. **License**: your choice. **Select the SDK**: **Docker** → **Blank**.
4. **Hardware**: *CPU basic · free*. **Visibility**: Public (simplest) or Private.
5. Click **Create Space**. Note the URL it gives you. It looks like:

   ```
   https://<your-username>-claimsight-a2a.hf.space
   ```

   Your username and the space name are lower-cased and every non-letter becomes
   a hyphen. This is the **public URL**; the agent card must advertise it.

### A2. Tell the Space which port to route to
Hugging Face reads Docker Space settings from YAML front matter at the top of
`README.md`. Add this block as the very first lines of `README.md` (above the
existing title) before you push:

```yaml
---
title: ClaimSight A2A
emoji: 🚗
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 9999
pinned: false
---
```

`app_port: 9999` matches the container's `EXPOSE 9999` and `A2A_PORT=9999`, so
Hugging Face routes the public URL to the server. (If you ever change the port,
change it in both places.)

### A3. Set the secrets and variables
In the Space: **Settings → Variables and secrets**. Add:

| Name | Kind | Value | Why |
|---|---|---|---|
| `GROQ_API_KEY` | **Secret** | your Groq key | the models the crews call |
| `A2A_PUBLIC_URL` | Variable | `https://<your-username>-claimsight-a2a.hf.space` | the address the agent card advertises; the ADK client reads the card and then calls this, so it MUST be the real public URL, not localhost |
| `LANGFUSE_PUBLIC_KEY` | Secret | *(optional)* | turn on cloud traces |
| `LANGFUSE_SECRET_KEY` | Secret | *(optional)* | " |
| `LANGFUSE_HOST` | Variable | *(optional)* e.g. `https://cloud.langfuse.com` | " |

You do **not** set `A2A_HOST` or `A2A_PORT`: the image already binds `0.0.0.0`
on `9999`. You do **not** set `CLAIMSIGHT_OFFLINE` (that is for tests). If you
leave the Langfuse keys out, tracing is simply off and the server runs fine.

### A4. Push the repo to the Space
The Space is a git repo. From your project folder:

```bash
git remote add space https://huggingface.co/spaces/<your-username>/claimsight-a2a
git push space main
```

(If git asks for a password, use a Hugging Face **access token** with *write*
scope from huggingface.co → Settings → Access Tokens, not your account
password.)

Hugging Face now builds the `Dockerfile`. Watch **Logs** → **Build** in the
Space. First build takes a few minutes (it resolves the whole `uv.lock`). When
it finishes you will see the server line in **Logs** → **Container**:

```
ClaimSight A2A server on https://<your-username>-claimsight-a2a.hf.space  (card: .../.well-known/agent-card.json)
```

### A5. Verify the public card
From anywhere:

```bash
curl https://<your-username>-claimsight-a2a.hf.space/.well-known/agent-card.json
```

It must return JSON whose `supported_interfaces[0].url` is exactly your
`A2A_PUBLIC_URL`. If it still shows `http://localhost:9999`, `A2A_PUBLIC_URL`
was not set when the container started — set it (A3) and **Settings → Factory
rebuild**.

---

## Part B — the dashboard on Streamlit Community Cloud (free tier)

The dashboard's **Presentation** mode is the default. It reads saved results
from the repo and calls no model, so it needs no secrets and is safe to show
publicly.

### B1. Give the presentation something to show
Presentation reads from `data/decisions/`, `data/logs/` and `data/reports/`,
which are **gitignored** (they are runtime output). A fresh clone has none, so
the cloud dashboard would show "0 processed". To populate it, commit a curated
handful of already-assessed claims:

```bash
uv run python run_flow.py CLM-5000     # and a few more you want to show
git add -f data/decisions/CLM-5000.json data/logs/CLM-5000.jsonl data/reports/CLM-5000.*
git commit -m "Sample results for the presentation dashboard"
git push
```

Commit only claims you are happy to show. **Never** add any claim whose split is
`test` in `data/manifest.json` — those 40 are sealed.

### B2. Deploy
1. Push the repo to GitHub if it is not already there.
2. share.streamlit.io → **Sign in with GitHub** → **Create app** → **Deploy a
   public app from GitHub**.
3. **Repository**: your repo. **Branch**: `main`.
4. **Main file path**: `dashboard/app.py`.
5. **Deploy**. Streamlit installs from `pyproject.toml` and starts the app.

No secrets are required. Leave the mode on **Presentation**. Do not switch the
cloud dashboard to **Live assessment** — live mode runs the real Flow, which
needs the Groq key, the MCP server and the embeddings model, none of which exist
in Streamlit's runtime. Live mode is for your laptop.

---

## Part C — point your local assistant at the deployed server

The ADK Claimant Assistant stays on your laptop and talks to the cloud A2A
server over the A2A protocol. Tell it the deployed address with one variable:

```bash
export CLAIMSIGHT_A2A_URL=https://<your-username>-claimsight-a2a.hf.space
uv run python run_assistant.py
```

(On Windows PowerShell: `$env:CLAIMSIGHT_A2A_URL="https://..."`.) The assistant
reads the agent card at that URL (`agents/claimant_assistant/agent.py`), then
sends claims to the cloud. Answer **N** to CrewAI's share-trace prompt as always.
Unset the variable to go back to a local server on `http://localhost:9999`.

---

## Free-tier caveats — read before any live demo

- **Cold start.** A Hugging Face Docker Space that has been idle goes to sleep
  and the next request waits while the container boots (up to a few minutes on
  first wake). Streamlit apps sleep too and show a "wake this app" button.
- **Idle sleep.** Free Spaces sleep after about 48 hours of no traffic; free
  Streamlit apps sleep after a shorter idle. They do not lose data, they just
  need waking.
- **Wake it first.** A few minutes before you present, hit the card once so the
  Space is warm:
  ```bash
  curl https://<your-username>-claimsight-a2a.hf.space/.well-known/agent-card.json
  ```
  and open the Streamlit URL in a tab. Do not let the first click of the demo be
  the thing that wakes a cold container.
- **Groq limits.** The free Groq tier caps tokens per minute and per day. The
  pipeline already spaces and retries requests (`src/config.py`), but a burst of
  live assessments can still hit a limit; if so, wait out the minute.
- **Embeddings for coverage RAG.** The Policy Analyst retrieves policy clauses
  with a local embeddings model (`ollama nomic-embed-text`, see
  `src/crews/assessment_crew.py`). The free container has no ollama, so a *live
  end-to-end* assessment in the cloud needs `OLLAMA_BASE_URL` set to a reachable
  embeddings server (for a demo, an ollama on your laptop exposed through a
  tunnel). Without it the card, the protocol and guardrail boundary, intake and
  the fraud/settlement tools still work, but coverage retrieval errors and the
  claim comes back as "could not be completed". The most reliable live demo runs
  the assistant against your **local** A2A server (unset `CLAIMSIGHT_A2A_URL`);
  the cloud Space is there to prove the deployment and serve the public card.

---

## What is deliberately NOT deployed, and why

- **The ADK Claimant Assistant** runs locally, not in the cloud. That is the
  point of the A2A link: a local ADK agent talking to a remote CrewAI system
  across a standard protocol. Deploying both would hide the boundary we are
  demonstrating.
- **Live assessment on the Streamlit dashboard.** The cloud dashboard is
  presentation-only: saved results, no model, no keys. Live running belongs on a
  machine that has the Groq key, the MCP server and the embeddings model.
- **The sealed test split.** The 40 claims marked `split: "test"` in
  `data/manifest.json` have never been run and must not be, anywhere - not for a
  demo, not for tuning, not for an accuracy claim. They are excluded from every
  deployment by not being invoked.
- **The 2.3 GB VehiDE images.** Gitignored and not redistributed; the
  `.dockerignore` keeps `data/photos/vehiDE/` out of the image. The 11 committed
  claim photos are enough for the photo-versus-invoice demo.
- **The local-model / privacy mode.** Running the pipeline against a locally
  hosted model (`LLM_PROVIDER=ollama`) is the privacy-preserving demonstration;
  it needs a model host and stays on a local machine.
- **A self-hosted Langfuse.** If you want traces, use Langfuse Cloud by setting
  the keys in the Space (A3). We do not stand up an observability server.
```
