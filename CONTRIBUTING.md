# Contributing

## Workflow

1. Create a feature branch.
2. Make the smallest focused change that solves the problem.
3. Run `terraform fmt`, `terraform validate`, and any relevant checks.
4. Update `README.md` or other docs when behavior changes.
5. Open a pull request to `main` with a Conventional Commit title.

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

Protect `main` with required pull requests and require these checks before merge: `Conventional PR` and `Terraform CI`.

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
