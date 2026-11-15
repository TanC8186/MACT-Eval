# Getting started

Run all commands from the repository root unless a `cd` step is shown. The shell examples use Bash (including Git Bash on Windows).

## Local development

Requirements: Node.js 22 (22.6 or newer), pnpm 11.15.1, Java 21, Maven 3.9+, Python 3.12, Docker Desktop, and FFmpeg for media processing. Use Python 3.12 for the AI environment: its pinned NumPy 1.26.4 does not support Python 3.13.

Copy the configuration template and generate two independent values for `MACT_JWT_SECRET` and `MACT_SERVICE_TOKEN`:

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Place the generated values in `.env`; keep that file local. The supplied Compose configuration is for local development and binds service ports to the loopback interface.

Compose reads the root `.env` automatically. For the native backend and AI commands below, load it in each new terminal before changing directories:

```bash
set -a
. ./.env
set +a
export MACT_AI_SERVICE_TOKEN="$MACT_SERVICE_TOKEN"
```

```bash
pnpm install --frozen-lockfile
docker compose up -d postgres redis minio
```

Run the backend, AI service, and dashboard in separate terminals:

```bash
# Backend
cd backend
mvn spring-boot:run
```

```bash
# AI service, from the repository root
cd ai-service
python -m pip install -r requirements.txt
uvicorn app.main:app --reload --port 8001
```

```bash
# Dashboard, from the repository root
pnpm dev:teacher-web
```

The dashboard normally uses `http://localhost:5173`, the backend uses `http://localhost:8000/api`, and the AI service uses `http://localhost:8001`. Vite may choose another port if 5173 is occupied. Database initialization, account setup, model configuration, and media permissions must be completed before real analysis. Without installed model assets, a missing capability is reported explicitly.

For a synthetic local demonstration, set `MACT_DEMO_ENABLED=true` in the backend terminal before startup. This enables the documented development teacher account (`teacher` / `123456`) and administrator account (`admin` / `admin123`). These are local sample accounts; keep demo mode disabled for real data. An installed classroom-action checkpoint is still required for real action predictions.

For a container-based deployment, build the dashboard first; its Dockerfile copies the built `dist` files:

```bash
pnpm build:web
docker compose --env-file .env -f docker-compose.yml -f docker-compose.cpu.yml up --build -d
```

The CPU override selects CPU/int8 inference; it does not install external dataset or model assets. The full verification scripts retain assumptions from the original Windows development environment, including an E-drive storage policy. They are not a portable one-command deployment guarantee.

## Build and verification

```bash
pnpm build:web
pnpm run test:frontend-auth
pnpm run test:frontend-evidence
mvn -f backend/pom.xml test
python scripts/verify/run_ai_tests.py
python -m pip install -r scripts/verify/requirements-verify.txt
python -m pytest tools/public_data/tests -q -p no:cacheprovider
```

The original workspace also includes `bash scripts/verify/verify_all.sh` for integrated verification. A nonzero result or an explicit incomplete-coverage result must not be reported as a complete pass. Historical test counts describe their recorded revisions and are not a current guarantee for every checkout.
