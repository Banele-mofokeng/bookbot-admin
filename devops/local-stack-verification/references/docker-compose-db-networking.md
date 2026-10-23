# Docker-Compose DB Networking & File Verification

When both the bot and database run in Docker containers, the database URL
must use the container hostname (e.g., `postgres`), not `localhost` or
`host.docker.internal`. `localhost` inside the bot container refers to the
bot itself; `host.docker.internal` connects to the host machine's Postgres,
which may have a different password or no database at all.

The working pattern for this repo:

```yaml
# docker-compose.yml — bot service
DATABASE_URL: postgresql://postgres:password@postgres:5432/queuebot
```

Where the `postgres` service defines:

```yaml
postgres:
  environment:
    POSTGRES_PASSWORD: password
    POSTGRES_DB: queuebot
```

## Verifying `.env` and environment files

Some systems mask secret-bearing files for defense-in-depth: `.env` content
may display as `***` when read with `cat` or `grep`, even though the actual
bytes contain the real value (e.g., `password`). Trusting the masked display
leads to incorrect fixes.

Always verify the actual bytes:

```python
# Python — reads actual bytes, not masked display
with open('.env', 'rb') as f:
    data = f.read()
print('Contains password?', b'password' in data)
print('Contains ***?', b'***' in data)
```

If the byte check shows `password` but `cat .env` shows `***`, the masking is
only visual — the file is correct. If the byte check shows `***`, the file
truly contains a placeholder and must be fixed.
