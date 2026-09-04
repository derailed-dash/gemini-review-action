# Extensions — override or extend agents-cli

An extension overrides or adds a built-in command. It's a directory containing an `agents-cli-extension.yaml`, so any git repo can serve as a registry.

Two things you can do: **author** an extension (start ad-hoc in the current repo, publish it later), or **adopt** an existing one.

---

## Author an ad-hoc extension (no separate repo)

Drop a single file, `agents-cli-extension.yaml`, at the project root (next to `agents-cli-manifest.yaml`). It's auto-loaded at project scope, so no `extension add` is needed. Commit it and teammates and CI get the same overrides.

Only one, at that exact path, and always project scope. For a second one, or to install a local extension globally, move it into its own directory and `agents-cli extension add local@./path --global`; `#name` picks one out of a directory holding several.

**Publish it for other repos:** move the same file (and its scripts) into its own git repo and tag it — others then `agents-cli extension add <org>/<repo>#<name> --ref v1.0.0`. The file doesn't change; ad-hoc and shared are the same format.

### Schema by example (`agents-cli-extension/v1alpha1`)

Everything below is optional except `run` on a command (a non-empty list). Unknown keys are rejected, so a typo fails loudly instead of silently doing nothing.

Machine-readable equivalent: `schemas/agents-cli-extension-v1alpha1.schema.json` in the agents-cli repo, generated from the same models the loader uses. Point a `yaml-language-server` modeline at it for editor validation.

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/google/agents-cli/main/schemas/agents-cli-extension-v1alpha1.schema.json
schema: agents-cli-extension/v1alpha1
name: my-extension
description: What this extension does.
requires:
  agents_cli: ">=1.3,<2"   # example - derive from `agents-cli --version`, see below
  on_incompatible: warn    # warn (install + warn) | error (refuse at add/update, block its commands if the CLI drifts out)

commands:
  override:               # replace a built-in; user argv passes through verbatim
    deploy:
      run: ["uv", "run", "scripts/custom_deploy.py"]
      description: SBOM upload, then the built-in deploy.
    eval.generate:        # dotted name = a subcommand (group.sub)
      run: ["uv", "run", "scripts/eval_generate.py"]
      description: Framework-specific inference runner.
  add:                    # a brand-new command that doesn't exist yet
    compliance-report:
      run: ["python", "scripts/compliance_report.py"]
      description: Generate the quarterly compliance report.
```

### Rules that matter

- **`run:` is a command vector** executed with **no shell**; user argv is appended verbatim. Paths relative to the extension dir resolve to absolute, and `$AGENTS_CLI_EXTENSION_DIR` locates sibling scripts/templates.
- **You can't override a command *group*** (e.g. `eval`) — override a specific subcommand (`eval.generate`); peers like `eval grade`/`eval compare` keep their built-in behavior.
- **Re-invoke the built-in safely.** An override runs with `AGENTS_CLI_DISABLE_OVERRIDES=1`, so calling `agents-cli deploy` inside your wrapper hits the built-in (no infinite recursion).
- **Chaining is done in a wrapper script**, since `run:` is a single vector (not a shell line). To do "check, then the built-in", point `run:` at a script that sequences the steps:
  ```bash
  #!/usr/bin/env bash
  set -e
  "$AGENTS_CLI_EXTENSION_DIR/scripts/compliance_check.sh"   # non-zero here aborts
  agents-cli deploy "$@"                                  # hits the built-in (guard is set)
  ```
- **Start `run:` with a program, not a script.** The vector is executed directly — no shell — so
  `["python", "scripts/x.py"]` or `["uv", "run", "python", "scripts/x.py"]` works everywhere, while a
  bare `["scripts/x.py"]` relies on a shebang and never runs on Windows (the CLI warns if you do it).
  Paths in the vector are resolved relative to the extension directory, so they work from any cwd.
- **Conflicts** (same scope, shown in `agents-cli extension list` / `agents-cli info`): two extensions claiming one command is first-wins, later ignored. Cross-scope is fine — project wins over user (`--global`).
- **Declare a compatibility range** with `requires`, always. Run `agents-cli --version`, set the lower bound to that `major.minor` and the upper bound to the next major (`>=1.3,<2` if the CLI reports 1.3.x — the number in the example above is only an example). Let the user pick `on_incompatible`; default `warn`.
  - `warn`: installs, runs, and warns when out of range.
  - `error`: `extension add`/`update` refuse an out-of-range install, and if a later CLI upgrade moves you out of range the extension's **commands fail** with the range and the fix rather than silently running the built-in, which would do something else. The recovery commands (`install`, `extension *`) keep working either way.
  - `schema` (`agents-cli-extension/v1alpha1`) tracks the manifest format, not the CLI version, and is accepted across CLI majors.

---

## Adopt an existing extension

```bash
agents-cli extension add <ref> [--global] [--ref <branch|tag|sha>] [--yes]
agents-cli extension list                 # what's active, its scope, and its commands
agents-cli extension update [<name>]      # advance the pin (re-resolve the tracked ref)
agents-cli extension remove <name>        # drop it and delete its vendored copy
agents-cli info                        # shows active extensions + sources + conflicts
```

### Reference forms

| Form | Meaning |
|------|---------|
| `acme/acli-extensions` | any `org/repo` on github.com |
| `acme/acli-extensions#soc2-deploy` | select one extension from a multi-extension repo |
| `https://git.example.com/acme/acli-extensions` | any git host — `https://`, `http://` or `ssh://` |
| `git@git.example.com:acme/acli-extensions` | the same host, in scp form |
| `local@../my-extension` | a local path (for development) |
| `<name>` | first-party shorthand — resolves to the `google/agents-cli` repo |
| `--ref <branch\|tag\|sha>` | pin a branch, tag, or commit SHA |

A URL is cloned with your ambient git configuration, so whatever already authenticates you to that host (credential helper, ssh agent) applies — agents-cli neither asks for nor stores credentials.

An extension changes commands. To run a different agent framework, scaffold from that framework's template instead (`agents-cli create my-agent --agent <org>/<repo>@<tag>`); the template carries its own `agents-cli-extension.yaml`, so nothing is installed machine-wide.

### Scopes

- **Project scope (default)** — committed to *this repo only* (recorded in `agents-cli-extensions.yaml`, working copy vendored under `extensions/`), so it can't affect your other projects and CI/teammates get it. Commit both files so it works offline (`install` re-fetches if missing — see [Pinning and updates](#pinning-and-updates)).
- **User scope (`--global`)** — `~/.config/agents-cli/`. Applies to **every** project on the machine, and works before a project exists (e.g. to apply an override before a project exists). Because a global override changes commands everywhere, prefer project scope unless you truly want it machine-wide.
- When both scopes define the same command, **project wins**; `agents-cli info` shows the source.

### Trust

- First-party extensions added via the **shorthand** form (`extension add <name>`) are trusted automatically.
- Every other reference — an `org/repo`, a URL, a local path, and `google/agents-cli` typed in full — **prompts before install** (its commands run arbitrary code when invoked). `--yes` skips the prompt (blanket trust — use only for automation/bootstrap).

### Pinning and updates

- `extension add` resolves the ref to an exact commit SHA and records `source` / `ref` / `sha` under `extensions:` in `agents-cli-extensions.yaml` (its own file at both scopes, so the writer can never touch your project manifest), vendoring a working copy under `extensions/`.
- `agents-cli install` re-materializes any missing/stale vendored copy from the pinned SHA (verified by a stamp file) — CI and fresh checkouts get the exact reviewed code and command overrides. It never advances a pin.
- `extension update [name]` advances the pin to the latest commit of the **same tracked ref** (e.g. a branch), re-prompting for third-party trust. Nothing updates in the background. Note: a pinned tag/SHA re-resolves to itself — to move to a **different** tag, re-run `extension add <ref> --ref <new-tag>` (it replaces the existing pin).
- `extension remove <name>` drops the entry and deletes the working copy. It removes from one scope per call (project before user), so an extension installed at both scopes needs a second `remove`.

Tracking a version:

```bash
agents-cli extension add acme/acli-extensions#soc2 --ref v1.2.0   # pin a tag (recommended)
agents-cli extension add acme/acli-extensions#soc2 --ref main     # follow a branch
agents-cli extension update soc2                               # advance within the tracked ref
agents-cli extension add acme/acli-extensions#soc2 --ref v1.3.0   # move to another tag: re-add, not update
agents-cli extension add acme/acli-extensions#soc2 --ref v1.2.0   # roll back the same way
```

A failed re-`add` rolls back to nothing rather than to the previous pin, so re-add the old ref to
restore it.
