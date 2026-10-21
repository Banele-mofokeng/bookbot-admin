/**
 * API Worker — puts the FastAPI bot behind Cloudflare.
 *
 * The bot itself runs in a Cloudflare Container (see wrangler.backend.jsonc).
 * This Worker exists because a container has no inbound port of its own:
 * every request arrives at the Worker first and is forwarded to the
 * container over the platform's internal link.
 *
 * One container, one name. The bot's scheduler — the reminder sweep, the
 * midnight reset, the WhatsApp outbox drain — is a single process, and a
 * second copy would double-send. See DEPLOY.md, "Replicas: keep this at one".
 */
import { Container, getContainer } from "@cloudflare/containers";

// The name of the one container instance this Worker talks to. Everything
// (webhook, admin API, keep-alive ping) goes to the same instance.
const BOT_INSTANCE = "bot";

export class BotContainer extends Container {
	// The bot listens on 9000 — see Dockerfile.bot.
	defaultPort = 9000;

	// How long the container stays up after its last request. The cron
	// trigger below pings it more often than this, so in practice it never
	// sleeps. Set generously rather than tightly: a cold start adds seconds
	// to the first webhook, and Evolution is waiting on that response.
	sleepAfter = "10m";

	// Readiness probe. /ping answers from the process itself; /health would
	// query Postgres and Redis, so a slow database would look like a dead
	// process and fail the port check.
	pingEndpoint = "container/ping";

	constructor(ctx, env) {
		super(ctx, env);

		// Secrets are Worker secrets (`wrangler secret put`), not build-time
		// values, and are handed to the container process at start. A running
		// process keeps its startup environment, so changing a secret means
		// the container has to start again to pick it up.
		this.envVars = withoutUndefined({
			DATABASE_URL: this.env.DATABASE_URL,
			REDIS_URL: this.env.REDIS_URL,
			JWT_SECRET: this.env.JWT_SECRET,
			WEBHOOK_SECRET: this.env.WEBHOOK_SECRET,
			SUPERADMIN_EMAIL: this.env.SUPERADMIN_EMAIL,
			SUPERADMIN_PASSWORD: this.env.SUPERADMIN_PASSWORD,
			ALLOWED_ORIGINS: this.env.ALLOWED_ORIGINS,
			TZ: this.env.TZ,
		});
	}
}

function withoutUndefined(obj) {
	const out = {};
	for (const [key, value] of Object.entries(obj)) {
		if (value !== undefined && value !== null) out[key] = String(value);
	}
	return out;
}

export default {
	// Every request is the bot's request — the admin API and Evolution's
	// webhook alike. There is no routing table here on purpose: the bot
	// already owns its own paths, and a second one in the Worker would only
	// be a place for the two to disagree.
	async fetch(request, env) {
		return getContainer(env.BOT, BOT_INSTANCE).fetch(request);
	},

	// Keep-alive. The bot's scheduled work (reminders, outbox drain) only
	// runs while the process is up, and a container that goes to sleep stops
	// the process. A container also only has to answer to keep its own
	// activity window alive, so this is a request to /ping on the bot.
	//
	// The interval is well under sleepAfter, so the container never actually
	// sleeps and the first real request never pays a cold start.
	async scheduled(event, env, ctx) {
		ctx.waitUntil(
			getContainer(env.BOT, BOT_INSTANCE)
				.fetch(new Request("http://bot.internal/ping"))
				.catch((err) => {
					console.error("Keep-alive ping failed:", err?.message ?? err);
				}),
		);
	},
};
