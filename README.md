### MSuite Automation

MSuite Automation for the MSuite platform: event triggers, filters, multi-step actions, and visual workflow execution.

#### Features & Permissions
- **Automation Condition Edit Permissions**: `get_condition_edit_access` validates role write access, document ownership (for specific automations), and permlevel 1 permissions before allowing condition modifications.
- **Instagram Mention Consent & Templates**:
  - `get_ig_step_meta`: Returns step configuration limits, expiry windows, and `templates_enabled` flag.
  - `send_ig_consent_template`: Skips execution with `RESULT_SKIPPED_TEMPLATES_DISABLED` when site-level messenger templates flag is disabled.
  - The mention nodes are always listed in the builder but the automation cannot be saved unless every trigger is a Social Mention trigger.
- **Plan Limits & Gating (`msuite_plan_gates`)**:
  - `Messaging Automation`: Channel feature checks (`notifications`, `lead_automation`, `order_automation`) combined with `automation_active` (product `AUTOMATION`, `period="total"`, filters `disabled=0`). Allows saving disabled automations and editing already-active automations without consuming extra capacity.
  - `Messaging Automation Run`: Execution log gated by `automation_runs_per_month` (product `AUTOMATION`, `period="month"`), limiting execution runs created per calendar month.
  - Proactive 80% soft warning before hitting limits; transitional bypass when keys are missing from plan payload.


#### Validation & Run Behaviour (UAT fixes)
- **Date Based triggers**: `offset_days` must be a whole number from `OFFSET_DAYS_MIN` (1) to `OFFSET_DAYS_MAX` (10); defined in `constants.py` and enforced in `Messaging Automation._validate_triggers`. Wait steps must be a whole number greater than 0 (no upper cap).
- **Variable mapping**: a chosen WhatsApp template with an unmapped variable (neither a source field nor a fallback value) blocks every save, drafts included ("Automation is not valid"). Bulk campaign steps are skipped.
- **Activation checks (`engine/graph.py`)**: turning an automation on is blocked by empty If/Else branches and by unconnected (orphan) steps; saving a draft only warns. Field Change triggers also fire when a record is created with the matching value (no separate "also on create" option).
- **Send-to field types**: WhatsApp, SMS and Add to List need a Phone field, Send Email needs an Email field; a blank Update Field value is rejected when the target field is mandatory.
- **Send Notification (Notify) step**:
  - **Step type**: `Send Notification` (shown as **Notify** in the builder and `get_automations` `channels` chip).
  - **Fields** (`Automation Step`): `notify_push` (Check, default 1), `notify_in_app` (Check, default 1), `notify_mode` (Select, default `Role`), `notify_role` (Link `Role`), `notify_users` (Code JSON list of user IDs), `notify_field` (Data), `notify_title` (Data, Jinja, truncated to 140 chars), `notify_body` (Small Text, Jinja), `notify_category` (Select), `notify_priority` (`high` / `normal`), `notify_route` (Data, Jinja, sanitized via `msuite_core.services.notifications.sanitize_route`, defaults to `/`).
  - **Template & route safety**: Notification Jinja templates render with `get_jenv(restrict_globals=True)` and a minimal safe helper subset (blocking `frappe.db.sql`, `frappe.get_doc`, `frappe.get_all`, `frappe.sendmail`, `frappe.make_post_request`, and `.__` attribute access; blocked templates fail the step with a readable error). Rendered routes are sanitized so non-relative URLs, `//...`, `javascript:`, backslashes, and control characters fall back to `/`.
  - **Recipient modes** (the five modes):
    - `Role`: enabled users holding `notify_role` (`Has Role`).
    - `Specific Users`: enabled users listed in `notify_users` (JSON list).
    - `Record Owner`: `doc.owner` (if enabled user).
    - `Assignee`: users with `Open` `ToDo` assignments on the record (if enabled users).
    - `Field on Record`: comma/semicolon-separated values from `notify_field` (checked on every reaching DocType). Disabled `User` accounts (`enabled=0`) are dropped, while non-User email identifiers are kept for push-only delivery; in-app bell rows still require enabled `User` accounts. Blank values and `Guest` are dropped in all modes.
  - **Categories**: `General` (default), `Academic`, `Billing`, `Marketing`, `System`.
  - **Idempotency & error handling**:
    - Push idempotency keys include the run name (`<dedupe_base>:<run>:<recipient>`), so separate saves/runs of the same record do not collide inside `msuite_notification`'s claim window.
    - Step errors (`step_error`, i.e. `outcome_status == "error"`, such as empty rendered title, invalid/blocked Jinja, or no channel selected) mark the `Messaging Automation Run` as `Failed` with the error text recorded on the log entry and surfaced in `get_runs`.
    - `no recipient`: no recipient resolved after filtering.
    - `push unavailable`: push-only step when `msuite_notification` is not installed or `Push Notification Config` is disabled/suppressed.
    - `duplicate`: deduplication lock already held for this run/step/record.
    - Needs `msuite_notification` installed for push; without it, in-app bell notifications still send and push-only steps skip with `push unavailable` (never fails the run). Per-recipient push enqueue errors are logged to Error Log without failing the run or blocking remaining recipients.
  - **Admin how-to**: Configure provider keys in **Push Notification Config** → create or edit a Messaging Automation and add a **Notify** step → test recipients, rendered text, and route with **Test** (dry run) before activating.
- **Dry run (`engine/preview.py`)**: the result carries a `trigger` check (Field Change current value, Date Based date window, Document Event by the record's docstatus, Schedule by the trigger filters; delete events show "fires when the record is deleted" with no verdict). Steps are still previewed when the trigger would not match. Skipped steps show their reason on the canvas node. Also previews `Send Notification` steps with `channels`, `to`, `title`, `body`, and `route` (or `skipped: no recipient`) without sending or writing logs.
- **Run log**: failed runs expose the error in `get_runs` / `last_run`; WhatsApp steps record the `Channel Message` name and `get_runs` joins its live `status` / `status_error` / `failure_reason` (sent → delivered → read, or failed + reason).
- **Failed sends are recorded and shown (AUT-123)**: a WhatsApp send the channel rejects keeps its failed `Channel Message` name in the run log (`SendFailed` / `SendRetry` carry `channel_message`), so the reason shows in the run. A run is stored as `Failed` when any send errored (retry skips sends already made). If WhatsApp reports a message as failed after it was sent, `get_runs` shows that run as `Failed` with the linked `Channel Message` failure reason and `delivery_failed: true` (display only; the stored status is unchanged and Retry is hidden).
- **All-skipped runs and Deal primary contact mobile (AUT-124, D7, D8, Follow-ups)**:
  - **Structured step outcome key (`outcome_status`) and delivery collision fix**: step execution outcomes write to `outcome_status` rather than `status`, resolving the collision with Channel Message delivery status. Stored log backward compatibility is maintained across all versions. A message's delivery status ('delivered', 'read') does not alter step outcome logic.
  - **Partial skip with successful actions (MED-2)**: `executor._persist` marks a run `Skipped` only when all send steps were recipient-skipped AND no other action step (`Update Field` or `Add to List`) succeeded; a run that performed successful field updates or list additions stays `Completed`.
  - **Run errors and skip reasons (CRIT-1, MED-3)**: In `get_runs`, run `error` is computed after merging Channel Message records, ensuring asynchronous Meta delivery failure reasons reach `r['error']` for `Failed` runs. In `get_automations`, `last_run.error` for `Skipped` runs carries the skip reason (consistent with `get_runs`).
  - **Structured skip outcome (D7)**: step execution returns a structured `StepOutcome` (subclassing `str`) carrying `.outcome_status` and `.reason` while keeping the display string intact. `executor._persist` determines `Skipped` from structured statuses and reasons rather than regex matching, while legacy stored logs with `'skipped: ...'` strings continue to map to `Skipped`. Only recipient-reason skips (`no recipient`, `opted out`, `no consent`, `no consent record` in `RECIPIENT_SKIP_REASONS`) mark an all-send run as `Skipped`. Idempotency skips (`duplicate`, `already sent`) keep run status `Completed`.
  - **Virtual-field resolver table (D8)**: module-level table `VIRTUAL_FIELD_RESOLVERS` keyed by `(doctype, field)` registers virtual fields for `(CRM Deal, primary_contact_mobile)` and `(Delivery Note, sales_order.*)`. `field_value`, `channels.resolve_phone`, and `api.get_doctype_fields` read from this table, eliminating hardcoded DocType branches from `resolve_phone`. For `CRM Deal` with multiple `is_primary` child rows, the row with the lowest `idx` is selected; if no primary contact row exists, `None` is returned without fallbacks.
- **`retry_run(run_name)`** (whitelisted, needs write access): creates a new run from a `Failed` run's stored context, skipping the trigger checks. The old run stays as history; the retry does not count toward the monthly run limit, and can resend steps that already succeeded before the failure. A retry is refused with a clear message when the automation is paused, or when the record is deleted and the run did not save a copy of it.
- **Update Field step**: saves the record with a normal `save()`, so its own rules apply as for a manual edit (mandatory fields, permissions, other apps' rules such as the Lead pipeline). If a rule rejects the value, the step fails, the run shows the rule's message and the record keeps its old value. A rejected Update Field value marks the run Failed (even if a send in the same run succeeded). The save never starts other automations (`AUTOMATION_WRITE_FLAG`). A run started by the scheduler or a date-based trigger runs as Administrator, and some rules skip Administrator (the Lead pipeline does). So when the session user is Administrator, the save runs as the automation's owner and is restored afterwards, also on error. If the owner is empty, missing or disabled, the step fails, with no fallback to Administrator. The save also needs the owner's write permission on the record; an owner without it fails the step with a permission error naming the owner. Other permission errors keep their own message. Exception: an Administrator-owned automation keeps the bypass.
- **Pause and duplicate**: pausing an automation and duplicating one skip the unmapped-variable check (`SKIP_MAPPING_CHECK_FLAG`), so a template that gained variables in Meta cannot block them. Normal saves and activation still run it.
- **Opt-out**: a recipient who opted out (Contact flag, Audience Member, or Channel Handle `opted_out`) is skipped for MARKETING templates only; utility templates and free-form text are still sent.
- **Opt-in (AUT-119)**: MARKETING templates also need a recorded WhatsApp opt-in, or the step result is `skipped: no consent` (the dry run shows `no consent`). Opt-in means a Channel Handle with `opted_in` = 1 (set when the customer opts in through WhatsApp). Contact `consent_updated_at` does not count: SMS and Email events set it too. An opt-out always wins and keeps `skipped: opted out`. Utility, authentication and free-form sends are not affected. Turn the check off with `require_marketing_opt_in` in WhatsApp Settings (on by default; a site that has not run `bench migrate` yet behaves as on, and migrate and a fresh install write 1). Deploy `msuite_automation` and `msuite_whatsapp` together: Automation calls `has_marketing_consent`, which only the new WhatsApp version has. The check is `has_marketing_consent` in `msuite_whatsapp`, called through the WhatsApp channel app.

### External API (token auth)

An external system (for example Flavorz) manages automations with the endpoints in `msuite_automation.msuite_automation.api.automations`. Send `Authorization: token <api_key>:<api_secret>`. Automations are `Messaging Automation` documents.

These endpoints return the value directly. They do not use the `{ok, data, error}` envelope of the social post API. An error returns a real HTTP status (403 no permission, 404 not found, 417 validation).

**Roles.** `WhatsApp Manager`, `MSuite Admin` and `System Manager` can read, write and delete `Messaging Automation`. `Social Manager` cannot manage automations. A plan limit on active automations and on runs per month can also refuse a call.

| Endpoint | Method | Arguments | Returns |
|---|---|---|---|
| `get_automations` | GET | `search`, `trigger_type`, `enabled` (`1` or `0`), `reference_doctype`, `start`, `page_length` | `{"items": [...], "total_count": n}`. Each item: `name`, `title`, `disabled`, `trigger_type`, `reference_doctype`, `trigger_types`, `doctypes`, `when_summary`, `channels`, `last_run`, `modified`. |
| `get_automation` | GET | `name` | The document with all child tables. |
| `save_automation` | POST | `doc` (JSON). Include `name` to update. | `{"name": ...}` |
| `toggle_automation` | POST | `name`, `enabled` (`1` or `0`) | `{"name": ..., "disabled": 0 or 1}` |
| `duplicate_automation` | POST | `name` | `{"name": <copy>}`. The copy is off and titled "<title> (copy)". |
| `delete_automation` | POST | `name` | `{"name": ...}` |
| `bulk_delete_automations` | POST | `names` (list) | `{"deleted": [...], "failed": [{"name", "error"}]}` |
| `get_runs` | GET | `name`, `limit` (default `RUN_LIST_DEFAULT_LIMIT` = 10, at most `RUN_LIST_MAX_LIMIT` = 50) | Runs, newest first: `name`, `status`, `trigger_id`, `reference_doctype`, `reference_name`, `creation`, `resume_at`, `sent_count`, `log`, `error`, `delivery_failed`. |
| `retry_run` | POST | `run_name` | `{"name": <new run>}`. Only a `Failed` run, and the automation must be on. |
| `test_automation` | GET | `doc`, `docname`, `trigger_id` | A dry run on one record. Sends nothing and writes nothing. |
| `get_stats` | GET | none | `{"total", "active", "dispatched", "by_trigger", "doctypes"}` |
| `get_reference_doctypes` | GET | none | DocType names the user can read. |
| `get_doctype_fields` | GET | `doctype` | Field metadata for pickers: `fieldname`, `label`, `fieldtype`, `options`, `reqd`. |
| `get_ig_step_meta` | GET | none | Instagram step limits and `templates_enabled`. |
| `get_condition_edit_access` | GET | `name` (optional) | `true` when the user may edit trigger conditions. |

**`doc` for `save_automation`.**

- Fields: `title` (required), `description`, `disabled`, `send_frequency` (`Every time`, `Once per day`, `Once per document`), `filter_logic`, `phone_field`, `email_field`, `whatsapp_account`, `sms_account`, `skip_return_documents`.
- Child tables: `triggers`, `filters`, `steps`, `edges`, `variable_mappings`, `recipients`, `notes`.
- Trigger types: `Document Event`, `Field Change`, `Date Based`, `Schedule`.
- Step types: `Send WhatsApp`, `Send Email`, `Send SMS`, `Wait`, `Wait for Event`, `If/Else`, `Switch`, `Update Field`, `Add to Recipient List`, `Send IG Public Reply`, `Send IG Consent Template`, `Send Notification`.
- A user without permission level 1 write access cannot change a trigger `condition`. MSuite keeps the saved condition and ignores the one sent.

**Mention automation.** Use a `Document Event` trigger on `Social Mention` with `doc_event` `After Insert`, and a `Send IG Public Reply` step with `ig_reply_text`. The Instagram steps need every trigger to be a `Social Mention` trigger. Turning the automation on makes MSuite reply publicly to every new mention on accounts with a live token.

```json
{
  "title": "Mention reply",
  "disabled": 1,
  "triggers": [{"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": "Social Mention", "doc_event": "After Insert"}],
  "steps": [{"step_id": "s1", "step_type": "Send IG Public Reply", "ig_reply_text": "Thanks for the mention!"}],
  "edges": [{"source_id": "t1", "source_handle": "", "target_id": "s1"}]
}
```

The full field reference, with every trigger and step field, is in `docs/flavorz-msuite-api-for-sagar.md` in the workspace root.

### Broadcast Integration

`msuite_automation` integrates with unified Broadcasts and Recipient Lists for workflow step execution.

#### Configuration Flag

- Key: `msuite_broadcast_enabled` in `site_config.json`. The default is off.
- The flag affects only the WhatsApp lead-status automation in `msuite_automation/engine/channels.py`.
  - Flag off: the automation uses the legacy `WhatsApp Recipient List` and `Bulk WhatsApp Message` code. Nothing changes.
  - Flag on: the automation uses `Recipient List`, `Recipient Contact` and `Broadcast`.

#### Flagged Lead-Status Automation Path

When the flag is on, the automation for a lead depends on the schedule type of the bulk step:

- **Immediate**: The automation skips the lead if the number is invalid or opted out of WhatsApp. Otherwise it creates a `Broadcast` with `recipient_source` set to `Individual` and one WhatsApp `Broadcast Channel` row (`is_lead_automation_broadcast` set). It launches the `Broadcast` with `msuite_campaigns.broadcast.hub.launch`, which sends the message to that one lead.
- **Scheduled Time** and **Delay Buffer**: The automation adds the number (E.164 format) to the `Recipient List` of the rule. It does not add the same number twice. It finds the open `Broadcast` of the rule (status `Draft` or `Scheduled`) or creates one, and updates the recipient count. The `Broadcast` is sent later by the central broadcast scheduler.
- **Target broadcast**: If the rule names a `target_broadcast` that is no longer open, the automation skips the lead and returns `{"status": "skipped", "reason": "target_broadcast_not_open"}`.

There is no Email or SMS path in this automation.

#### Automation Step Link Repointing

`Automation Step` records link `target_bulk_message` to `Broadcast` and `recipient_list` to `Recipient List`.

The patch `msuite_automation.patches.repoint_automation_step_links`:
- Repoints `target_bulk_message` legacy links (e.g. `Bulk WhatsApp Message`) to unified `Broadcast` records via `msuite_campaigns.broadcast.legacy.resolve`.
- Repoints `recipient_list` legacy links (e.g. `WhatsApp Recipient List`) to unified `Recipient List` records via `msuite_campaigns.broadcast.legacy.resolve`.
- Unresolved legacy names are safely recorded in `unresolved` without blanking existing fields.
- **Manual Cutover Execution**: Not auto-registered in `patches.txt` so `bench migrate` will not repoint live data before frontend cutover while the legacy flag-off automation path still needs old names. Run manually in the cutover runbook with dry run as default (`dry_run=True`):
  ```bash
  bench --site <site> execute msuite_automation.patches.repoint_automation_step_links.execute --kwargs "{'dry_run': False}"
  ```

#### Tests

```bash
bench --site lemnia.localhost run-tests --app msuite_automation --module msuite_automation.msuite_automation.tests.test_broadcast_path
```

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch dev
bench install-app msuite_automation
```

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/msuite_automation
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### License

mit
