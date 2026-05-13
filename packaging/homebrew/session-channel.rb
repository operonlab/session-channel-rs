# Homebrew formula for session-channel — pre-built binary flavour.
#
# This file is the SOURCE OF TRUTH; the deployed copy lives at
# operonlab/homebrew-tap/Formula/session-channel.rb. After each
# release, copy this file over, update `version` + the four `sha256`
# values, and commit to the tap repo.
#
# A workflow to do that automatically is on the roadmap (see CHANGELOG).
#
# Users install via:
#   brew install operonlab/tap/session-channel
#
# brew tap operonlab/tap   (= github.com/operonlab/homebrew-tap)

class SessionChannel < Formula
  desc "Cross-pane, cross-CLI pub-sub bus over tmux + Redis Streams"
  homepage "https://github.com/operonlab/session-channel"
  version "0.2.0"
  license "MIT"

  on_macos do
    on_arm do
      url "https://github.com/operonlab/session-channel/releases/download/v#{version}/session-channel-v#{version}-aarch64-apple-darwin.tar.gz"
      sha256 "REPLACE_WITH_aarch64-apple-darwin_SHA256"
    end
    on_intel do
      url "https://github.com/operonlab/session-channel/releases/download/v#{version}/session-channel-v#{version}-x86_64-apple-darwin.tar.gz"
      sha256 "REPLACE_WITH_x86_64-apple-darwin_SHA256"
    end
  end

  on_linux do
    on_arm do
      url "https://github.com/operonlab/session-channel/releases/download/v#{version}/session-channel-v#{version}-aarch64-unknown-linux-gnu.tar.gz"
      sha256 "REPLACE_WITH_aarch64-unknown-linux-gnu_SHA256"
    end
    on_intel do
      url "https://github.com/operonlab/session-channel/releases/download/v#{version}/session-channel-v#{version}-x86_64-unknown-linux-gnu.tar.gz"
      sha256 "REPLACE_WITH_x86_64-unknown-linux-gnu_SHA256"
    end
  end

  # Redis is recommended (the service needs one) but not required —
  # users may already run Redis via Docker, brew services, or a remote host.
  depends_on "redis" => :recommended

  def install
    bin.install "channel"
    bin.install "channel-service"
    # Tarballs from v0.2.0 onward ship LICENSE + README.md alongside the binaries.
    pkgshare.install "LICENSE" if File.exist?("LICENSE")
    pkgshare.install "README.md" if File.exist?("README.md")
  end

  # `brew services start session-channel` launches channel-service in the
  # background; logs go to var/log.
  service do
    run [opt_bin/"channel-service"]
    keep_alive true
    log_path var/"log/session-channel.log"
    error_log_path var/"log/session-channel.log"
  end

  test do
    assert_match "channel 0.", shell_output("#{bin}/channel --version")
    assert_match "channel-service 0.", shell_output("#{bin}/channel-service --version")
  end
end
