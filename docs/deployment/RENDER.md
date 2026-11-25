> Historical Render hosting walkthrough. The recorded Cloud Run topology is
> described in [deployment notes](README.md). Run commands from the repository root.

# Deploying ClaimSight on Render — fully in the cloud, nothing on your laptop

This guide deploys the **whole** project to the cloud on **free** tiers, with
**no credit card** anywhere. It is written for the assessment rule that *nothing
may run from the local machine during the demo*: after this, a grader opens one
public link, types a claim, and every part runs in the cloud.

> **Why Render and not Hugging Face?** Hugging Face now charges for Docker
> hosting (PRO only). Render still has a free tier and takes no card. The old
> `docs/deployment/HUGGING_FACE.md` (Hugging Face) is kept for reference; **follow this file instead.**

---

## What gets deployed (two services, both free)

```
  the cloud (Render, free tier)
  -----------------------------
  claimant  -->  Claimant Assistant   (ADK web chat UI)      service #2
                        |  A2A
                        v
                 A2A server            (CrewAI back office)   service #1
                 = intake + assessment + the 6 MCP tools
```

Both are built from this repo:
- **service #1 `claimsight-a2a`** — `Dockerfile` → `src/a2a_server.py` (CrewAI).
- **service #2 `claimsight-assistant`** — `Dockerfile.assistant` → `adk web` (ADK).

This uses **only CrewAI and ADK** as the agentic frameworks, as required.

The `render.yaml` in this repo defines both services, so Render creates them in
one step.

---

## Part 0 — three free accounts / keys you need first

You do these yourself (sign-ups can't be automated). None need a credit card.

### 0a. A Groq API key — powers the CrewAI crews
1. Go to **console.groq.com** → sign in (Google login is fine).
2. **API Keys** → **Create API Key** → copy it. It starts with `gsk_...`.
   (You likely already have this in your local `.env` — the same key works.)

### 0b. A Google AI Studio key — powers the ADK assistant **and** the cloud embeddings
This single free key does two jobs: it drives the Gemini assistant, and it
replaces the laptop-only Ollama embeddings used for policy-clause search.
1. Go to **aistudio.google.com/apikey** (sign in with your Google account).
2. **Create API key** → **Create API key in new project** → copy it. It starts
   with `AIza...`. **No billing / no card is requested.**

### 0c. A Render account — hosts both services
1. Go to **render.com** → **Get Started** → sign up with **GitHub** (easiest).
2. No card is asked for the free tier.

You also need this project in a **GitHub repo** (Render deploys from GitHub).

---

## Part 1 — push this repo to GitHub

If it is already on GitHub, skip to Part 2. Otherwise, create an empty repo on
github.com, then from the project folder:

```bash
git add .
git commit -m "Render deployment: cloud embeddings + ADK assistant service"
git push -u origin main
```

(Make sure your `.env` is **not** committed — it is already in `.gitignore`.
Keys go into Render's dashboard, never into the repo.)

---

## Part 2 — create both services from the blueprint

1. On Render: **New +** (top right) → **Blueprint**.
2. **Connect** your GitHub account if asked, then pick this repository.
3. Render reads `render.yaml` and shows **two services**: `claimsight-a2a` and
   `claimsight-assistant`. Click **Apply** / **Create Services**.

Render now starts building both Docker images. The first build takes a few
minutes (it installs the whole dependency tree). You can watch **Logs → Deploy**
on each service. They will go live, but they cannot assess a claim yet — they
need the keys, which you add next.

---

## Part 3 — paste the secrets

Render marked the secret values as "must be set" (they are **not** in the repo).
Add them in each service's **Environment** tab, then let it redeploy.

**On `claimsight-a2a` → Environment → Add Environment Variable:**

| Key | Value |
|---|---|
| `GROQ_API_KEY` | your `gsk_...` key from 0a |
| `GOOGLE_API_KEY` | your `AIza...` key from 0b |

(`CLAIMSIGHT_EMBEDDER=google` and `A2A_PUBLIC_URL` are already set by the
blueprint — leave them.)

**On `claimsight-assistant` → Environment → Add Environment Variable:**

| Key | Value |
|---|---|
| `GOOGLE_API_KEY` | the same `AIza...` key from 0b |

(`CLAIMSIGHT_A2A_URL` is already set by the blueprint — leave it.)

Click **Save Changes** on each; Render redeploys automatically.

### Check the URLs match (one-minute step, important)
A Render service lives at `https://<service-name>.onrender.com`. If the name
`claimsight-a2a` was already taken by someone else, Render adds a suffix (e.g.
`claimsight-a2a-xk3p.onrender.com`). If so:
1. Copy the **real** URL shown at the top of the `claimsight-a2a` page.
2. On `claimsight-a2a`, set `A2A_PUBLIC_URL` to that exact URL.
3. On `claimsight-assistant`, set `CLAIMSIGHT_A2A_URL` to that same exact URL.
4. Save (both redeploy). If the names were free, they already match and you can
   skip this.

---

## Part 4 — verify it works (do this once, and again right before the demo)

### 4a. The A2A server's public agent card
From any terminal (or just open the URL in a browser):

```bash
curl https://claimsight-a2a.onrender.com/.well-known/agent-card.json
```

You should get JSON whose `supported_interfaces[0].url` is exactly your
`A2A_PUBLIC_URL`. If it still shows `localhost`, `A2A_PUBLIC_URL` was not set when
the container started — set it (Part 3) and **Manual Deploy → Clear build cache
& deploy**.

### 4b. The assistant and a real end-to-end claim
1. Open `https://claimsight-assistant.onrender.com` in a browser.
2. In the ADK UI, pick **claimant_assistant** from the agent list.
3. Type: **`Please assess claim CLM-5000`**.
4. Watch it call the A2A server and return a decision with a payout and reasons.

If that returns a real assessment, **everything is deployed and nothing is
running on your laptop.** That is the requirement met.

---

## Free-tier caveats — read before the live demo

- **Cold start / sleep.** Free Render services sleep after ~15 minutes idle. The
  next request wakes them, which takes ~30–60 seconds. **Wake both ~5 minutes
  before you present**: open the assistant URL and `curl` the agent card so both
  containers are warm. Do not let the first click of the demo be the thing that
  wakes a cold service.
- **First assessment re-embeds the policy.** On a fresh container the policy
  wording is embedded once (via your Google key) before the first claim. It is a
  one-time few-second cost per container; warming it (4b) covers it.
- **Groq free limits.** The free Groq tier caps tokens per minute. The pipeline
  already spaces and retries requests, but a burst of live assessments can hit a
  limit; if so, wait out the minute and retry.
- **Do not demo the sealed test split.** The 40 claims marked `split: "test"` in
  `data/manifest.json` must never be run. Demo the `CLM-5000`-range claims.

---

## What changed in the code to make this work (for your report / viva)

Only two things were local-only before; both are now cloud:

1. **Policy-clause embeddings.** Were `ollama nomic-embed-text` (laptop only).
   Now `src/crews/assessment_crew.py` reads `CLAIMSIGHT_EMBEDDER`: default
   `ollama` for local/tests, `google` (Gemini `gemini-embedding-001`) in the
   cloud, using the same `GOOGLE_API_KEY` as the assistant. The offline test
   suites are unchanged and still pass (they keep their own ollama embedder).
2. **The ADK assistant.** Was `run_assistant.py` on the laptop. Now
   `Dockerfile.assistant` serves it with `adk web` as its own Render service, so
   the claimant-facing agent is hosted too.

Everything else (the A2A server, the 6 MCP tools, the Groq-based crews, the
Groq-based vision) was already cloud-ready.
