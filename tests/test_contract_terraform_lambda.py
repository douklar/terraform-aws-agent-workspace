"""Checks that main.tf and the Lambda code agree.

Env var names, action names and the reserved mode list are written out in both
Terraform and Python. Change one side only and it breaks at runtime.
"""

import ast
import re
from pathlib import Path

import pytest

import ami_transfer
import scheduler

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_TF = (REPO_ROOT / "main.tf").read_text()
VARIABLES_TF = (REPO_ROOT / "variables.tf").read_text()

# Lambda automatically provides these; main.tf neither sets nor may set them
# (AWS reserves the AWS_* namespace in a function's environment block).
RUNTIME_PROVIDED_ENV = {"AWS_REGION"}


def hcl_string_list(source, marker):
    """Return the string literals of the list that follows ``marker``.

    ``marker`` must identify the start of a bracketed list; everything up to the
    matching close bracket is scanned for quoted strings.
    """
    start = source.find(marker)
    assert start != -1, f"could not locate {marker!r} - the contract test needs updating"
    open_bracket = source.index("[", start)
    depth = 0
    for index in range(open_bracket, len(source)):
        if source[index] == "[":
            depth += 1
        elif source[index] == "]":
            depth -= 1
            if depth == 0:
                body = source[open_bracket : index + 1]
                break
    else:  # pragma: no cover - only reachable on malformed HCL
        raise AssertionError(f"unbalanced brackets after {marker!r}")

    values = re.findall(r'"([^"]+)"', body)
    assert values, f"no string values found after {marker!r}"
    return values


def env_vars_read_by(module):
    """Every environment variable name the module reads, via AST not regex.

    Covers direct reads (``os.getenv``/``os.environ[...]``) and the module's own
    typed wrappers, which take the variable name as their first argument.
    """
    tree = ast.parse(Path(module.__file__).read_text())
    names = set()
    wrappers = {"getenv", "env_bool", "positive_int_env"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in wrappers and node.args and isinstance(node.args[0], ast.Constant):
                names.add(node.args[0].value)
        elif isinstance(node, ast.Subscript):
            value = node.value
            if (
                isinstance(value, ast.Attribute)
                and value.attr == "environ"
                and isinstance(node.slice, ast.Constant)
            ):
                names.add(node.slice.value)

    assert names, f"no environment reads found in {module.__name__} - extraction is broken"
    return names


def terraform_env_block(function_resource):
    """The environment variable keys main.tf sets on one Lambda resource."""
    marker = f'resource "aws_lambda_function" "{function_resource}"'
    start = MAIN_TF.find(marker)
    assert start != -1, f"no aws_lambda_function.{function_resource} in main.tf"
    env_start = MAIN_TF.find("environment {", start)
    assert env_start != -1, f"no environment block on aws_lambda_function.{function_resource}"
    env_end = MAIN_TF.find("\n  }", env_start)
    block = MAIN_TF[env_start:env_end]

    keys = set(re.findall(r"^\s{6}([A-Z][A-Z0-9_]*)\s*=", block, re.MULTILINE))
    assert keys, f"no environment variables parsed for {function_resource}"
    return keys


class TestReservedModeAliases:
    """The three copies of the reserved mode list must agree.

    scheduler.py holds the behaviour, main.tf's locals back the scheduler_mode
    precondition, and variables.tf rejects cohorts named after an alias. A name
    in one list but not another is a quiet bug: the Lambda turns the alias into
    on-demand or disabled, so that cohort never matches anything.
    """

    def test_main_tf_on_demand_locals_match_the_lambda(self):
        declared = hcl_string_list(MAIN_TF, "on_demand_scheduler_modes")
        assert set(declared) == scheduler.ON_DEMAND_ALIASES

    def test_main_tf_disabled_locals_match_the_lambda(self):
        declared = hcl_string_list(MAIN_TF, "disabled_scheduler_modes")
        assert set(declared) == scheduler.DISABLED_ALIASES

    def test_window_mode_validation_rejects_every_lambda_alias(self):
        blocked = set(hcl_string_list(VARIABLES_TF, "!contains("))
        every_alias = scheduler.ON_DEMAND_ALIASES | scheduler.DISABLED_ALIASES

        missing = every_alias - blocked
        assert not missing, (
            f"instance_schedule_windows would accept {sorted(missing)} as cohort names, "
            "but the Lambda collapses them to a reserved mode - such a cohort can never match"
        )

    def test_validation_does_not_block_names_the_lambda_allows(self):
        # Blocking too much is less harmful, but still rejects a name that works.
        blocked = set(hcl_string_list(VARIABLES_TF, "!contains("))
        every_alias = scheduler.ON_DEMAND_ALIASES | scheduler.DISABLED_ALIASES

        assert not blocked - every_alias

    def test_the_canonical_modes_are_themselves_reserved(self):
        # Aliases resolve to these, so they have to be reserved as well.
        assert scheduler.SCHEDULER_MODE_ON_DEMAND in scheduler.ON_DEMAND_ALIASES
        assert "disabled" in scheduler.DISABLED_ALIASES

    def test_default_mode_is_not_reserved(self):
        # The default cohort has to be schedulable, or nothing follows a schedule.
        default = scheduler.SCHEDULER_MODE_DEFAULT
        assert default not in scheduler.ON_DEMAND_ALIASES | scheduler.DISABLED_ALIASES

    def test_alias_sets_are_disjoint(self):
        # A name in both sets would resolve by luck, and the two behaviours are
        # opposites.
        assert not scheduler.ON_DEMAND_ALIASES & scheduler.DISABLED_ALIASES


class TestSchedulerEnvironmentContract:
    def test_every_variable_the_handler_reads_is_set(self):
        provided = terraform_env_block("scheduler")
        required = env_vars_read_by(scheduler) - RUNTIME_PROVIDED_ENV

        missing = required - provided
        assert not missing, (
            f"lambda/scheduler.py reads {sorted(missing)} but main.tf does not set them; "
            "MANAGER_TAG_VALUE in particular fails the invocation closed"
        )

    def test_manager_tag_value_has_no_fallback(self):
        # This decides which AMIs cleanup may delete. With a default, two
        # deployments in one account could delete each other's backups, so read
        # it with os.environ and fail loudly when it is missing.
        source = Path(scheduler.__file__).read_text()
        assert 'os.environ["MANAGER_TAG_VALUE"]' in source
        assert 'os.getenv("MANAGER_TAG_VALUE"' not in source

    def test_manager_tag_value_is_deployment_scoped(self):
        # Built from name_prefix, never a shared fixed string.
        assert "MANAGER_TAG_VALUE        = local.manager_tag_value" in MAIN_TF
        assert 'manager_tag_value = "${var.name_prefix}-scheduler"' in MAIN_TF

    def test_no_variable_is_set_but_never_read(self):
        provided = terraform_env_block("scheduler")
        read = env_vars_read_by(scheduler)

        # An unused variable looks like it still does something.
        assert not provided - read


class TestAmiTransferEnvironmentContract:
    def test_every_variable_the_handler_reads_is_set(self):
        provided = terraform_env_block("ami_transfer")
        required = env_vars_read_by(ami_transfer) - RUNTIME_PROVIDED_ENV

        assert not required - provided

    def test_no_variable_is_set_but_never_read(self):
        provided = terraform_env_block("ami_transfer")
        assert not provided - env_vars_read_by(ami_transfer)

    def test_manager_tag_value_has_no_fallback(self):
        source = Path(ami_transfer.__file__).read_text()
        assert 'os.environ["MANAGER_TAG_VALUE"]' in source

    def test_aws_region_is_not_set_by_terraform(self):
        # Lambda reserves AWS_*, so setting it fails the apply. The handler reads
        # the value Lambda provides instead.
        assert "AWS_REGION" not in terraform_env_block("ami_transfer")
        assert "AWS_REGION" in env_vars_read_by(ami_transfer)


class TestScheduleActionContract:
    """Every action main.tf schedules must be one lambda_handler routes."""

    def scheduled_actions(self):
        # Only the locals block holds schedule payloads. Stop before
        # aws_lambda_permission, where "action" means an IAM verb.
        locals_end = MAIN_TF.find('resource "aws_kms_key"')
        assert locals_end != -1
        actions = set(re.findall(r'^\s+action\s+=\s+"([a-z_]+)"', MAIN_TF[:locals_end], re.MULTILINE))
        assert actions, "no scheduled actions parsed from main.tf"
        return actions

    def test_every_scheduled_action_is_routed(self, monkeypatch):
        # Stub every implementation so routing is tested without touching AWS -
        # letting the real ones run would make this the only test in the suite
        # that opens a network connection.
        for name, result in (
            ("manage_instances", {"count": 0, "instances": []}),
            ("enforce_schedule", {"count": 0, "instances": []}),
            ("create_amis", []),
            ("cleanup_amis", {"cleaned": [], "errors": []}),
            ("cleanup_daily_snapshots", []),
            ("create_daily_snapshots", []),
            ("run_security_update", {}),
        ):
            monkeypatch.setattr(scheduler, name, lambda *a, _r=result, **k: _r)

        for action in self.scheduled_actions():
            response = scheduler.lambda_handler({"action": action}, None)
            assert response["action"] == action, (
                f"main.tf schedules {action!r} but lambda_handler does not route it; "
                "every invocation of that schedule would fail into the DLQ"
            )

    def test_the_expected_action_set_is_scheduled(self):
        # Catches a schedule going missing, not just a misspelled one.
        assert self.scheduled_actions() == {
            "start",
            "stop",
            "enforce_schedule",
            "create_ami",
            "create_daily_snapshots",
            "cleanup_amis",
            "security_update",
        }

    @pytest.mark.parametrize("backup_type", ["weekly", "monthly"])
    def test_scheduled_backup_types_have_a_retention_window(self, backup_type, monkeypatch):
        # cleanup_amis reads the BackupType tag. A type it does not know falls
        # into the daily window and gets deleted much earlier than configured.
        assert f'backup_type = "{backup_type}"' in MAIN_TF
        assert f'{backup_type.upper()}_RETENTION_DAYS' in MAIN_TF


class TestRuntimeContract:
    def runtimes(self):
        found = set(re.findall(r'runtime\s+=\s+"(python[0-9.]+)"', MAIN_TF))
        assert found, "no Lambda runtime parsed from main.tf"
        return found

    def test_both_functions_use_one_runtime(self):
        assert len(self.runtimes()) == 1

    def test_ci_python_matches_the_lambda_runtime(self):
        # Running the tests on a different minor version than the functions
        # deploy to would miss anything that only breaks on one of them.
        workflow = (REPO_ROOT / ".github" / "workflows" / "terraform-ci.yml").read_text()
        ci_version = re.search(r"python-version:\s*'([0-9.]+)'", workflow)
        assert ci_version, "no python-version pinned in terraform-ci.yml"

        assert self.runtimes() == {f"python{ci_version.group(1)}"}


class TestTagKeyContract:
    def test_iam_copy_policy_allows_exactly_the_tags_the_lambda_writes(self):
        # The copy policy limits aws:TagKeys. Any other key fails CopyImage.
        allowed = set(hcl_string_list(MAIN_TF, '"aws:TagKeys"'))
        assert allowed == {"CreatedBy", "BackupType", "SourceImageId"}

    def test_scheduler_mode_tag_key_matches_the_instance_tag(self):
        # The submodule tags the instance with "scheduler" and the Lambda finds
        # the fleet by that key. A mismatch gives an empty fleet and a scheduler
        # that looks healthy while doing nothing.
        assert 'SCHEDULER_MODE_TAG_KEY   = "scheduler"' in MAIN_TF
        submodule = (REPO_ROOT / "modules" / "ec2-instance" / "main.tf").read_text()
        assert 'key         = "scheduler"' in submodule
