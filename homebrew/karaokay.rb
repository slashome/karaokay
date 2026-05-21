# Formula template for the Homebrew tap (`slashome/homebrew-tap`).
#
# Setup once:
#   1. Push a Git tag (e.g. v0.1.0) on the karaokay repo and create the GitHub release.
#   2. Compute the tarball checksum:
#        curl -sL https://github.com/slashome/karaokay/archive/refs/tags/v0.1.0.tar.gz | shasum -a 256
#      Paste the value into `sha256` below.
#   3. Generate the `resource` blocks (transitive Python deps) — pick one:
#        a) pip install homebrew-pypi-poet
#           poet python-mpd2 syncedlyrics    # paste output between the markers
#        b) brew install pipgrip
#           brew update-python-resources Formula/karaokay.rb    # from inside the tap repo
#   4. Copy this file to `slashome/homebrew-tap/Formula/karaokay.rb`, commit, push.
#   5. Test:
#        brew install --build-from-source slashome/tap/karaokay
#        brew test slashome/tap/karaokay
#        brew audit --strict --new slashome/tap/karaokay
#
# Bumping the version: update `url`, `sha256`, regenerate resources, commit.

class Karaokay < Formula
  include Language::Python::Virtualenv

  desc "Synchronized lyrics in your terminal, powered by MPD"
  homepage "https://github.com/slashome/karaokay"
  url "https://github.com/slashome/karaokay/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "REPLACE_WITH_TARBALL_SHA256"
  license "MIT"

  depends_on "python@3.12"

  # >>> resources: auto-generated, do not edit by hand
  # (run `poet python-mpd2 syncedlyrics` and paste the output here)
  # <<< resources

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "Karaoké", shell_output("#{bin}/karaokay --help")
  end
end
