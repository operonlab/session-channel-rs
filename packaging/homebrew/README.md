# Homebrew tap

The `session-channel.rb` formula in this directory is the **source of truth**.
The deployed copy that users actually install from lives at:

- Repo: <https://github.com/operonlab/homebrew-tap>
- Path: `Formula/session-channel.rb`

## One-time setup (when first publishing the tap)

```bash
# 1. Create the tap repo on GitHub (must be named `homebrew-tap`)
gh repo create operonlab/homebrew-tap --public --description "Homebrew tap for operonlab tools"

# 2. Clone and seed
git clone https://github.com/operonlab/homebrew-tap
cd homebrew-tap
mkdir -p Formula
cp ../session-channel/packaging/homebrew/session-channel.rb Formula/

# 3. Fill in the four sha256 values from the latest GitHub Release tarballs
#    (each tarball ships with a .sha256 companion file).

# 4. Commit + push
git add Formula/session-channel.rb
git commit -m "feat: add session-channel formula"
git push
```

Users can then install with:

```bash
brew install operonlab/tap/session-channel
# or, explicit tap step:
brew tap operonlab/tap
brew install session-channel
```

## Per-release bump

For every new `v*.*.*` tag pushed to `operonlab/session-channel`:

1. Wait for the `Release` workflow to finish (it uploads four tarballs + `.sha256` files).
2. Copy this directory's `session-channel.rb` over the tap copy.
3. Replace `version` with the new tag (without the `v` prefix).
4. Replace the four `sha256` placeholders with values from the `.sha256` files.
5. `git commit -m "session-channel <version>"` + `git push` in the tap repo.

Automation lives on the roadmap as `update-tap.yml` — see `CHANGELOG.md`.
