# Shared rate-limit backend recommendation

Issue: #47

## Recommendation

Use **Redis/Dragonfly as the default shared rate-limit backend** for all production deployments.

Why:
- Consistent throttling decisions across multiple app replicas
- Better protection for auth-sensitive endpoints during scale-out
- Already supported in code (`RATE_LIMIT_REDIS_URL`, `RATE_LIMIT_PREFIX`)
- Safe fallback to in-memory limiter when Redis is unavailable

## Target configuration

Required:
- `RATE_LIMIT_REDIS_URL=redis://<host>:6379/0` (or `rediss://...`)

Optional:
- `RATE_LIMIT_PREFIX=miso-gallery:ratelimit`
- `RATE_LIMIT_ROUTE_LIMITS={"auth":{"max_requests":5,"window":300}}`

## Migration plan

### Phase 0 — pre-check
1. Confirm Redis/Dragonfly endpoint is reachable from all app pods.
2. Validate latency and availability targets for the cache tier.
3. Ensure persistence policy aligns with platform standards (AOF/RDB as needed).

### Phase 1 — canary rollout
1. Enable `RATE_LIMIT_REDIS_URL` for a single pod/canary release.
2. Keep existing route limits unchanged.
3. Watch for:
   - unexpected `429` spikes
   - auth/login failures
   - limiter fallback warnings in app logs

### Phase 2 — full rollout
1. Roll out the same Redis settings to all replicas.
2. Keep a single shared `RATE_LIMIT_PREFIX` per environment.
3. Verify distributed consistency by issuing requests from multiple clients and pods.

### Phase 3 — tuning
1. Apply endpoint overrides using `RATE_LIMIT_ROUTE_LIMITS` for high-risk routes.
2. Re-test login flow and API endpoints under burst traffic.
3. Document final limits in deployment values.

## Validation checklist

- [ ] No increase in authentication failure rate after rollout
- [ ] Rate-limit behavior is consistent across replicas
- [ ] No sustained fallback-to-memory warnings in logs
- [ ] Existing UI behavior remains unchanged for normal request volume

## Rollback

1. Remove `RATE_LIMIT_REDIS_URL` from deployment env.
2. Redeploy; app falls back to in-memory limiter automatically.
3. Investigate Redis connectivity or script errors before retrying migration.
