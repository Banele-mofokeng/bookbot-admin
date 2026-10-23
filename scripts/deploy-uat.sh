#!/bin/bash
# Deploy UAT stage: admin dashboard + bot backend.
# Set VITE_API_URL to the UAT bot's public URL before deploying.
set -euo pipefail

export VITE_API_URL="https://bookbot-api-uat.<your-project>.workers.dev"

echo "=== Building UAT admin dashboard (VITE_API_URL=$VITE_API_URL) ==="
node scripts/require-api-url.mjs
echo "Building..."
npx vite build

echo "=== Deploying UAT dashboard to Cloudflare Workers ==="
npx wrangler deploy --config wrangler.uat.jsonc

echo "=== Deploying UAT bot backend ==="
npx wrangler deploy --config wrangler.backend.uat.jsonc

echo "=== UAT deploy complete ==="
