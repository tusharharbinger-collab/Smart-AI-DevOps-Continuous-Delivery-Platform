# RESOLVED (2026-09-15, same day): `docker pull` blocked on this machine for large images

**Update:** access came through / the block cleared on retry the same day —
did not actually wait until 2026-09-16. `envoyproxy/envoy:v1.33.2` pulled
successfully, `cloud-provider-kind` provisioned a real load balancer, and
the `live_url` feature (see `09-universal-delivery-platform.md` sub-phase
9.3) is now fully working end to end, including a much bigger discovery it
led to — see that file's "HTTPRoute routing was broken for every project"
entry. Leaving the rest of this note as-written for the record.

---


## Symptom

`docker pull` fails consistently for any image involving a large download,
across multiple unrelated images (`kindest/node`, `envoyproxy/envoy`,
`hello-world`). Small, already-cached images (`alpine`) pull fine. Always
the same failure shape, on a different layer each retry, never completing —
confirmed across 7+ retries with zero incremental progress.

```
failed to copy: httpReadSeeker: failed open: unexpected status from GET request to
https://production.cloudfront.docker.com/registry-v2/docker/registry/v2/blobs/...: 403 Forbidden
```

Also reproduced against Google's independent Docker Hub mirror
(`mirror.gcr.io`) — same failure signature, ruling out a Docker Hub-specific
outage.

## Diagnosis

Machine is an office laptop; the user does not have the access needed for
`docker pull` of large images on the current network/proxy policy. Earlier
in this session, switching networks resolved an equivalent symptom for
`kind` cluster image pulls — but this instance persisted after a retry
request, meaning the access gap is present again (or was never actually
granted, just briefly bypassed by the earlier network change).

## Required access (submitted to IT)

Outbound HTTPS, no content/size filtering, to:
- `hub.docker.com`, `index.docker.io`, `registry-1.docker.io`, `auth.docker.io`
- `production.cloudfront.docker.com` and `*.cloudfront.net`
- `mirror.gcr.io`, `storage.googleapis.com`

## Status

Blocked on IT granting access — expected to be resolved tomorrow
(2026-09-16). No further retries planned until then.

## What this is blocking

`cloud-provider-kind` (`bin/cloud-provider-kind.exe`, already downloaded
and running as a background process) needs to pull
`docker.io/envoyproxy/envoy:v1.33.2` to create a real, host-reachable load
balancer container for the local Kind cluster's Envoy Gateway. This is the
last step of making a project's `live_url` (see `projects_router.py`'s
`_live_url()` / `GATEWAY_BASE_URL`) actually resolve from a browser — the
application-layer part of that feature (persisting `path_prefix`, computing
`live_url`, returning it from the API, showing it in the UI) is already
built, tested, and confirmed correct independent of this blocker.

## Resume steps once access is granted

1. `docker pull envoyproxy/envoy:v1.33.2` — confirm it completes.
2. Check `cloud-provider-kind.exe`'s log (it's already running in the
   background and retrying on its own) for a successful
   `SyncLoadBalancerSuccessful` event on
   `envoy-gateway-system/envoy-production-local-edge-gateway-...`.
3. `kubectl get svc -n envoy-gateway-system` — confirm the Service now has
   a real `EXTERNAL-IP` instead of `<pending>`.
4. Curl a real project's `live_url` from the host and confirm it actually
   responds.
