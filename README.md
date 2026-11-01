# deye-rpi-api

Tiny API for a Deye hybrid inverter. A single poller calls Deye Cloud
`/v1.0/device/latest` every 5 minutes and caches one snapshot in memory;
clients only read that snapshot, so client traffic never reaches Deye Cloud.

```
GET /api/display   (header X-API-Key)
→ {"soc":58,"today_kwh":4.8,"grid":true,"age_s":120,"stale":false}
```

| Field | Deye key |
|---|---|
| `soc` | `SOC` |
| `today_kwh` | `PVDailyPowerGenerationActive` (fallback `DailyActiveProduction`) |
| `grid` | `GridVoltageL1L2 > 150` and `GridFrequency > 45` |
| `age_s` | now − `collectionTime`; `stale` when > `STALE_SECONDS` |

`GET /healthz` is unauthenticated and used by the Docker healthcheck.

## Run on the Pi

```bash
curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker $USER   # once, then re-login
git clone https://github.com/hasithakn/deye-rpi-api.git && cd deye-rpi-api
cp .env.example .env && nano .env

docker compose up -d --build

curl -H "X-API-Key: $API_KEY" localhost:8090/api/display
docker compose logs -f deye-api
```

Update: `git pull && docker compose up -d --build`

## Behaviour

- Token is kept in memory; a new one is obtained on start, ~1 day before expiry,
  or when Deye rejects it (at most one login per poll).
- On errors the last snapshot is kept and polling backs off 5 → 10 → 20 … 60 min;
  HTTP 429 waits 60 min.
- Each poll adds 0–30 s of jitter.

## Cloudflare

Uses an existing `cloudflared` tunnel (e.g. one running with `network_mode: host`):

1. Zero Trust → Networks → Tunnels → your tunnel → Public hostname → add
   `deye.<your-domain>` → service `http://localhost:8090`.
2. Optional: WAF rate-limit rule on `/api/*`, Cloudflare Access for a second auth layer.
