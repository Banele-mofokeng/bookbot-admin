#!/bin/bash
# Deploy PROD stage: admin dashboard + bot backend.
# Set VITE_API_URL to the PROD bot's public URL before deploying.
set -euo pipefail

export VITE_API_URL="https://bookbot-api-prod.<your-project>.workers.dev"

echo "=== Building PROD admin dashboard (VITE_API_URL=$VITE_API_URL) ==="
node scripts/require-api-url.mjs
echo "Building..."
npx vite build

echo "=== Deploying PROD dashboard to Cloudflare Workers ==="
npx wrangler deploy --config wrangler.prod.jsonc

echo "=== Deploying PROD bot backend ==="
npx wrangler deploy --config wrangler.backend.prod.jsonc

echo "=== PROD deploy complete ==="
