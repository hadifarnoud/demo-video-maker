#!/usr/bin/env bash
# Wraps the app's demo-world seed (it lives in the app repo's scripts and must be safe to re-run).
#   DEMO_SEED_CMD='pnpm tsx scripts/seed-demo.ts' lib/seed/run.sh
# playwright login.setup.ts runs DEMO_SEED_CMD itself before each take; use this to seed by hand.
set -euo pipefail
: "${DEMO_SEED_CMD:?set DEMO_SEED_CMD to the app's demo seed command}"
exec bash -c "$DEMO_SEED_CMD"
