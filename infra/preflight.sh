#!/usr/bin/env bash
# Checks machine resources and required tooling for incident-agent.
# Never fails loudly with a stack trace: always prints a clear, actionable status.
set -uo pipefail

SSD_PATH="${INCIDENT_AGENT_SSD:-/Volumes/JEX2L}"
MISSING=()
WARN=()

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
ok()   { printf "  \033[32m[ok]\033[0m   %s\n" "$1"; }
bad()  { printf "  \033[31m[missing]\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m[warn]\033[0m %s\n" "$1"; }

bold "== incident-agent preflight =="

echo
bold "Hardware"
if [[ "$(uname)" == "Darwin" ]]; then
  CHIP=$(sysctl -n machdep.cpu.brand_string 2>/dev/null)
  CORES=$(sysctl -n hw.ncpu)
  RAM_GB=$(( $(sysctl -n hw.memsize) / 1024 / 1024 / 1024 ))
  echo "  CPU: ${CHIP} (${CORES} cores)"
  echo "  RAM: ${RAM_GB} GB"
  if [[ $RAM_GB -lt 8 ]]; then
    warn "Less than 8GB RAM — use a small model (qwen2.5:1.5b) and keep Helm resource requests minimal."
  elif [[ $RAM_GB -lt 16 ]]; then
    warn "Under 16GB RAM — consider qwen2.5:3b instead of the 7b default."
  fi
fi

AVAIL_GB=$(df -g / 2>/dev/null | tail -1 | awk '{print $4}')
echo "  Free disk on /: ${AVAIL_GB:-unknown} GB"
if [[ -n "${AVAIL_GB:-}" && "$AVAIL_GB" -lt 25 ]]; then
  warn "Internal disk has < 25GB free. Point Docker Desktop's VM disk image and OLLAMA_MODELS at ${SSD_PATH} (see README)."
fi
if [[ -d "$SSD_PATH" ]]; then
  SSD_AVAIL=$(df -g "$SSD_PATH" 2>/dev/null | tail -1 | awk '{print $4}')
  echo "  Free disk on ${SSD_PATH}: ${SSD_AVAIL:-unknown} GB"
fi

echo
bold "Required tools"
check() {
  local name="$1" cmd="$2"
  if command -v "$cmd" >/dev/null 2>&1; then
    ok "$name ($(command -v "$cmd"))"
    return 0
  else
    bad "$name"
    MISSING+=("$name")
    return 1
  fi
}

check "docker" docker
if command -v docker >/dev/null 2>&1; then
  if ! docker info >/dev/null 2>&1; then
    bad "docker daemon not running (Docker Desktop must be started)"
    MISSING+=("docker-daemon")
  fi
fi
check "kind" kind
check "kubectl" kubectl
check "helm" helm
check "ollama" ollama
if command -v ollama >/dev/null 2>&1; then
  if ! ollama list >/dev/null 2>&1; then
    warn "ollama CLI found but the ollama server doesn't seem to be running (run 'ollama serve' or open the app)."
  fi
fi
check "python3" python3
check "uv" uv
check "git" git

echo
if [[ ${#MISSING[@]} -gt 0 ]]; then
  bold "Missing prerequisites — install these before 'make up' can bring up the cluster:"
  for m in "${MISSING[@]}"; do
    case "$m" in
      docker) echo "  brew install --cask docker   # then launch Docker Desktop once" ;;
      docker-daemon) echo "  Launch Docker Desktop and wait for it to say 'running'." ;;
      kind) echo "  brew install kind" ;;
      kubectl) echo "  brew install kubectl" ;;
      helm) echo "  brew install helm" ;;
      ollama) echo "  brew install ollama   # or https://ollama.com/download" ;;
      uv) echo "  brew install uv   # or: curl -LsSf https://astral.sh/uv/install.sh | sh" ;;
      python3) echo "  brew install python@3.12" ;;
      git) echo "  brew install git" ;;
    esac
  done
  echo
  echo "Tip: to keep Docker/kind image data off your internal disk, set Docker Desktop's"
  echo "  Settings > Resources > Advanced > Disk image location to: ${SSD_PATH}/docker-data"
  echo "and export OLLAMA_MODELS=${SSD_PATH}/ollama-models before pulling models."
  exit 1
else
  bold "All prerequisites present."
  exit 0
fi
