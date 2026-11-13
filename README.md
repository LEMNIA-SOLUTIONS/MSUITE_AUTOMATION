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
- **Dry run (`engine/preview.py`)**: the result carries a `trigger` check (Field Change current value, Date Based date window, Document Event by the record's docstatus, Schedule by the trigger filters; delete events show "fires when the record is deleted" with no verdict). Steps are still previewed when the trigger would not match. Skipped steps show their reason on the canvas node.
- **Run log**: failed runs expose the error in `get_runs` / `last_run`; WhatsApp steps record the `Channel Message` name and `get_runs` joins its live `status` / `status_error` (sent → delivered → read, or failed + reason).
- **`retry_run(run_name)`** (whitelisted, needs write access): creates a new run from a `Failed` run's stored context, skipping the trigger checks. The old run stays as history; the retry does not count toward the monthly run limit, and can resend steps that already succeeded before the failure. A retry is refused with a clear message when the automation is paused, or when the record is deleted and the run did not save a copy of it.
- **Update Field step**: saves the record with a normal `save()`, so its own rules apply as for a manual edit (mandatory fields, permissions, other apps' rules such as the Lead pipeline). If a rule rejects the value, the step fails, the run shows the rule's message and the record keeps its old value. A rejected Update Field value marks the run Failed (even if a send in the same run succeeded). The save never starts other automations (`AUTOMATION_WRITE_FLAG`). A run started by the scheduler or a date-based trigger runs as Administrator, and some rules skip Administrator (the Lead pipeline does). So when the session user is Administrator, the save runs as the automation's owner and is restored afterwards, also on error. If the owner is missing or disabled, the step fails (no fallback to Administrator). Exception: an Administrator-owned automation keeps the bypass.
- **Opt-out**: a recipient who opted out (Contact flag, Audience Member, or Channel Handle `opted_out`) is skipped for MARKETING templates only; utility templates and free-form text are still sent.
- **Opt-in (AUT-119)**: MARKETING templates also need a recorded WhatsApp opt-in, or the step result is `skipped: no consent` (the dry run shows `no consent`). Opt-in means a Channel Handle with `opted_in` = 1, or a Contact with `unsubscribed_whatsapp` = 0 and `consent_updated_at` set. An opt-out always wins and keeps `skipped: opted out`. Utility, authentication and free-form sends are not affected. Turn the check off with `require_marketing_opt_in` in WhatsApp Settings (on by default; a site that has not run `bench migrate` yet behaves as on, and migrate writes 1). The check is `has_marketing_consent` in `msuite_whatsapp`, called through the WhatsApp channel app.

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
