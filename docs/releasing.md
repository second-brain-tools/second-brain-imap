# Releasing

Releases are published to **PyPI** by the [`release`](../.github/workflows/release.yml)
workflow through an OIDC **trusted publisher**: no API token is stored anywhere.

On a `v*` tag push the workflow first runs the full [`checks`](../.github/workflows/checks.yml)
suite on the tagged commit: lint, types, the test matrix, the package build, and the smoke test of
the built wheel on Linux, macOS and Windows. A red check **blocks the publish**. The sdist and wheel
that are published are the ones built and tested in that suite; the publish job does not rebuild.
Re-publishing a version that is already on PyPI is skipped, so re-running a release is safe.

## The release order

Sign-in, the audit log, HTTP serving and the settings-file rules come from
[second-brain-core](https://github.com/second-brain-tools/second-brain-core). A change there is
released there **first**; then this package's pin moves to it (`second-brain-core>=X.Y.Z,<X.(Y+1)`
in `pyproject.toml`, and `uv lock`), and this package is released as usual. A release of this
package never depends on an unreleased library.

## One-time operator setup

1. **Trusted publisher on PyPI:** a GitHub publisher on <https://pypi.org> with owner
   `second-brain-tools`, repository `second-brain-imap`, workflow `release.yml`, environment
   `pypi`. Before the first release it is a *pending* publisher (**Account → Publishing → Add a
   pending publisher**); afterwards it is under **Manage project → Publishing**.
2. **GitHub environment:** an Actions environment named **`pypi`** (Settings → Environments). The
   workflow declares `environment: pypi`, and the name must match the publisher exactly.
3. **Private vulnerability reporting** on (Settings → Code security), so the way to report a
   problem that [SECURITY.md](../SECURITY.md) gives exists.

Only a `v*` tag publishes. Run by hand from a branch, the workflow runs the checks and stops.

## Cutting a release

1. Bump `__version__` in [`src/second_brain_imap/__init__.py`](../src/second_brain_imap/__init__.py)
   (hatch reads it) and name the release in [`CHANGELOG.md`](../CHANGELOG.md): the `[Unreleased]`
   entries move under `## [<version>] — <name>`.
2. Merge that to `main`, then put a signed tag matching the version on the merged commit and push
   it:

   ```bash
   git tag -s v0.1.0 -m "v0.1.0 — First release"
   git push origin v0.1.0
   ```

3. The workflow runs the checks and, only if everything is green, publishes the tested sdist and
   wheel.
4. Once PyPI has it, the workflow creates the **GitHub Release** for the tag with
   [`scripts/github-release.sh`](../scripts/github-release.sh): the title and notes come from the
   version's `## [<version>]` section of the CHANGELOG (the run fails if that section is missing or
   empty), the sdist and wheel are attached, and a pre-release version is marked as one.
   Re-running updates the existing release instead of failing.

## Verify

Start the published version the way a user does and talk MCP to it, the check the workflow ran
against the wheel, now against PyPI:

```bash
uv cache clean second-brain-imap   # uv remembers what PyPI offered a few minutes ago
uv run scripts/smoke.py --from "second-brain-imap==0.1.0" --expect 0.1.0
```

PyPI itself can take a minute to offer a new version. The smoke test needs no mail server: the
server is pointed at a closed port and must say so.
