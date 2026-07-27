# Changelog

All notable changes to this module are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Work in progress on `dev`, released as `v3.0.0` when merged to `main`. Nothing
here is published yet; no `v3.0.0-*` tag exists. The public interface may still
change.

**Removes two input variables and four outputs**; existing configurations that
use them must migrate. See [Migration from 2.x](#migration-from-2x) below.
Default behavior for consumers who used none of them is unchanged.

### Removed (breaking)

- `kms_key_arn` (top-level variable), replaced by `encryption.kms_key_arn`,
  which is only valid when `encryption.type = "customer-managed"`.
- `storage.encrypted` (object field), replaced by `encryption.type`. Use
  `encryption.type = "aws-managed"` (the default) to keep the encrypted root
  volume, or `"unencrypted"` for the old `storage.encrypted = false`.
- Outputs `ami_transfer_copy_enabled`, `ami_transfer_export_enabled`,
  `cost_report`, and `cost_report_enabled`. Each only echoed back an input the
  caller already had, so they added interface surface without adding
  information. Read your own `var.ami_transfer` / `var.cost_report` instead.
- Output `manual_copy_latest_ami_permission_command`, replaced by
  `manual_copy_latest_ami_example`. The old output returned a command
  containing a literal `<REPLACE_WITH_SCHEDULE_ARN>` placeholder that had to be
  hand-edited before it would run, for a wiring step this module does not
  perform. The replacement is a directly runnable `aws lambda invoke`, matching
  `manual_export_latest_ami_example`.

### Added

- `encryption` variable: a single control for every KMS-aware resource the
  module manages (EBS root volume, SSM parameters, SQS queues, S3 export
  bucket, CloudWatch log groups, Lambda environment variables, and EventBridge
  schedule payloads). Three modes: `unencrypted`, `aws-managed` (default), and
  `customer-managed` (supply an existing `kms_key_arn` or let the module create
  one key and thread it everywhere). The default preserves v2.x behavior for
  consumers who did not set `kms_key_arn` or `storage.encrypted`.
- `kms_key_arn` output: the customer-managed key ARN in use, or `null` when
  `encryption.type` is `aws-managed` or `unencrypted`.
- `ami_transfer.export_bucket_force_destroy` (default `false`) controls whether
  a module-created export bucket can be removed while it still holds objects.
- `scheduler_features.weekly_ami_retention_days` (default `30`). Weekly AMIs
  previously expired on the daily snapshot window (default 7 days) because they
  had no retention setting of their own. They now keep their own, longer
  default, so enabling `backup_cleanup` retains weekly AMIs for 30 days rather
  than 7. Set it to `7` to keep the old behavior.
- `manual_copy_latest_ami_example` output: a runnable `aws lambda invoke`
  command for the AMI copy action, replacing the placeholder-bearing
  `manual_copy_latest_ami_permission_command`.

### Migration from 2.x

```hcl
# 2.x
kms_key_arn = "arn:aws:kms:eu-central-1:123456789012:key/abc..."
storage     = { encrypted = false }

# 3.0
encryption = {
  type        = "customer-managed"
  kms_key_arn = "arn:aws:kms:eu-central-1:123456789012:key/abc..."
}
# For storage.encrypted = false, set encryption.type = "unencrypted" instead.
# If you set neither in 2.x, no change is required, "aws-managed" is the default.
```

### Changed

- Provider and Terraform constraints are now minimums only: `aws >= 6.47.0`,
  `archive >= 2.5.0`, `terraform >= 1.9.0`. The previous `~> 6.47` and
  `< 2.0.0` upper bounds could make this module unusable alongside another
  module that required a newer major version, which is the failure HashiCorp's
  own guidance for shared modules exists to prevent. Pin versions in your root
  module and lock file instead; `examples/basic` shows that pattern.
- The module-created S3 export bucket now uses `force_destroy`
  (`ami_transfer.export_bucket_force_destroy`, default `false`) instead of a
  hardcoded `prevent_destroy = true`. The previous setting made
  `terraform destroy`, and toggling `ami_transfer.enable_export` back off,
  fail permanently on the count-gated bucket, even when empty, with no consumer
  override; `force_destroy = false` only blocks deletion while the bucket
  actually still has objects, as a normal recoverable error. Set
  `export_bucket_force_destroy = true` for a disposable/CI workspace that
  should tear down regardless of bucket contents.
- `instance_schedule_windows` now rejects cross-midnight or all-day windows
  (`start_time >= stop_time`) when `scheduler_features.reconcile = false`, since
  the discrete start/stop schedules alone cannot stop the instance at the
  correct time for those windows. With reconcile enabled (the default), such
  windows are fully supported.

### Fixed

- Both Lambdas no longer reserve 5 concurrent executions. On accounts whose
  unreserved concurrency sits at the 10-execution minimum, that reservation made
  `terraform apply` fail outright. Each function runs on a single schedule for a
  single instance and never runs concurrently, so concurrency is left unmanaged.
- The scheduler's snapshot-reference check now scopes `DescribeImages` to
  `Owners=["self"]`. It previously scanned every public and shared AMI in the
  region looking for a match that can only ever be one of your own.
- `weekly_ami_retention_days` is documented in the README and in the
  `scheduler_features` variable description, along with the fact that no
  retention value has any effect unless `backup_cleanup` is enabled.
- `customer-managed` encryption now covers every KMS-capable resource with no
  silent gaps, including all three CloudWatch log groups and the IAM grants that
  EBS snapshot/AMI creation, `CopyImage`, and EventBridge Scheduler require.
- `encryption.type = "unencrypted"` now genuinely disables SQS SSE
  (`sqs_managed_sse_enabled = false`) instead of silently forcing SSE-SQS.
- Scheduler window parsing was simplified to the documented `start_time` /
  `stop_time` format; unreachable legacy key handling was removed.
- `terraform.tfvars.example` was updated to the v2 object-based inputs
  (`env_vars`, `ami_transfer`, `encryption`, `storage`); it previously
  referenced removed v1 flat variables that would fail on apply.
- The submodule's `workspace_log_group_kms_key_id` now accepts a bare key ID or
  a full ARN, matching `ssm_parameter_kms_key_id`.
- README.md's usage examples were pinned to `version = "~> 1.0"` even though
  the object-based inputs they showed require the breaking 2.0.0 change, and
  the `encryption`/`export_bucket_force_destroy` examples require this release
  and all are pinned to `~> 3.0` now.
- README.md's separate "Encryption reference" table was stale relative to the
  unified `encryption` variable (it claimed CloudWatch Logs has no encryption
  by default, contradicting the "Encryption" section above it) and has been
  removed in favor of that single, current explanation.

## [2.0.0] - 2026-06-08

### Changed

- **Breaking:** inputs moved from flat variables to grouped objects
  (`storage`, `env_vars`, `ami_transfer`, `scheduler_features`). Existing
  configurations must migrate to the new variable names.
- Tag-driven scheduler: the per-instance `scheduler` tag selects the scheduling
  cohort, and the Lambda reads it fresh each invocation so runtime tag edits are
  never reverted by Terraform.

## [1.0.3] - 2026-05-31

- Reliability and dependency maintenance on the 1.x line.

## [1.0.0] - 2026-05-31

- Initial public release to the Terraform Registry as
  `douklar/agent-workspace/aws`.

[Unreleased]: https://github.com/douklar/terraform-aws-agent-workspace/compare/v2.0.0...dev
[2.0.0]: https://github.com/douklar/terraform-aws-agent-workspace/compare/v1.0.3...v2.0.0
[1.0.3]: https://github.com/douklar/terraform-aws-agent-workspace/compare/v1.0.0...v1.0.3
[1.0.0]: https://github.com/douklar/terraform-aws-agent-workspace/releases/tag/v1.0.0
