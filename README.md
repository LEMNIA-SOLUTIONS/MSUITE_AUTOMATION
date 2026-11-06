### MSuite Automation

MSuite Automation for the MSuite platform: event triggers, filters, multi-step actions, and visual workflow execution.

#### Features & Permissions
- **Automation Condition Edit Permissions**: `get_condition_edit_access` validates role write access, document ownership (for specific automations), and permlevel 1 permissions before allowing condition modifications.
- **Instagram Mention Consent & Templates**:
  - `get_ig_step_meta`: Returns step configuration limits, expiry windows, and `templates_enabled` flag.
  - `send_ig_consent_template`: Skips execution with `RESULT_SKIPPED_TEMPLATES_DISABLED` when site-level messenger templates flag is disabled.
  - The mention nodes are always listed in the builder but the automation cannot be saved unless every trigger is a Social Mention trigger.


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
