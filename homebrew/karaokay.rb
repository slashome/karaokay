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
  sha256 "9c34329fdf772adcb4f2699cb31aeef78e75fbb2169faf7097e6e61aea3c4462"
  license "MIT"

  depends_on "python@3.13"

  # >>> resources: regenerate with `poet --resources python-mpd2 --also syncedlyrics`
  resource "beautifulsoup4" do
    url "https://files.pythonhosted.org/packages/c3/b0/1c6a16426d389813b48d95e26898aff79abbde42ad353958ad95cc8c9b21/beautifulsoup4-4.14.3.tar.gz"
    sha256 "6292b1c5186d356bba669ef9f7f051757099565ad9ada5dd630bd9de5fa7fb86"
  end

  resource "certifi" do
    url "https://files.pythonhosted.org/packages/f3/ce/ee2ecad540810a79593028e88299baeae54d346cc7a0d94b6199988b89b1/certifi-2026.5.20.tar.gz"
    sha256 "69dea482ab64caa7b9f6aba1c6bf48bb6a5448d1c0f1b17ab42ad8c763a5344d"
  end

  resource "charset-normalizer" do
    url "https://files.pythonhosted.org/packages/e7/a1/67fe25fac3c7642725500a3f6cfe5821ad557c3abb11c9d20d12c7008d3e/charset_normalizer-3.4.7.tar.gz"
    sha256 "ae89db9e5f98a11a4bf50407d4363e7b09b31e55bc117b4f7d80aab97ba009e5"
  end

  resource "idna" do
    url "https://files.pythonhosted.org/packages/82/77/7b3966d0b9d1d31a36ddf1746926a11dface89a83409bf1483f0237aa758/idna-3.15.tar.gz"
    sha256 "ca962446ea538f7092a95e057da437618e886f4d349216d2b1e294abfdb65fdc"
  end

  resource "python-mpd2" do
    url "https://files.pythonhosted.org/packages/53/be/e77206eb35eb37ccd3506fba237e1431431d04c482707730ce2a6802e95c/python-mpd2-3.1.1.tar.gz"
    sha256 "4baec3584cc43ed9948d5559079fafc2679b06b2ade273e909b3582654b2b3f5"
  end

  resource "RapidFuzz" do
    url "https://files.pythonhosted.org/packages/2c/21/ef6157213316e85790041254259907eb722e00b03480256c0545d98acd33/rapidfuzz-3.14.5.tar.gz"
    sha256 "ba10ac57884ce82112f7ed910b67e7fb6072d8ef2c06e30dc63c0f604a112e0e"
  end

  resource "requests" do
    url "https://files.pythonhosted.org/packages/ac/c3/e2a2b89f2d3e2179abd6d00ebd70bff6273f37fb3e0cc209f48b39d00cbf/requests-2.34.2.tar.gz"
    sha256 "f288924cae4e29463698d6d60bc6a4da69c89185ad1e0bcc4104f584e960b9ed"
  end

  resource "soupsieve" do
    url "https://files.pythonhosted.org/packages/7b/ae/2d9c981590ed9999a0d91755b47fc74f74de286b0f5cee14c9269041e6c4/soupsieve-2.8.3.tar.gz"
    sha256 "3267f1eeea4251fb42728b6dfb746edc9acaffc4a45b27e19450b676586e8349"
  end

  resource "syncedlyrics" do
    url "https://files.pythonhosted.org/packages/f1/7d/8b1d838a4c1a9fd9ed2dfd5296592e1090f935748cb3b4996e4efe531d5d/syncedlyrics-1.0.1.tar.gz"
    sha256 "3db32469ed5a6dd5d96bb4eb16df44ba749121529b462efe0eb8b3df790f66b0"
  end

  resource "typing-extensions" do
    url "https://files.pythonhosted.org/packages/72/94/1a15dd82efb362ac84269196e94cf00f187f7ed21c242792a923cdb1c61f/typing_extensions-4.15.0.tar.gz"
    sha256 "0cea48d173cc12fa28ecabc3b837ea3cf6f38c6d1136f85cbaaf598984861466"
  end

  resource "urllib3" do
    url "https://files.pythonhosted.org/packages/53/0c/06f8b233b8fd13b9e5ee11424ef85419ba0d8ba0b3138bf360be2ff56953/urllib3-2.7.0.tar.gz"
    sha256 "231e0ec3b63ceb14667c67be60f2f2c40a518cb38b03af60abc813da26505f4c"
  end
  # <<< resources

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "Karaoké", shell_output("#{bin}/karaokay --help")
  end
end
