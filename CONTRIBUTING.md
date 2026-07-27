# Contributing

## Branches

Two long-lived branches, and only one of them can publish a stable version:

| Branch | Role |
| --- | --- |
| `dev` | Default branch. All day-to-day work lands here. Nothing here is published to the Terraform Registry unless someone deliberately cuts a pre-release. |
| `main` | Release branch. Every merge into it publishes a stable `vX.Y.Z` tag. Only `dev` and `hotfix/*` branches may target it. |

The Terraform Registry publishes **tags**, not branches, so `dev` can move as
fast as you like without affecting any released version. Consumers pin
`version = "~> 3.0"`; that constraint only ever resolves to a stable tag, and
tags are only created by the two workflows described below.

Nothing is stripped from the released tree. `tests/`, `examples/`, and CI
config are part of the standard Terraform module structure and are expected to
be present in a published module.

## Workflow

1. Branch off `dev` (`feat/...`, `fix/...`, `docs/...`).
2. Make the smallest focused change that solves the problem.
3. Run `terraform fmt`, `terraform validate`, and the checks under [Testing](#testing).
4. Update `README.md`, `CHANGELOG.md`, or other docs when behavior changes.
5. Open a pull request to `dev` with a Conventional Commit title. `Terraform CI`
   and `Conventional PR` must pass.

### Releasing

Open a pull request from `dev` to `main`. Its title decides the version bump
(see [Release version mapping](#commit-and-release-standard)), so title the
release PR after the most significant change it carries. A release containing
any `feat!:` commit needs a `feat!:` title. On merge, `Release main` tags the
merge commit and publishes a GitHub release.

### Pre-releases

To try a version from the registry before it is ready for `main`, run the
**Pre-release from dev** workflow (Actions → Run workflow) with a tag such as
`v3.0.0-alpha.2`. It tags `dev` directly. Terraform ignores pre-release
versions unless one is requested exactly, so `~> 3.0` consumers never receive
it. `main` is untouched.

### Hotfixes

Branch off `main`, PR back into `main`, which releases the patch immediately.
Then merge `main` into `dev` so the fix is not lost on the next release.

## Repo Layout

- `main.tf` contains the root Harbor module orchestration for scheduler, Lambda, and shared AWS resources.
- `modules/ec2-instance` contains the reusable EC2 instance submodule.
- `lambda/` contains the Lambda handlers packaged by Terraform.

## Coding Notes

- Keep generated files out of version control.
- Prefer small, reviewable Terraform diffs.
- Update `.gitignore` when new build artifacts appear.

## Testing

At minimum, run:

```bash
terraform fmt -check -recursive
terraform init -backend=false -input=false
terraform validate
terraform -chdir=examples/basic init -backend=false -input=false
terraform -chdir=examples/basic validate
terraform -chdir=modules/ec2-instance/examples/basic init -backend=false -input=false
terraform -chdir=modules/ec2-instance/examples/basic validate
python -m py_compile lambda/*.py
```

Do not commit tracked `__pycache__` or `*.pyc` files.

## Commit And Release Standard

Stable releases are created only from merged pull requests targeting `main`.

Protect both `main` and `dev` with required pull requests and require these checks before merge: `Conventional PR` and `Terraform CI`. `main` additionally restricts who may push, since a push there publishes a version.

Pull request titles and commit messages must follow Conventional Commits:

```text
<type>[optional scope][!]: <description>
```

Allowed types:

- `feat`
- `fix`
- `docs`
- `style`
- `refactor`
- `perf`
- `test`
- `build`
- `ci`
- `chore`
- `revert`

Release version mapping:

| Conventional Commit signal | Version bump | Example |
| --- | --- | --- |
| First merged PR to `main` with no existing `v*` tag | `v1.0.0` | `feat: initial Harbor module release` |
| `feat:` | minor | `v1.0.0` -> `v1.1.0` |
| `fix:`, `docs:`, `style:`, `refactor:`, `perf:`, `test:`, `build:`, `ci:`, `chore:`, `revert:` | patch | `v1.0.0` -> `v1.0.1` |
| `type!:` or `BREAKING CHANGE:` | major | `v1.2.3` -> `v2.0.0` |

Examples:

```text
feat: add EC2 workspace module
fix: correct Tailscale bootstrap retry
docs: update module usage examples
chore: update Terraform provider constraints
feat!: change module input schema
```

Install local hooks before committing:

```bash
pre-commit install
```

The pre-commit configuration installs both normal pre-commit hooks and a `commit-msg` hook that rejects non-Conventional Commit messages.
