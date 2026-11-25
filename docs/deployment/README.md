# Deployment

## Recorded Cloud Run topology

The project change log records three Cloud Run services sharing a Cloud Storage
bucket. Resource URLs, bucket names, IAM bindings and mount configuration are
external deployment settings; this repository does not contain an exported
Cloud Run infrastructure specification.

| Service | Build file | Startup | Role |
|---|---|---|---|
| A2A adjudicator | `Dockerfile` | `python src/a2a_server.py` | Hosts CrewAI Flow and launches MCP tools as stdio children |
| Claimant assistant | `Dockerfile.assistant` | `adk web ... agents` | Hosts Google ADK browser chat and its MCP child |
| Dashboard | `Dockerfile.dashboard` | `streamlit run dashboard/app.py ...` | Reads saved outcomes; hosted live assessment is blocked |

All three build from the repository root using Python 3.12 and `uv.lock`.
The Docker commands bind to `0.0.0.0` and use platform `$PORT` when supplied.
The local Streamlit configuration remains bound to `127.0.0.1`.

## Configuration

| Setting | Where | Purpose |
|---|---|---|
| `GROQ_API_KEY` | A2A service | Text and vision inference credentials |
| `GOOGLE_API_KEY` | Assistant | Gemini conversational model credentials |
| `A2A_PUBLIC_URL` | A2A service | Public base URL advertised in its agent card |
| `CLAIMSIGHT_A2A_URL` | Assistant | Same A2A base URL, used to discover the card |
| `CLAIMSIGHT_EMBEDDER=onnx` | Recorded A2A deployment | In-container embeddings without local Ollama |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` | Traced services | Optional telemetry credentials and matching regional host |
| `CLAIMSIGHT_LOG_ARCHIVE` | A2A service | Bucket-mounted destination for a finished monitoring log |
| `CLAIMSIGHT_DASHBOARD_READONLY=1` | Dashboard, optional explicit setting | Disable local live controls; Cloud Run's `K_SERVICE` also forces read-only |

Inject secrets at runtime; do not put them into images or version control.
The Google embedder option additionally needs Google credentials in the A2A
service, but the recorded ONNX setup does not need an embedding API key.

## Why storage must be shared

The assistant's MCP tools and the dashboard read saved decisions/review records
from their own filesystem paths. Separate container disks do not share those
writes. Mount shared record paths for readers and writers as appropriate.

Monitoring deliberately writes the active log locally, then copies the finished
file into `CLAIMSIGHT_LOG_ARCHIVE`. Reader services mount the archived logs as
`data/logs`. A failed archive copy is reported without discarding the decision.
These mounts are deployment configuration, not an MCP HTTP service.

## Historical alternatives

- [Hugging Face/container walkthrough](HUGGING_FACE.md)
- [Render walkthrough](RENDER.md), with the existing root `render.yaml`

These guides document earlier hosting experiments. Their platform-specific
settings are not the current Cloud Run resource configuration. See
[changes since evaluation](../CHANGES_SINCE_EVAL.md) for retrieval and deployment
changes that affect how historical results should be interpreted.
