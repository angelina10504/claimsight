# ClaimSight A2A server - the CrewAI "back office" behind the Agent2Agent
# protocol. One container runs src/a2a_server.py; CrewAI launches the MCP tool
# server (./claimsight_tools -> src/mcp_server.py) as a stdio child process on
# demand, so this single image is intake, assessment and the six tools together.
#
# Build:  docker build -t claimsight-a2a .
# Run:    docker run --rm -p 9999:9999 --env-file .env \
#             -e A2A_PUBLIC_URL=http://localhost:9999 claimsight-a2a
# Card:   curl http://localhost:9999/.well-known/agent-card.json
#
# No secret is baked in. GROQ_API_KEY and everything else are read from the
# environment at runtime: config.py's load_dotenv quietly no-ops when there is
# no .env in the image, so os.getenv reads whatever the platform injects.

# uv + Python 3.12 (the project pins >=3.12,<3.13). This is the uv-based install
# the project already uses, so the image resolves from the same uv.lock.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# uv in a container: copy packages into the venv (hardlinks do not cross the
# build-cache boundary) and compile to .pyc at install time for a faster first
# request.
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, in their own layer, so editing source does not re-resolve
# the whole tree. --frozen honours uv.lock exactly (crewai 1.15.x, a2a-sdk, and
# the pinned opentelemetry-sdk==1.44.0 that google-adk requires); the project
# itself is NOT installed - the code runs from the working tree below - so no
# build backend is needed.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# The source and the committed fixtures the server reads: the claims, policies
# and invoices, the policy wording the Policy Analyst retrieves over RAG, and
# the 11 claim photos the Damage Analyst sees. The .dockerignore keeps secrets,
# the 2.3 GB VehiDE images and the runtime output folders out of the image.
COPY . .

# Put the venv first on PATH so the bare `python` in ./claimsight_tools - which
# CrewAI execs as the MCP server - is this project's interpreter, and make the
# launcher executable in case a Windows checkout did not preserve the bit.
ENV VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH"
RUN chmod +x claimsight_tools \
    && mkdir -p data/logs data/decisions data/review_queue data/intake data/evidence data/reports

# Bind every interface inside the container - localhost would be unreachable
# from outside - and advertise the public URL in the agent card. HOST, A2A_PORT
# and A2A_PUBLIC_URL are all overridable at run time; nothing here is fixed.
ENV A2A_HOST=0.0.0.0 \
    A2A_PORT=9999
EXPOSE 9999

# A platform that injects $PORT (Cloud Run, Render) wins; otherwise A2A_PORT is
# used. On Hugging Face Spaces the platform routes to the port declared as
# app_port in the Space README, which DEPLOY.md sets to 9999 to match.
CMD ["sh", "-c", "export A2A_PORT=\"${PORT:-$A2A_PORT}\"; exec python src/a2a_server.py"]
