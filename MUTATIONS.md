# Guarded Google Ads CRUD

This fork adds generic create, update, and remove operations through
`GoogleAdsService.Mutate`. Reads continue to use the existing GAQL `search`
tool.

The implementation discovers mutable resources and protobuf fields from the
installed Google Ads API version. It is not limited to campaigns: budgets,
ad groups, ads, criteria, assets, conversion actions, and other resources
exposed by `MutateOperation` are supported.

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
```

Optional but recommended budget cap, expressed in micros:

```env
GOOGLE_ADS_MAX_DAILY_BUDGET_MICROS=100000000
```

`100000000` micros equals 100 units of the account currency per day.

Deletion and sensitive account/billing mutations remain disabled unless each
capability is explicitly enabled:

```env
GOOGLE_ADS_ALLOW_REMOVE=false
GOOGLE_ADS_ALLOW_SENSITIVE_MUTATIONS=false
```

Sensitive resources include billing setup, account budgets, account links,
manager/client links, and customer user access.

## Two-step execution

All mutation tools default to `validate_only=true`.

1. Submit the intended operation for API validation.
2. Review the normalized payload and the returned `required_confirmation`.
3. After explicit user approval, repeat the exact payload with
   `validate_only=false` and the returned confirmation string.

Confirmation verbs indicate operation risk:

- `EXECUTE <hash>`: ordinary create or update.
- `ENABLE <hash>`: enables a status-bearing resource.
- `REMOVE <hash>`: removes a resource.
- `REMOVE_AND_ENABLE <hash>`: batch contains both actions.
- `SENSITIVE <hash>`: account-access or billing resource.

Changing any operation field changes the hash and invalidates the previous
confirmation.

## Creation policy

Resources whose status enum supports `PAUSED` are always created as `PAUSED`.
An attempt to create them as `ENABLED` is rejected. Enable them later with a
separate validated update and an `ENABLE <hash>` confirmation.

## Discovering fields

Do not guess mutation payload fields. First call:

```text
mutations_get_mutation_schema(resource="Campaign", max_depth=1)
```

The result includes writable fields, enum values, repeated fields, nested
messages, supported actions, and the operation field name.

## Batch example

A batch can use negative temporary IDs to connect resources created in the
same atomic request:

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
        "amount_micros": "20000000",
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
