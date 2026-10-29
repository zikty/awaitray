# Releasing

1. Bump `version` in `pyproject.toml`, commit, and push to the default
   branch.
2. Tag it: `git tag v<version> && git push origin v<version>`.

The **Release** workflow does the rest:

- checks that the tag matches `pyproject.toml`;
- builds the `.deb` and publishes the GitHub release with it attached;
- updates `Formula/awaitray.rb` in the tap `zikty/homebrew-awaitray` with
  the source tarball's url and sha256;
- updates the AUR package `awaitray` (`pkgver`, `sha256sums`, `.SRCINFO`).

The two package steps need repository secrets, and are skipped with a
notice when one is missing:

| Secret | What it is |
|---|---|
| `HOMEBREW_TAP_TOKEN` | fine-grained token with *Contents: read and write* on the tap repo only |
| `AUR_SSH_KEY` | private SSH key whose public half is on the AUR account that maintains `awaitray` |

`packaging/homebrew/awaitray.rb` and `packaging/aur/PKGBUILD` are the
templates: change them here, never in the tap or the AUR repo, or the next
release overwrites the change.

The **Packaging** workflow builds and installs all three from the current
checkout whenever `packaging/` changes, so a release never ships an untested
package recipe.
