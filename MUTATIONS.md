# Google Ads direct CRUD

The Prefect Horizon server exposes direct Google Ads CRUD using the
`direct-crud-v1` contract.

## Tools

```text
list_mutable_resources
get_mutation_schema
get_mutation_crud_status
create_resource
update_resource
remove_resource
update_ad_group_ad_statuses
batch_mutate
```

Google Ads mutable resource types are discovered from the installed client
library. Use `get_mutation_schema` before constructing a payload.

## One-call behavior

- `dry_run=false` is the default.
- A live call validates the customer scope, resource schema, protobuf payload,
  resource references, update mask, operation count, and budget limits.
- It then sends `GoogleAdsService.Mutate(validate_only=true)`.
- If native validation completes, it sends the live mutation before returning.
- `dry_run=true` returns after native validation and sends no live mutation.
- No signed confirmation, approval token, mutation switch, action gate, or
  prepare/execute pair is required.

`batch_mutate` is atomic by default (`partial_failure=false`). When
`partial_failure=true`, temporary negative resource IDs are rejected because
dependent operations cannot safely use partial-failure semantics.

## Horizon deployment: at most two keys

```env
MCP_CREDENTIALS=<base64-encoded credential envelope>
# Optional restriction:
MCP_CONFIG={"customers":["8448275903"],"max_operations":20,"max_daily_budget_micros":50000000,"max_total_budget_micros":500000000}
```

The credential envelope contains `google_credentials`, `developer_token`, and
optional `login_customer_id`. `google_credentials` may be omitted when
workload identity provides ADC.

`MCP_CONFIG` is optional. Without it, or when `customers` is absent or empty,
every customer accessible to the credential is allowed, the default
20-operation limit applies, and no budget caps are configured. Resource names
and all nested `customers/...` references must still belong to the request
customer.

Legacy Horizon variables such as
`GOOGLE_APPLICATION_CREDENTIALS_JSON_BASE64`,
`GOOGLE_ADS_DEVELOPER_TOKEN`, `GOOGLE_ADS_LOGIN_CUSTOMER_ID`,
`GOOGLE_ADS_ALLOWED_CUSTOMER_IDS`, `GOOGLE_ADS_MAX_*`,
`GOOGLE_ADS_MUTATIONS_ENABLED`,
`GOOGLE_ADS_ALLOW_REMOVE`, `GOOGLE_ADS_ALLOW_ENABLE`,
`GOOGLE_ADS_ALLOW_PARTIAL_FAILURE`, and `GOOGLE_ADS_CONFIRMATION_SECRET` are
not part of the two-key contract and should be removed from Horizon.

## Results

Every response includes:

- contract and operation-hash versions;
- normalized customer ID and operations;
- native validation status;
- whether a live execution was attempted;
- Google Ads mutate response resource names;
- explicit partial-failure details when returned by Google Ads.

An unexpected transport failure during the live call is reported as an
unknown execution state. Do not automatically retry it without reconciling
account state.

## ChatGPT workspace action control

After redeploying, refresh the custom MCP app so ChatGPT imports the current
schemas and annotations. A workspace owner/admin must enable the mutation
actions under Workspace Settings → Apps → Action control. Where allowed,
choose **Never ask**. A `workspace_policy_block` belongs to ChatGPT workspace
policy and cannot be bypassed by the MCP server.

## Verification

Automated tests mock the Google Ads client:

```shell
python -m unittest discover -s tests -p "*_test.py"
python -m compileall ads_mcp horizon_server.py
fastmcp inspect horizon_server.py:mcp
```

Run live create/update/remove only in a dedicated Google Ads test account, not
the production account.
