// Build guard: VITE_API_URL must be present before a deploy build.
//
// Vite compiles the value into the bundle, so it is fixed at build time and a
// later change to the variable does nothing until a rebuild. The failure mode
// without this check is the worst shape available: the build succeeds, the
// site deploys, the dashboard loads — and every request goes to the SPA's own
// origin, which answers 404. Nothing in the deploy looks wrong; the dashboard
// is simply empty, and the reason is only visible in the browser console.
//
// Failing during the build costs a deploy and nothing else, so the loud
// failure is strictly cheaper than the quiet one.
//
// This lives in Node rather than in shell so the same guard runs on the
// Cloudflare build image, on Railway's Docker build, and on a developer's
// machine, with the same message everywhere.

const url = process.env.VITE_API_URL

if (!url) {
  console.error(
    'VITE_API_URL is not set.\n' +
    '\n' +
    'It is the bot\'s public URL, baked into the bundle at build time.\n' +
    'Set it in the environment that runs the build:\n' +
    '\n' +
    '  Cloudflare  Settings -> Variables and Secrets -> VITE_API_URL\n' +
    '  Cloudflare  (Workers Builds: add it to the build environment)\n' +
    '  local       export VITE_API_URL=https://<your-bot>.up.railway.app\n' +
    '\n' +
    'See DEPLOY.md, "Service 2: Admin Frontend (React)".'
  )
  process.exit(1)
}

if (!/^https?:\/\//.test(url)) {
  console.error(
    `VITE_API_URL is not an absolute URL: "${url}"\n` +
    '\n' +
    'It must be the bot\'s full origin, including the scheme, e.g.\n' +
    '  https://<your-bot>.up.railway.app\n' +
    '\n' +
    'A relative value would send every API call to the dashboard\'s own origin.'
  )
  process.exit(1)
}

console.log(`VITE_API_URL is set -> ${url}`)
