#!/usr/bin/env bash
# =============================================================================
# install_deps.sh  –  Set up the Metalift development environment
#
# Usage:
#   bash install_deps.sh [--prefix <dir>] [--skip-racket] [--skip-llvm]
#                        [--skip-poetry] [--skip-llvm-pass] [--skip-bitwuzla]
#
# Run from the verge root directory.
#
# What this script does:
#   1. Prepends site packages (Python 3.10, Poetry, CVC5, CMake, LLVM 15)
#      from /grid/common/pkgs/ onto PATH / LD_LIBRARY_PATH.
#   2. Downloads & installs Racket 8.12 + Rosette into --prefix (default
#      /lan/csv/orion_t1_v1/ihchang/verge/deps) if not already present.
#   2.5 Downloads bitwuzla (SMT solver used by Rosette) into --prefix.
#   3. Downloads a pre-built LLVM 11 clang/opt into --prefix for compiling
#      LLVM IR (Metalift currently requires LLVM 11 for the pass).
#   4. Installs Python dependencies via Poetry (poetry install).
#   5. Builds the custom LLVM pass (llvm-pass/).
#   6. Prints a shell snippet you can paste into ~/.bashrc / ~/.bash_profile.
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
PREFIX="/lan/csv/orion_t1_v1/ihchang/verge/deps"
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
      sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

REPO_ROOT="/lan/csv/orion_t1_v1/ihchang/verge/metalift"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info()  { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
ok()    { echo -e "\033[1;32m[ OK ]\033[0m  $*"; }
warn()  { echo -e "\033[1;33m[WARN]\033[0m  $*"; }
die()   { echo -e "\033[1;31m[ERR ]\033[0m  $*" >&2; exit 1; }

need_cmd() { command -v "$1" &>/dev/null || die "Required command not found: $1"; }

# ---------------------------------------------------------------------------
# 1. Site packages from /grid/common/pkgs/
# ---------------------------------------------------------------------------
GRID_PKGS=/grid/common/pkgs

info "Prepending site packages from ${GRID_PKGS} ..."

# Python 3.10 (vanilla build – clean ssl/venv)
PYTHON_HOME="${GRID_PKGS}/python/v3.10.8"
[[ -d "${PYTHON_HOME}/bin" ]] || die "Python 3.10 not found at ${PYTHON_HOME}"

# Poetry
POETRY_HOME="${GRID_PKGS}/poetry/v2.1"
[[ -d "${POETRY_HOME}/bin" ]] || die "Poetry not found at ${POETRY_HOME}"

# CVC5
CVC5_HOME="${GRID_PKGS}/cvc5/v1.3.1"
[[ -d "${CVC5_HOME}/bin" ]] || die "CVC5 not found at ${CVC5_HOME}"

# CMake (needed to build the LLVM pass)
CMAKE_HOME="${GRID_PKGS}/cmake/v3.30"
[[ -d "${CMAKE_HOME}/bin" ]] || CMAKE_HOME="${GRID_PKGS}/cmake/v3.28.2"
[[ -d "${CMAKE_HOME}/bin" ]] || CMAKE_HOME="${GRID_PKGS}/cmake/latest"
[[ -d "${CMAKE_HOME}/bin" ]] || die "CMake not found under ${GRID_PKGS}/cmake"

# LLVM 15 (Cadence build – glibc-compatible with RHEL 8)
LLVM15_HOME="${GRID_PKGS}/llvm/ps2025"
[[ -d "${LLVM15_HOME}/bin" ]] || die "LLVM 15 not found at ${LLVM15_HOME}"

export PATH="${PYTHON_HOME}/bin:${POETRY_HOME}/bin:${CVC5_HOME}/bin:${CMAKE_HOME}/bin:${LLVM15_HOME}/bin:${PATH}"
# NOTE: do NOT add Python's lib/ to LD_LIBRARY_PATH – its bundled OpenSSL
# overrides the system libssl and breaks kerberos/cmake (EVP_KDF_ctrl error).
export LD_LIBRARY_PATH="${LLVM15_HOME}/lib:${LD_LIBRARY_PATH:-}"

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
    info "Downloading Racket 8.12 installer ..."
    RACKET_INSTALLER_URL="https://mirror.racket-lang.org/installers/8.12/racket-8.12-x86_64-linux-cs.sh"
    RACKET_INSTALLER="/tmp/racket-8.12-installer.sh"

    need_cmd curl
    curl -fsSL --retry 5 --retry-delay 3 \
      "${RACKET_INSTALLER_URL}" -o "${RACKET_INSTALLER}"
    chmod +x "${RACKET_INSTALLER}"

    info "Installing Racket into ${RACKET_PREFIX} ..."
    # Remove any partial/empty dir so the installer doesn't prompt "delete?"
    rm -rf "${RACKET_PREFIX}"
    mkdir -p "$(dirname "${RACKET_PREFIX}")"
    # Use non-interactive in-place install flags
    bash "${RACKET_INSTALLER}" --in-place --dest "${RACKET_PREFIX}"

    ok "Racket installed."
    rm -f "${RACKET_INSTALLER}"
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
  info "Downloading bitwuzla ${BITWUZLA_VERSION} for Linux x86_64 ..."
  # Release asset: Bitwuzla-<ver>-Linux-x86_64-static.zip
  BITWUZLA_ASSET="Bitwuzla-${BITWUZLA_VERSION}-Linux-x86_64-static"
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

  [[ -x "${BITWUZLA_BIN}" ]] || die "bitwuzla binary not found after extraction at ${BITWUZLA_BIN}"
  ok "bitwuzla installed at ${BITWUZLA_BIN}."
fi

export BITWUZLA_PATH="${BITWUZLA_BIN}"

# ---------------------------------------------------------------------------
# 3. LLVM for compilation (use grid LLVM 15 – glibc-compatible with RHEL 8)
#    The downloaded LLVM 11 Ubuntu prebuilts require glibc 2.32 which RHEL 8
#    does not have. LLVM 15 from /grid/common/pkgs/ is built for this system.
# ---------------------------------------------------------------------------
LLVM11_PREFIX="${LLVM15_HOME}"   # alias: reuse LLVM 15 from grid
LLVM11_BIN="${LLVM15_HOME}/bin/clang"

if [[ "${SKIP_LLVM}" -eq 1 ]]; then
  warn "Skipping LLVM check (--skip-llvm)."
else
  ok "Using grid LLVM 15 at ${LLVM15_HOME} (glibc-compatible with RHEL 8)."
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

  # Tell Poetry to use the site-packages Python 3.10 (use realpath to avoid symlink issues)
  PYTHON3_BIN="$(realpath "${PYTHON_HOME}/bin/python3")"
  poetry env use "${PYTHON3_BIN}"
  poetry install --no-interaction

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

  # Point CMake at grid LLVM 15 (glibc-compatible)
  LLVM_CMAKE_DIR="${LLVM15_HOME}/lib/cmake/llvm"

  if [[ -d "${LLVM_CMAKE_DIR}" ]]; then
    cmake .. -DLLVM_DIR="${LLVM_CMAKE_DIR}"
  else
    warn "LLVM cmake dir not found at ${LLVM_CMAKE_DIR}; trying without ..."
    cmake ..
  fi

  make -j"$(nproc)"
  cd "${REPO_ROOT}"
  ok "LLVM pass built: llvm-pass/build/addEmptyBlocks/libAddEmptyBlocksPass.so"
fi

# ---------------------------------------------------------------------------
# 6. Print environment snippet
# ---------------------------------------------------------------------------
cat <<EOF

=============================================================================
  Setup complete!  Add the following to your ~/.bashrc or ~/.bash_profile:
=============================================================================

# --- Metalift environment ---
export PATH="${PYTHON_HOME}/bin:${POETRY_HOME}/bin:${CVC5_HOME}/bin:${CMAKE_HOME}/bin:${LLVM15_HOME}/bin:\${PATH}"
export LD_LIBRARY_PATH="${LLVM15_HOME}/lib:\${LD_LIBRARY_PATH:-}"
export PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring
export POETRY_CACHE_DIR="${PREFIX}/poetry-cache"
export POETRY_DATA_DIR="${PREFIX}/poetry-data"
export POETRY_CONFIG_DIR="${PREFIX}/poetry-config"
export BITWUZLA_PATH="${PREFIX}/bitwuzla/bin/bitwuzla"
EOF

if [[ -x "${RACKET_BIN}" ]]; then
  echo "export PATH=\"${RACKET_PREFIX}/bin:\${PATH}\""
fi

cat <<'EOF'

# Activate the virtualenv (Poetry 2.x removed 'poetry shell'):
#   source /lan/csv/orion_t1_v1/ihchang/verge/metalift/.venv/bin/activate
# Or run a single command without activating:
#   poetry run python <script.py>
=============================================================================
EOF
