# Contributing

Thanks for your interest in second-brain-imap.

## Where the code lives

Sign-in, the audit log, HTTP serving and the settings-file rules are shared with the other Second
Brain servers in [second-brain-core](https://github.com/second-brain-tools/second-brain-core).
**Fix shared code there**, release it, then move this package's pin; this repository holds what is
the mail server's own (the IMAP client, the mailbox rules, the message reader, the tools). To try
an unreleased core change here, point uv at your checkout for the moment, and don't commit it:

```toml
[tool.uv.sources]
second-brain-core = { path = "../second-brain-core", editable = true }
```

## Development setup

```bash
git clone https://github.com/second-brain-tools/second-brain-imap
cd second-brain-imap
uv sync
uv run pre-commit install   # lint and format on every commit
```

## Tests, lint and types

```bash
uv run pytest --cov-fail-under=90   # the gate CI enforces
uvx pre-commit run --all-files      # ruff lint + format + actionlint, as CI
uv run mypy                         # strict, over src, tests and scripts
```

The suite needs no mail server: the IMAP side is a scripted server in the tests. The **read-only
rule is tested, not assumed**: one test reads [imap.py](src/second_brain_imap/imap.py)'s syntax
tree and fails if it calls anything on the IMAP client but the commands this server sends (see
[SECURITY.md](SECURITY.md)). A change that needs another IMAP command changes that test too, and
says why in the pull request.

## How CI works

One reusable workflow, [`checks.yml`](.github/workflows/checks.yml), holds every gate: lint, types,
tests on Python 3.11 to 3.15 on Linux and 3.13 on macOS and Windows, and a build that runs
`twine check` and the smoke test of the wheel ([`scripts/smoke.py`](scripts/smoke.py)) on all three
systems. [`ci.yml`](.github/workflows/ci.yml) runs it on every push and pull request to `main`,
and [`release.yml`](.github/workflows/release.yml) runs it before publishing
([docs/releasing.md](docs/releasing.md)). [`labeler.yml`](.github/workflows/labeler.yml) labels
pull requests by the files they change; that is advisory, not a gate.

## Issues and labels

Each issue is filed from one of the issue forms (bug, feature, task, spike, epic) and gets a
milestone or the `backlog` label, and one or more `component:` labels: `imap`, `mcp`, `docs`,
`ci`.

## License of contributions

The server is licensed under the GNU Affero General Public License, version 3 or later
([LICENSE](LICENSE)). A contribution is accepted under that license: by opening a pull request you
agree to it. There is no contributor agreement to sign. A sign-off (`git commit -s`) says you have
the right to submit the change under those terms
([Developer Certificate of Origin](https://developercertificate.org/)).

## Pull requests

- Target `main`; one logical change per pull request; squash-merged.
- A short, lowercase conventional title (`fix: …`, `feat: …`, `docs: …`).
- Add or update tests for any new behaviour, and a CHANGELOG line under `[Unreleased]`.
- CI must pass before merge.
