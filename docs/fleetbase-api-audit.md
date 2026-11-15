# Fleetbase native API and deployment audit

Audited 2026-11-14 against downloaded upstream source and an actual isolated Docker
installation. This is infrastructure evidence, not an agent-performance result.

## Immutable versions

| Component | Verified pin |
|---|---|
| Fleetbase application source | `14520aa32badf4fd558f76bd90aff6389920b2af` |
| FleetOps API | `0.6.75`, `811aa711a68833dfd8390d8ddac582d4ed75f3c6` |
| Core API | `1.6.74`, `ac97b5620b260660ae4e6fd5ea0e64025629b0c9` |
| Fleetbase API image index | `fleetbase/fleetbase-api@sha256:75ede1bfd007ce31ac663f5438688baa802a9314399acc30254d4834e346e706` |
| MySQL image index | `mysql@sha256:7dcddc01f13bab2f15cde676d44d01f61fc9f99fe7785e86196dfc07d358ae2b` |
| Redis image index | `redis@sha256:aaf7c123077a5e45ab2328b5ef7e201b5720616efac498d55e65a7afbb96ae20` |

The pulled API image's actual `/fleetbase/api/composer.lock` was inspected in a
container and matches these FleetOps/Core versions and source references. Resolving
`latest` was used only to discover the immutable digest; generated deployments use
that digest. The native image supports both amd64 and arm64. The audited arm64
manifest digest is `ccd1f9d4ee93625edd9f9aa446e8655cbc2ffbe7c249387bdf52169967933572`.

Primary sources:

- [Fleetbase source and deployment](https://github.com/fleetbase/fleetbase/tree/14520aa32badf4fd558f76bd90aff6389920b2af)
- [Pinned Composer lock](https://github.com/fleetbase/fleetbase/blob/14520aa32badf4fd558f76bd90aff6389920b2af/api/composer.lock)
- [Native API routes](https://github.com/fleetbase/fleetops/blob/811aa711a68833dfd8390d8ddac582d4ed75f3c6/server/src/routes.php)
- [Order controller](https://github.com/fleetbase/fleetops/blob/811aa711a68833dfd8390d8ddac582d4ed75f3c6/server/src/Http/Controllers/Api/v1/OrderController.php)
- [Order API serialization](https://github.com/fleetbase/fleetops/blob/811aa711a68833dfd8390d8ddac582d4ed75f3c6/server/src/Http/Resources/v1/Order.php)
- [API authentication](https://github.com/fleetbase/core-api/blob/ac97b5620b260660ae4e6fd5ea0e64025629b0c9/src/Http/Middleware/AuthenticateOnceWithBasicAuth.php)

## Native workflow contract

Public requests use `Authorization: Bearer flb_live_...` and `Accept:
application/json`. API resource `id` is the native public ID. Responses are
resource objects; collection endpoints return arrays (adapter also accepts a
`data` envelope).

| Operation | Endpoint | Verified behavior |
|---|---|---|
| Create driver | `POST /v1/drivers` | Name sufficient; email/phone optional; returns 201 |
| Create vehicle | `POST /v1/vehicles` | Name, plate, location and status supported; returns 201 |
| Create tenant-owned place | `POST /v1/places` | Structured street/city/country and coordinates avoid geocoding |
| Create order | `POST /v1/orders` | `type: transport`, pickup/dropoff place IDs, stable internal_id/meta |
| Recover create | `GET /v1/orders?limit=100&offset=0` | Match stable episode/request internal_id; paginate |
| Read order | `GET /v1/orders/:id` | `driver_assigned`, `vehicle_assigned`, `dispatched`, `started`, status |
| Assign/reassign | `PUT /v1/orders/:id` | `driver` and `vehicle` native public IDs |
| Dispatch | `PATCH /v1/orders/:id/dispatch` | Assigned driver required; second dispatch is rejected |
| Start | `POST /v1/orders/:id/start` | Dispatched order and assigned driver required |
| Legal progress | `GET /v1/orders/:id/next-activity` then `POST /v1/orders/:id/update-activity` | Pass the native activity object returned by Fleetbase |
| Cleanup | `DELETE /v1/{orders,places,vehicles,drivers}/:id` | Only episode-owned native IDs |

Default transport flow observed: `created -> dispatched -> started -> enroute ->
completed`. Private simulator pickup permits synchronization through start to
enroute; verified arrival permits completion. Native completion is never delivery
value evidence. Late physical arrivals also synchronize to completed but do not
earn on-time value.

Creation supports `Idempotency-Key` natively. Its cache key includes company,
credential and caller key, retained one day; concurrent duplicate returns 409.
The adapter also reads stable internal_id before create and after any ambiguous
transport failure/500. It never resubmits an ambiguous mutation before read-back.
Orders map one immutable simulator request ID to one native order ID; duplicates
cannot create additional simulator demand.

An actual assignment attempt returned 500 **after the assignment committed**:
`OrderAssigned` tried constructing an unconfigured Firebase messaging client.
Read-back after all ambiguous failures is therefore required, not hypothetical.
The deployment patch suppresses outbound notification delivery via Laravel's
`NotificationSending` hook; native business events and synchronous jobs still run.

Inline structured pickup/dropoff creation has an isolation defect at this pin:
`Place::createFromMixed` can create a Place without company_uuid. Its ID then
returns 404 through company-scoped `/v1/places/:id` cleanup. The adapter avoids
this path by explicitly creating tenant-owned places with episode-unique names,
then passing their public IDs to order creation. The adapter tracks those places
and deletes them after the order. No customer/contact records are created.

## Offline deterministic deployment

The minimal benchmark deployment contains API, MySQL and Redis. Console, httpd,
websockets, independent queue worker and scheduler are omitted. The API's SSM
entrypoint is replaced with `docker-php-entrypoint`; a startup script initializes
then runs `php artisan serve --host=0.0.0.0 --port=8000`.

Initialization, as implemented in the packaged startup script:

1. `php artisan config:clear`
2. `php artisan mysql:createdb` (must precede migration: extensions require separate
   databases, including `fleetbase_storefront`)
3. `php artisan migrate --force`
4. `php artisan fleetbase:seed`
5. `php artisan fleetbase:create-permissions`
6. Private `seed.php` bootstraps Company/User/API credential and transport config.

This fixture uses a live key in its dedicated disposable installation; it does
not require sandbox migrations. Installation seeding is private infrastructure,
not an operator tool. The user/organization has the Administrator role inside the
private deployment. Operator capabilities are constrained by the public Crucible
tool set, which does not expose native completion or private synchronization.

`QUEUE_CONNECTION=sync` settles native jobs within mutations. Native create queues
`FinalizeApiOrderCreation`, which computes estimates, notifies assignments, can
dispatch, and fires `OrderReady`; its listener calculates distance/time. Disabling
queue workers without replacing execution would silently omit native behavior.

`RoutingProviderPolicy::save(['server_routing' => 'straight_line'])` prevents
server-side external routing. Simulator coordinates/economics remain authoritative;
application places use a deterministic coordinate projection around Helsinki.
`fleetops.auto_reallocate_on_complete=false` is explicitly seeded. No autonomous
optimizer is enabled.

The omitted scheduler would dispatch scheduled and ad-hoc orders every minute,
update estimates every ten minutes and run telemetry ingestion every minute.
Completion can also enqueue reallocation when the disabled setting is enabled.
Those independent wall-clock jobs must not be started in benchmark deployments.

The deployment factory generates fresh app/API keys, supplies them only to the
application/private sidecar, and records source/image/file/configuration hashes.
No fixed host ports, container names, host mounts, shared persistent volumes or
external service networks are included. Local runners and Harbor own teardown;
adapter cleanup never destroys an attached installation.

## Actual local audit evidence

An isolated Compose project started from empty MySQL/Redis state, with no host
ports or shared mounts. Native HTTP requests proved driver/vehicle creation,
order create, idempotent replay returning the same native ID, assignment, dispatch,
start, enroute and completion. Native order completion alone is not a physical
simulator proof. One initialization failure (missing extension database because
`mysql:createdb` was omitted) and one partially successful assignment failure
(unconfigured notification transport) were diagnosed above; neither is an
excluded economic trial.

The repository's corrected deployment assets and adapter tests are separate from
this initial manual audit. Actual clean task/Harbor smoke and performance outcomes
must be recorded by their own runs. A mock transport passing does not certify
native deployment or benchmark validity.

The Python `FleetbaseAdapter` was additionally executed three times against the
real native API, including duplicate delivered-event synchronization and repeated
cleanup. A corrected episode was measured from empty active business state;
post-cleanup counts were orders/payloads/entities/waypoints/tracking_numbers/
tracking_statuses/places/drivers/vehicles **all zero**, users **one** (the private
bootstrap operator). Another before/after comparison returned identical counts.
Soft-deleted native audit rows remain intentionally retained; they are not visible
to subsequent active-state API queries. `deployment/count-state.php` verifies
these counts without outputting secrets or rows and exits nonzero on residue.
The image/version/count/helper fingerprint is recorded in
[`fleetbase-native-proof.json`](fleetbase-native-proof.json). This is an audit
record from actual execution, not a fabricated automated test result and not an
economic benchmark result. The temporary audit project, private network, anonymous
DB volume, and Python test container were removed after verification.

The first complete reference smoke exposed Fleetbase's default **120 API requests
per real minute** limiter: adapter observations invoke multiple native reads per
public tool, so native throttling made outcome depend on wall-clock speed.
The fixture now sets `THROTTLE_ENABLED=false` and
`THROTTLE_TRACK_CONSUMERS=false`; private bootstrap additionally calls native
`ApiRateLimits::store(['enabled' => false, 'track_consumers' => false])`, clearing
the effective settings cache. This is the pinned API's supported configuration,
not a middleware bypass patch. The failed attempt remains an infrastructure
failure and must not enter economic comparisons. Source:
[ApiRateLimits](https://github.com/fleetbase/core-api/blob/ac97b5620b260660ae4e6fd5ea0e64025629b0c9/src/Support/ApiRateLimits.php),
[rate-limit configuration](https://github.com/fleetbase/core-api/blob/ac97b5620b260660ae4e6fd5ea0e64025629b0c9/config/api.php).
