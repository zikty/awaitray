# Homebrew formula for awaitray (macOS menu-bar app). Lives in the tap
# zikty/homebrew-awaitray; the release workflow updates url and sha256.
#
#   brew install zikty/awaitray/awaitray
#   brew services start awaitray
class Awaitray < Formula
  include Language::Python::Virtualenv

  desc "Menu-bar app for GitHub pull requests awaiting your review"
  homepage "https://github.com/zikty/awaitray"
  url "https://github.com/zikty/awaitray/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  license "MIT"

  depends_on "gh"
  depends_on :macos
  depends_on "python@3.12"

  resource "pyobjc-core" do
    url "https://files.pythonhosted.org/packages/a5/78/abc4ce5920305780aeb36b4067a86253378b36e29ba96673a3deb02eb03a/pyobjc_core-12.2.2.tar.gz"
    sha256 "3906452339cd06a3bb07df103c2511d4cb0f7a22d8771c0b802eba15d9a642b6"
  end

  resource "pyobjc-framework-Cocoa" do
    url "https://files.pythonhosted.org/packages/75/76/49c6da2c6a831020b4854ba20079d5a1030474bffc776b7b73c2eeff8c15/pyobjc_framework_cocoa-12.2.2.tar.gz"
    sha256 "c96c0ef69a71afbbb0e6a7d594b455c5fe47d62e0db376ee7a2b4b828c16ace9"
  end

  def install
    virtualenv_install_with_resources
  end

  service do
    run [opt_bin/"awaitray"]
    keep_alive successful_exit: false
    process_type :interactive
    log_path var/"log/awaitray.log"
    error_log_path var/"log/awaitray.log"
  end

  def caveats
    <<~EOS
      Log in to GitHub once with:
        gh auth login
      Start awaitray now and at login with:
        brew services start awaitray
    EOS
  end

  test do
    assert_match "settings:", shell_output("#{bin}/awaitray config path")
  end
end
