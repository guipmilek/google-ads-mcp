# Guarded Google Ads CRUD

This fork adds generic create, update, and remove operations through
`GoogleAdsService.Mutate`. Reads continue to use the existing GAQL `search`
tool.

The implementation discovers mutable resources and protobuf fields from the
installed Google Ads API version. It supports campaigns, budgets, ad groups,
ads, criteria, assets, conversion actions, and other resources represented by
`MutateOperation`.

## MCP tools

- `mutations_list_mutable_resources`
- `mutations_get_mutation_schema`
- `mutations_create_resource`
- `mutations_update_resource`
- `mutations_remove_resource`
- `mutations_batch_mutate`

## Required production controls

Live execution is disabled by default. Configure the deployment explicitly:

```env
GOOGLE_ADS_MUTATIONS_ENABLED=true
GOOGLE_ADS_ALLOWED_CUSTOMER_IDS=8448275903
GOOGLE_ADS_MAX_OPERATIONS_PER_REQUEST=20
GOOGLE_ADS_CONFIRMATION_SECRET=<random secret with at least 32 bytes>
```

Keep the confirmation secret private and identical across all replicas. It is
used only to sign short-lived execution confirmations.

Recommended limits:

```env
GOOGLE_ADS_MAX_DAILY_BUDGET_MICROS=100000000
GOOGLE_ADS_MAX_TOTAL_BUDGET_MICROS=500000000
GOOGLE_ADS_CONFIRMATION_TTL_SECONDS=900
GOOGLE_ADS_ALLOW_ENABLE=false
GOOGLE_ADS_ALLOW_REMOVE=false
GOOGLE_ADS_ALLOW_SENSITIVE_MUTATIONS=false
GOOGLE_ADS_ALLOW_PARTIAL_FAILURE=false
```

`100000000` micros equals 100 units of the account currency per day. A
custom-period `total_amount_micros` is blocked unless
`GOOGLE_ADS_MAX_TOTAL_BUDGET_MICROS` is configured explicitly.

Enabling resources, deletion, sensitive account/billing resources, and live
partial-failure batches remain disabled unless each capability is explicitly
enabled.

## Two-step execution

All mutation tools default to `validate_only=true`.

1. Submit the intended operation for API validation.
2. Review the normalized payload and `required_confirmation`.
3. After explicit user approval, repeat the exact payload with
   `validate_only=false` and the complete signed confirmation string.

Confirmations resemble:

```text
EXECUTE <hash>.<signed-payload>.<signature>
```

The signed payload binds the confirmation to the customer, normalized
operations, risk verb, `partial_failure` setting, and expiration time. A token
issued by one replica can be verified by another replica that has the same
`GOOGLE_ADS_CONFIRMATION_SECRET`.

Replay protection is best-effort and process-local. The connector does not
claim globally exactly-once execution without a durable shared idempotency
store. Never retry a live mutation automatically after a timeout or transport
error; query the affected resources first, then validate a new operation.

Confirmation verbs indicate operation risk:

- `EXECUTE`: ordinary create or update.
- `ENABLE`: enables a status-bearing resource.
- `REMOVE`: removes a resource.
- `REMOVE_AND_ENABLE`: batch contains both actions.
- `SENSITIVE`: account-access or billing resource.

Changing any operation field changes the hash and invalidates the confirmation.
Confirmations created by older deployments are invalid after this hardening
release and must be regenerated with `validate_only=true`.

## Partial failure

Use `partial_failure=false` for validated production changes. This makes the
request atomic: every operation succeeds or none is applied.

When `partial_failure=true`, invalid operations are returned in
`partial_failure_error`. A validation response containing that error does not
receive an executable confirmation. Live partial-failure execution is blocked
unless `GOOGLE_ADS_ALLOW_PARTIAL_FAILURE=true` is configured explicitly.

The connector rejects partial failure when the batch contains temporary
negative resource IDs or dependent operations.

## Creation policy

Resources whose status enum supports `PAUSED` are always created as `PAUSED`.
An attempt to create them as `ENABLED` is rejected. Resources without a
`PAUSED` enum value retain their supplied or API-default status.

## Discovering fields

Do not guess mutation payload fields. First call:

```text
mutations_get_mutation_schema(resource="Campaign", max_depth=1)
```

The schema reports field behaviors including `OUTPUT_ONLY`, `IMMUTABLE`, and
`REQUIRED`, along with create/update permissions, enum values, repeated fields,
nested messages, supported actions, and the operation field name.

Output-only fields are rejected before the API call. Immutable fields can be
provided during creation when appropriate but cannot be changed by update.
`resource_name` is allowed as the update identifier and must not be included in
`update_mask`.

## Batch planning

The deployment limit defaults to 20 operations per request. A plan containing
127 changes is not one validated transaction: it must be divided into at least
seven independently validated stages. Each stage receives its own confirmation.

Order dependent work conservatively. For example:

1. Create and validate replacement ads while old ads remain unchanged.
2. Verify the created resources through GAQL.
3. Pause old ads in a separate validated stage.
4. Change bidding, targeting, and conversion settings in separate stages.

Google Ads supports up to 10,000 operations per mutate request, but a smaller
connector limit reduces blast radius and makes human review practical.

## Response contract

Responses distinguish:

- `mode`: `VALIDATE_ONLY` or `EXECUTE`.
- `validation_status`: `PASSED`, `FAILED_PARTIAL`, or
  `PRIOR_VALIDATION_VERIFIED`.
- `execution_status`: `NOT_EXECUTED`, `SUCCEEDED`, or `PARTIAL_FAILURE`.
- `partial_failure_error`: returned when Google reports per-operation errors.
- `verification.post_mutation_read_performed`: currently `false`.

A successful mutate response does not prove every requested business outcome.
Use GAQL after execution to verify status, URLs, associations, budgets, and
other affected fields.

## Batch example

A batch can use negative temporary IDs to connect resources created in the same
atomic request:

```json
{
  "customer_id": "8448275903",
  "validate_only": true,
  "partial_failure": false,
  "operations": [
    {
      "action": "create",
      "resource": "CampaignBudget",
      "data": {
        "resource_name": "customers/8448275903/campaignBudgets/-1",
        "name": "New search budget",
        "amount_micros": 20000000,
        "delivery_method": "STANDARD"
      }
    },
    {
      "action": "create",
      "resource": "Campaign",
      "data": {
        "resource_name": "customers/8448275903/campaigns/-2",
        "name": "New search campaign",
        "advertising_channel_type": "SEARCH",
        "campaign_budget": "customers/8448275903/campaignBudgets/-1",
        "manual_cpc": {}
      }
    }
  ]
}
```

The campaign is normalized to `PAUSED` before validation.

## Scope

This CRUD layer covers resources represented by create, update, or remove
operations inside `GoogleAdsService.Mutate`. Specialized non-CRUD actions such
as recommendation application or offline conversion uploads require separate,
dedicated tools.
