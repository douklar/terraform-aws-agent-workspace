"""The GitHub Actions workflows.

The registry publishes tags, not branches, so what matters is which branch may
create which kind of tag: main produces stable versions, dev only pre-releases,
and no job rewrites the tree it publishes.
"""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML is needed to parse the workflow files")

WORKFLOW_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"
WORKFLOWS = sorted(WORKFLOW_DIR.glob("*.yml"))


def load(name):
    return yaml.safe_load((WORKFLOW_DIR / name).read_text())


def workflow_triggers(document):
    # PyYAML resolves the bare key `on:` to the boolean True (YAML 1.1), so the
    # trigger block is keyed by True rather than the string "on".
    return document.get("on", document.get(True))


class TestWorkflowsParse:
    def test_at_least_the_expected_workflows_exist(self):
        names = {path.name for path in WORKFLOWS}
        assert {
            "terraform-ci.yml",
            "main-release.yml",
            "conventional-pr.yml",
            "prerelease.yml",
        } <= names

    @pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
    def test_workflow_is_valid_yaml_with_jobs(self, path):
        document = yaml.safe_load(path.read_text())

        assert isinstance(document, dict), f"{path.name} did not parse to a mapping"
        assert document.get("jobs"), f"{path.name} defines no jobs"
        assert workflow_triggers(document), f"{path.name} defines no triggers"

    @pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
    def test_every_job_declares_a_runner(self, path):
        for name, job in yaml.safe_load(path.read_text())["jobs"].items():
            assert job.get("runs-on"), f"{path.name}:{name} has no runs-on"


class TestStableRelease:
    @pytest.fixture
    def steps(self):
        return load("main-release.yml")["jobs"]["release"]["steps"]

    def test_release_runs_only_on_a_merged_pull_request(self):
        job = load("main-release.yml")["jobs"]["release"]
        assert job["if"] == "github.event.pull_request.merged == true"

    def test_only_main_can_cut_a_stable_release(self):
        triggers = workflow_triggers(load("main-release.yml"))
        assert triggers["pull_request"]["branches"] == ["main"]

    def test_release_checks_out_main_so_the_merge_commit_is_reachable(self, steps):
        checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout"))
        assert checkout["with"]["ref"] == "main"
        # The version calculation walks tags and the base..merge range.
        assert checkout["with"]["fetch-depth"] == 0

    def test_tag_points_at_this_pull_requests_merge_commit(self, steps):
        # Not `git rev-parse HEAD`: a second merge landing mid-run would ship
        # under this job's version.
        tag_step = next(s for s in steps if s.get("name") == "Create and push tag")
        assert "steps.version.outputs.merge_sha" in tag_step["run"]
        assert "git rev-parse HEAD" not in tag_step["run"]

    def test_release_publishes_the_tree_it_was_handed(self, steps):
        # The registry serves the tagged tree as-is, and tests/ and examples/
        # belong in a published module. Rewriting main also diverges it from dev.
        commands = " ".join(step.get("run", "") for step in steps)

        assert "git rm" not in commands
        assert "git push origin main" not in commands
        assert "git commit" not in commands

    def test_main_never_produces_a_prerelease_tag(self, steps):
        # Pre-releases come from dev via prerelease.yml. One route only.
        commands = " ".join(step.get("run", "") for step in steps)

        assert "prerelease" not in commands.lower()

    def test_has_the_write_permission_it_needs(self):
        assert load("main-release.yml")["permissions"]["contents"] == "write"


class TestPrerelease:
    @pytest.fixture
    def steps(self):
        return load("prerelease.yml")["jobs"]["prerelease"]["steps"]

    def test_prerelease_is_manual_only(self):
        triggers = workflow_triggers(load("prerelease.yml"))
        assert set(triggers) == {"workflow_dispatch"}

    def test_prerelease_tags_dev(self, steps):
        checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout"))
        assert checkout["with"]["ref"] == "dev"

    def test_prerelease_refuses_a_stable_tag(self, steps):
        # Otherwise the dispatch box publishes stable versions from dev.
        commands = " ".join(step.get("run", "") for step in steps)

        assert r"^v[0-9]+\.[0-9]+\.[0-9]+-[0-9A-Za-z.-]+$" in commands

    def test_prerelease_is_marked_as_one_on_github(self, steps):
        release = next(s for s in steps if str(s.get("uses", "")).startswith("softprops/"))
        assert release["with"]["prerelease"] is True


class TestCiCoverage:
    def test_ci_gates_pull_requests_into_both_long_lived_branches(self):
        # Gating only main lets a broken change sit on dev until release day.
        triggers = workflow_triggers(load("terraform-ci.yml"))
        assert set(triggers["pull_request"]["branches"]) == {"main", "dev"}

    def test_conventional_titles_are_required_on_both_branches(self):
        # The version bump comes from the PR title.
        triggers = workflow_triggers(load("conventional-pr.yml"))
        assert set(triggers["pull_request"]["branches"]) == {"main", "dev"}

    def test_ci_runs_the_python_and_terraform_suites(self):
        steps = load("terraform-ci.yml")["jobs"]["terraform"]["steps"]
        commands = " ".join(step.get("run", "") for step in steps)

        assert "pytest tests/" in commands
        assert "terraform test" in commands

    def test_ci_lints_the_python_code(self):
        steps = load("terraform-ci.yml")["jobs"]["terraform"]["steps"]
        commands = " ".join(step.get("run", "") for step in steps)

        assert "ruff check" in commands

    def test_ci_reacts_to_changes_in_the_tests_themselves(self):
        paths = workflow_triggers(load("terraform-ci.yml"))["pull_request"]["paths"]
        assert "tests/**" in paths
        assert "lambda/**" in paths
