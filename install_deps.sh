#!/usr/bin/env bash
# =============================================================================
# install_deps.sh  –  Set up the Metalift development environment
#
# Usage:
#   bash install_deps.sh [--prefix <dir>] [--skip-racket] [--skip-llvm]
#                        [--skip-poetry] [--skip-llvm-pass] [--skip-bitwuzla]
#
# Can be run from anywhere; paths are resolved relative to this script's
# location (assumed to be the verge repo root).
#
# What this script does:
#   1. Resolves Python 3.10, Poetry, CVC5, CMake, and LLVM 15 locally
#      (Homebrew on macOS, apt/dnf on Linux; CVC5 always via GitHub release)
#      and prepends them onto PATH / LD_LIBRARY_PATH, installing anything
#      that isn't already present.
#   2. Downloads & installs Racket 8.12 + Rosette into --prefix (default
#      <repo-root>/deps) if not already present.
#   2.5 Downloads bitwuzla (SMT solver used by Rosette) into --prefix.
#   3. Reuses the local LLVM 15 toolchain for compiling LLVM IR (Metalift's
#      pass technically targets LLVM 11, but 15 is ABI-compatible for this).
#   4. Installs Python dependencies via Poetry (poetry install).
#   5. Builds the custom LLVM pass (llvm-pass/).
#   6. Prints a shell snippet you can paste into ~/.bashrc / ~/.zshrc.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
PREFIX="${SCRIPT_DIR}/deps"
SKIP_RACKET=0
SKIP_LLVM=0
SKIP_POETRY=0
SKIP_LLVM_PASS=0
SKIP_BITWUZLA=0

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix)      PREFIX="$2"; shift 2 ;;
    --skip-racket) SKIP_RACKET=1; shift ;;
    --skip-llvm)   SKIP_LLVM=1;   shift ;;
    --skip-poetry) SKIP_POETRY=1; shift ;;
    --skip-llvm-pass) SKIP_LLVM_PASS=1; shift ;;
    --skip-bitwuzla) SKIP_BITWUZLA=1; shift ;;
    -h|--help)
      sed -n '2,23p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

REPO_ROOT="${SCRIPT_DIR}/metalift"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info()  { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
ok()    { echo -e "\033[1;32m[ OK ]\033[0m  $*"; }
warn()  { echo -e "\033[1;33m[WARN]\033[0m  $*"; }
die()   { echo -e "\033[1;31m[ERR ]\033[0m  $*" >&2; exit 1; }

need_cmd() { command -v "$1" &>/dev/null || die "Required command not found: $1"; }

# macOS quarantines files downloaded via curl/https; strip that so the
# extracted binaries can execute without a Gatekeeper prompt.
dequarantine() {
  if [[ "${OS}" == "Darwin" ]]; then
    xattr -dr com.apple.quarantine "$1" 2>/dev/null || true
  fi
}

nproc_portable() {
  if command -v nproc &>/dev/null; then
    nproc
  else
    sysctl -n hw.ncpu 2>/dev/null || echo 4
  fi
}

# ---------------------------------------------------------------------------
# 1. Local toolchain: Python 3.10, Poetry, CVC5, CMake, LLVM 15
#    (previously a shared /grid/common/pkgs/ mount — now resolved locally)
# ---------------------------------------------------------------------------
OS="$(uname -s)"
case "$(uname -m)" in
  x86_64|amd64)  ARCH_TAG="x86_64" ;;
  arm64|aarch64) ARCH_TAG="arm64" ;;
  *) die "Unsupported architecture: $(uname -m)" ;;
esac

info "Detected ${OS}/${ARCH_TAG}. Resolving local toolchain ..."

if [[ "${OS}" == "Darwin" ]]; then
  command -v brew &>/dev/null || die "Homebrew not found. Install it from https://brew.sh and re-run."

  brew_prefix() {
    local formula="$1"
    if ! brew list --formula --versions "${formula}" &>/dev/null; then
      info "Installing ${formula} via Homebrew ..."
      brew install "${formula}"
    fi
    brew --prefix "${formula}"
  }

  PYTHON_HOME="$(brew_prefix python@3.10)"
  PYTHON_BIN="${PYTHON_HOME}/bin/python3.10"
  # Homebrew keeps the unversioned python3/pip3 symlinks in libexec/bin so
  # they don't clobber a separately-installed `python3` formula.
  PYTHON_UNVERSIONED_BIN="${PYTHON_HOME}/libexec/bin"
  CMAKE_HOME="$(brew_prefix cmake)"
  LLVM15_HOME="$(brew_prefix llvm@15)"

elif [[ "${OS}" == "Linux" ]]; then
  if command -v apt-get &>/dev/null; then
    PKG_INSTALL=(sudo apt-get install -y)
    sudo apt-get update -y
  elif command -v dnf &>/dev/null; then
    PKG_INSTALL=(sudo dnf install -y)
  else
    die "No supported package manager (apt-get/dnf) found; install Python 3.10, CMake, and LLVM 15 manually."
  fi

  command -v python3.10 &>/dev/null || "${PKG_INSTALL[@]}" python3.10 python3.10-venv python3.10-dev \
    || die "Could not install python3.10 automatically; install it manually for your distro."
  PYTHON_BIN="$(command -v python3.10)"
  PYTHON_HOME="$(dirname "$(dirname "${PYTHON_BIN}")")"
  PYTHON_UNVERSIONED_BIN=""

  command -v cmake &>/dev/null || "${PKG_INSTALL[@]}" cmake
  CMAKE_HOME="$(dirname "$(dirname "$(command -v cmake)")")"

  command -v clang-15 &>/dev/null || "${PKG_INSTALL[@]}" llvm-15 clang-15 \
    || die "Could not install LLVM 15 automatically; see https://apt.llvm.org for manual instructions."
  LLVM15_HOME="$(dirname "$(dirname "$(command -v clang-15)")")"

else
  die "Unsupported OS: ${OS}"
fi

ok "Python 3.10  -> ${PYTHON_HOME}"
ok "CMake        -> ${CMAKE_HOME}"
ok "LLVM 15      -> ${LLVM15_HOME}"

# ---- Poetry (installed the same way on every OS, pinned near grid's v2.1) -
POETRY_HOME="${PREFIX}/poetry"
POETRY_VERSION="2.1.4"
if [[ -x "${POETRY_HOME}/bin/poetry" ]]; then
  ok "Poetry already installed at ${POETRY_HOME}."
else
  info "Installing Poetry ${POETRY_VERSION} into ${POETRY_HOME} ..."
  need_cmd curl
  curl -sSL https://install.python-poetry.org \
    | POETRY_HOME="${POETRY_HOME}" POETRY_VERSION="${POETRY_VERSION}" "${PYTHON_BIN}" -
  ok "Poetry installed."
fi

# ---- CVC5 (no Homebrew/distro package; always fetched from GitHub) -------
CVC5_VERSION="1.3.1"
CVC5_HOME="${PREFIX}/cvc5"
CVC5_BIN="${CVC5_HOME}/bin/cvc5"

if [[ -x "${CVC5_BIN}" ]]; then
  ok "cvc5 already installed at ${CVC5_BIN}."
else
  case "${OS}" in
    Darwin) CVC5_OS_TAG="macOS" ;;
    Linux)  CVC5_OS_TAG="Linux" ;;
  esac
  CVC5_ASSET="cvc5-${CVC5_OS_TAG}-${ARCH_TAG}-static"
  CVC5_URL="https://github.com/cvc5/cvc5/releases/download/cvc5-${CVC5_VERSION}/${CVC5_ASSET}.zip"
  CVC5_TMP="/tmp/${CVC5_ASSET}.zip"

  info "Downloading cvc5 ${CVC5_VERSION} (${CVC5_ASSET}) ..."
  need_cmd curl
  need_cmd unzip
  curl -fsSL --retry 5 --retry-delay 3 "${CVC5_URL}" -o "${CVC5_TMP}" \
    || die "Failed to download cvc5 from ${CVC5_URL}"

  rm -rf "${CVC5_HOME}"
  mkdir -p "${CVC5_HOME}"
  unzip -q "${CVC5_TMP}" -d "${CVC5_HOME}"
  # Release zip wraps everything in a top-level "<asset-name>/" directory.
  mv "${CVC5_HOME}/${CVC5_ASSET}"/* "${CVC5_HOME}/"
  rmdir "${CVC5_HOME}/${CVC5_ASSET}"
  rm -f "${CVC5_TMP}"
  chmod +x "${CVC5_BIN}"
  dequarantine "${CVC5_HOME}"

  [[ -x "${CVC5_BIN}" ]] || die "cvc5 binary not found after extraction at ${CVC5_BIN}"
  ok "cvc5 installed at ${CVC5_BIN}."
fi

export PATH="${PYTHON_UNVERSIONED_BIN:+${PYTHON_UNVERSIONED_BIN}:}${PYTHON_HOME}/bin:${POETRY_HOME}/bin:${CVC5_HOME}/bin:${CMAKE_HOME}/bin:${LLVM15_HOME}/bin:${PATH}"

if [[ "${OS}" == "Linux" ]]; then
  # NOTE: do NOT add Python's lib/ to LD_LIBRARY_PATH – its bundled OpenSSL
  # overrides the system libssl and breaks kerberos/cmake (EVP_KDF_ctrl error).
  export LD_LIBRARY_PATH="${LLVM15_HOME}/lib:${LD_LIBRARY_PATH:-}"
fi
# macOS: no LD_LIBRARY_PATH/DYLD_LIBRARY_PATH needed — Homebrew's LLVM dylibs
# are resolved via install-name rpaths.

ok "PATH updated."
python3 --version
poetry --version
cvc5 --version | head -1
cmake --version | head -1

# ---------------------------------------------------------------------------
# 2. Racket 8.12 + Rosette
# ---------------------------------------------------------------------------
RACKET_PREFIX="${PREFIX}/racket"
RACKET_BIN="${RACKET_PREFIX}/bin/racket"

if [[ "${SKIP_RACKET}" -eq 1 ]]; then
  warn "Skipping Racket/Rosette installation (--skip-racket)."
else
  if [[ -x "${RACKET_BIN}" ]]; then
    ok "Racket already installed at ${RACKET_BIN}."
  else
    need_cmd curl
    rm -rf "${RACKET_PREFIX}"
    mkdir -p "${RACKET_PREFIX}"

    if [[ "${OS}" == "Darwin" ]]; then
      # Racket's macOS asset names use "aarch64", not "arm64".
      RACKET_ARCH_TAG="${ARCH_TAG}"
      [[ "${RACKET_ARCH_TAG}" == "arm64" ]] && RACKET_ARCH_TAG="aarch64"
      RACKET_ASSET="racket-minimal-8.12-${RACKET_ARCH_TAG}-macosx-cs"
      RACKET_URL="https://mirror.racket-lang.org/installers/8.12/${RACKET_ASSET}.tgz"
      RACKET_TMP="/tmp/${RACKET_ASSET}.tgz"

      info "Downloading Racket 8.12 (${RACKET_ASSET}) ..."
      curl -fsSL --retry 5 --retry-delay 3 "${RACKET_URL}" -o "${RACKET_TMP}" \
        || die "Failed to download Racket from ${RACKET_URL}"

      # Tarball wraps everything in a top-level "racket/" directory.
      tar -xzf "${RACKET_TMP}" -C "${RACKET_PREFIX}" --strip-components=1
      rm -f "${RACKET_TMP}"
      dequarantine "${RACKET_PREFIX}"
    else
      [[ "${ARCH_TAG}" == "x86_64" ]] \
        || die "Racket 8.12 has no official Linux ${ARCH_TAG} build; install Racket manually or pass --skip-racket."

      RACKET_INSTALLER_URL="https://mirror.racket-lang.org/installers/8.12/racket-8.12-x86_64-linux-cs.sh"
      RACKET_INSTALLER="/tmp/racket-8.12-installer.sh"

      curl -fsSL --retry 5 --retry-delay 3 \
        "${RACKET_INSTALLER_URL}" -o "${RACKET_INSTALLER}"
      chmod +x "${RACKET_INSTALLER}"

      info "Installing Racket into ${RACKET_PREFIX} ..."
      bash "${RACKET_INSTALLER}" --in-place --dest "${RACKET_PREFIX}"
      rm -f "${RACKET_INSTALLER}"
    fi

    ok "Racket installed."
  fi

  # Always ensure Rosette is installed (idempotent)
  export PATH="${RACKET_PREFIX}/bin:${PATH}"
  RACO="${RACKET_PREFIX}/bin/raco"

  info "Checking Rosette installation ..."
  if "${RACO}" pkg show rosette 2>&1 | grep -q "rosette"; then
    ok "Rosette already installed."
  else
    info "Installing Rosette via raco pkg install (this may take a few minutes) ..."
    "${RACO}" pkg install --auto --batch rosette \
      || die "Rosette install failed. Try manually: ${RACO} pkg install --auto rosette"
    ok "Rosette installed."
  fi
fi

# ---------------------------------------------------------------------------
# 2.5 Bitwuzla SMT solver (used by Rosette as its backend solver)
# ---------------------------------------------------------------------------
BITWUZLA_VERSION="0.9.1"
BITWUZLA_DIR="${PREFIX}/bitwuzla"
BITWUZLA_BIN="${BITWUZLA_DIR}/bin/bitwuzla"

if [[ "${SKIP_BITWUZLA}" -eq 1 ]]; then
  warn "Skipping bitwuzla installation (--skip-bitwuzla)."
elif [[ -x "${BITWUZLA_BIN}" ]]; then
  ok "bitwuzla already installed at ${BITWUZLA_BIN}."
else
  case "${OS}" in
    Darwin) BITWUZLA_OS_TAG="macOS" ;;
    Linux)  BITWUZLA_OS_TAG="Linux" ;;
  esac
  info "Downloading bitwuzla ${BITWUZLA_VERSION} for ${BITWUZLA_OS_TAG} ${ARCH_TAG} ..."
  # Release asset naming (no version in the filename): Bitwuzla-<OS>-<arch>-static.zip
  BITWUZLA_ASSET="Bitwuzla-${BITWUZLA_OS_TAG}-${ARCH_TAG}-static"
  BITWUZLA_URL="https://github.com/bitwuzla/bitwuzla/releases/download/${BITWUZLA_VERSION}/${BITWUZLA_ASSET}.zip"
  BITWUZLA_TMP="/tmp/bitwuzla-${BITWUZLA_VERSION}.zip"

  need_cmd curl
  need_cmd unzip
  curl -fsSL --retry 5 --retry-delay 3 \
    "${BITWUZLA_URL}" -o "${BITWUZLA_TMP}" \
    || die "Failed to download bitwuzla from ${BITWUZLA_URL}"

  rm -rf "${BITWUZLA_DIR}"
  mkdir -p "${BITWUZLA_DIR}/bin"
  unzip -q "${BITWUZLA_TMP}" -d "${BITWUZLA_DIR}/bin"
  # Move the actual binary up and clean up the extracted subdir
  mv "${BITWUZLA_DIR}/bin/${BITWUZLA_ASSET}/bin/bitwuzla" "${BITWUZLA_BIN}"
  rm -rf "${BITWUZLA_DIR}/bin/${BITWUZLA_ASSET}" "${BITWUZLA_TMP}"
  chmod +x "${BITWUZLA_BIN}"
  dequarantine "${BITWUZLA_DIR}"

  [[ -x "${BITWUZLA_BIN}" ]] || die "bitwuzla binary not found after extraction at ${BITWUZLA_BIN}"
  ok "bitwuzla installed at ${BITWUZLA_BIN}."
fi

export BITWUZLA_PATH="${BITWUZLA_BIN}"

# Rosette's own `bitwuzla` binding ignores BITWUZLA_PATH entirely -- it only
# looks for the binary inside its own package directory
# (<pkgs>/rosette/bin/bitwuzla), and fails instantly if it's not there. When
# that happens, metalift's Python wrapper (which pipes racket's stdout/stderr)
# doesn't surface the crash -- it just retries silently forever, looking
# exactly like a hang. Symlink it into place so Rosette actually finds it.
if [[ "${SKIP_RACKET}" -ne 1 && "${SKIP_BITWUZLA}" -ne 1 && -x "${RACKET_PREFIX}/bin/racket" && -x "${BITWUZLA_BIN}" ]]; then
  ROSETTE_COLLECTION_FILE="$("${RACKET_PREFIX}/bin/racket" -e '(displayln (path->string (collection-file-path "rosette" "rosette")))' 2>/dev/null || true)"
  if [[ -n "${ROSETTE_COLLECTION_FILE}" ]]; then
    ROSETTE_PKG_DIR="$(dirname "$(dirname "${ROSETTE_COLLECTION_FILE}")")"
    ROSETTE_BIN_DIR="${ROSETTE_PKG_DIR}/bin"
    mkdir -p "${ROSETTE_BIN_DIR}"
    ln -sf "${BITWUZLA_BIN}" "${ROSETTE_BIN_DIR}/bitwuzla"
    ok "Linked bitwuzla into Rosette's package dir: ${ROSETTE_BIN_DIR}/bitwuzla"
  else
    warn "Could not locate Rosette's package directory to link bitwuzla; Rosette verification may fail silently."
  fi
fi

# ---------------------------------------------------------------------------
# 3. LLVM for compilation (reuse the local LLVM 15 toolchain from step 1)
#    Metalift's pass technically targets LLVM 11, but linking against 15
#    avoids managing a second, separately-versioned LLVM install.
# ---------------------------------------------------------------------------
LLVM11_PREFIX="${LLVM15_HOME}"   # alias: reuse local LLVM 15
LLVM11_BIN="${LLVM15_HOME}/bin/clang"

if [[ "${SKIP_LLVM}" -eq 1 ]]; then
  warn "Skipping LLVM check (--skip-llvm)."
else
  ok "Using local LLVM 15 at ${LLVM15_HOME}."
fi

# ---------------------------------------------------------------------------
# 3.5 Apply local patches to the metalift and levi submodules
#     (compatibility fixes that haven't landed upstream)
# ---------------------------------------------------------------------------
apply_patches_to() {
  local target_dir="$1"
  local target_label="$2"
  local pattern="$3"
  [[ -d "${target_dir}" ]] || return 0
  shopt -s nullglob
  for patch_file in ${pattern}; do
    if git -C "${target_dir}" apply --check "${patch_file}" 2>/dev/null; then
      info "Applying patch $(basename "${patch_file}") to ${target_label} ..."
      git -C "${target_dir}" apply "${patch_file}"
      ok "Patch $(basename "${patch_file}") applied."
    elif git -C "${target_dir}" apply --reverse --check "${patch_file}" 2>/dev/null; then
      ok "Patch $(basename "${patch_file}") already applied."
    else
      warn "Patch $(basename "${patch_file}") doesn't apply cleanly (${target_label} may have changed upstream) — skipping."
    fi
  done
  shopt -u nullglob
}

if [[ -d "${SCRIPT_DIR}/patches" ]]; then
  # Naming convention: metalift-*.patch targets the metalift submodule,
  # levi-*.patch targets levi. A patch's own diff paths are relative to
  # its target's repo root regardless of prefix -- the prefix only
  # decides which submodule `git apply` runs against.
  apply_patches_to "${REPO_ROOT}" "metalift" "${SCRIPT_DIR}/patches/metalift-*.patch"
  apply_patches_to "${SCRIPT_DIR}/levi" "levi" "${SCRIPT_DIR}/patches/levi-*.patch"
fi

# ---------------------------------------------------------------------------
# 4. Python dependencies via Poetry
# ---------------------------------------------------------------------------
if [[ "${SKIP_POETRY}" -eq 1 ]]; then
  warn "Skipping poetry install (--skip-poetry)."
else
  info "Installing Python dependencies with Poetry ..."
  cd "${REPO_ROOT}"

  # Disable keyring to avoid DBus/org.freedesktop.secrets timeout on headless systems
  export PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring

  # Redirect Poetry cache + data dirs away from home (~/.cache, ~/.local)
  export POETRY_CACHE_DIR="${PREFIX}/poetry-cache"
  export POETRY_DATA_DIR="${PREFIX}/poetry-data"
  export POETRY_CONFIG_DIR="${PREFIX}/poetry-config"
  mkdir -p "${POETRY_CACHE_DIR}" "${POETRY_DATA_DIR}" "${POETRY_CONFIG_DIR}"

  # Tell Poetry to use the local Python 3.10 (realpath to dodge symlink issues)
  poetry env use "$(realpath "${PYTHON_BIN}")"
  poetry install --no-interaction

  # llmlift_scripts/ (used by the benchmarks/blend and benchmarks/llama
  # drivers, and by our own verge_prompt_bridge.py) imports `anthropic` and
  # `google.generativeai` at module load time, but metalift's pyproject.toml
  # doesn't declare either as a dependency. Patch that gap here rather than
  # depending on it having been fixed upstream.
  poetry run pip install anthropic google-generativeai

  ok "Python dependencies installed."
fi

# ---------------------------------------------------------------------------
# 5. Build the custom LLVM pass
# ---------------------------------------------------------------------------
if [[ "${SKIP_LLVM_PASS}" -eq 1 ]]; then
  warn "Skipping LLVM pass build (--skip-llvm-pass)."
else
  info "Building custom LLVM pass ..."
  cd "${REPO_ROOT}/llvm-pass"
  mkdir -p build
  cd build

  # Point CMake at the local LLVM 15
  LLVM_CMAKE_DIR="${LLVM15_HOME}/lib/cmake/llvm"

  if [[ -d "${LLVM_CMAKE_DIR}" ]]; then
    cmake .. -DLLVM_DIR="${LLVM_CMAKE_DIR}"
  else
    warn "LLVM cmake dir not found at ${LLVM_CMAKE_DIR}; trying without ..."
    cmake ..
  fi

  make -j"$(nproc_portable)"
  cd "${REPO_ROOT}"
  ok "LLVM pass built: llvm-pass/build/addEmptyBlocks/libAddEmptyBlocksPass.so"
fi

# ---------------------------------------------------------------------------
# 6. Print environment snippet
# ---------------------------------------------------------------------------
SHELL_RC="~/.bashrc or ~/.bash_profile"
[[ "${OS}" == "Darwin" ]] && SHELL_RC="~/.zshrc"

cat <<EOF

=============================================================================
  Setup complete!  Add the following to your ${SHELL_RC}:
=============================================================================

# --- Metalift environment ---
export PATH="${PYTHON_UNVERSIONED_BIN:+${PYTHON_UNVERSIONED_BIN}:}${PYTHON_HOME}/bin:${POETRY_HOME}/bin:${CVC5_HOME}/bin:${CMAKE_HOME}/bin:${LLVM15_HOME}/bin:\${PATH}"
EOF

if [[ "${OS}" == "Linux" ]]; then
  echo "export LD_LIBRARY_PATH=\"${LLVM15_HOME}/lib:\${LD_LIBRARY_PATH:-}\""
fi

cat <<EOF
export PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring
export POETRY_CACHE_DIR="${PREFIX}/poetry-cache"
export POETRY_DATA_DIR="${PREFIX}/poetry-data"
export POETRY_CONFIG_DIR="${PREFIX}/poetry-config"
export BITWUZLA_PATH="${PREFIX}/bitwuzla/bin/bitwuzla"
EOF

if [[ -x "${RACKET_BIN}" ]]; then
  echo "export PATH=\"${RACKET_PREFIX}/bin:\${PATH}\""
fi

cat <<EOF

# Activate the virtualenv (Poetry 2.x removed 'poetry shell'):
#   source ${REPO_ROOT}/.venv/bin/activate
# Or run a single command without activating:
#   poetry run python <script.py>
=============================================================================
EOF
