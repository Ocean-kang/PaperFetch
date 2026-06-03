cat > /root/code/PaperFetch/run.sh <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="/root/code/PaperFetch"
CONDA_SH="/root/miniconda3/etc/profile.d/conda.sh"
CONDA_ENV="paperfetch"
PY="/root/miniconda3/envs/paperfetch/bin/python"
SCRIPT="$ROOT/PaperFrech_daily_keyword.py"
LOG_DIR="$ROOT/log"
LOG_FILE="$LOG_DIR/run.log"

mkdir -p "$LOG_DIR"
cd "$ROOT"

# 从这里开始，所有 shell 输出和 Python 输出都进入 run.log
exec >> "$LOG_FILE" 2>&1

echo
echo "===== PaperFetch START $(date '+%F %T %z') ====="
echo "PWD=$(pwd)"
echo "USER=$(id -un)"
echo "PATH=$PATH"

trap 'code=$?; echo "===== PaperFetch ERROR $(date "+%F %T %z") ====="; echo "line=$LINENO"; echo "cmd=$BASH_COMMAND"; echo "exit=$code"; exit $code' ERR

if [[ ! -f "$CONDA_SH" ]]; then
    echo "ERROR: conda.sh not found: $CONDA_SH"
    exit 10
fi

source "$CONDA_SH"
conda activate "$CONDA_ENV"

echo "Conda env: $CONDA_DEFAULT_ENV"
echo "Python from command: $(command -v python)"
echo "Python explicit: $PY"
"$PY" --version

if [[ ! -x "$PY" ]]; then
    echo "ERROR: python not executable: $PY"
    exit 11
fi

if [[ ! -f "$SCRIPT" ]]; then
    echo "ERROR: script not found: $SCRIPT"
    echo "Current directory files:"
    ls -lah "$ROOT"
    exit 12
fi

echo "Running script: $SCRIPT"

set +e
timeout 600 "$PY" "$SCRIPT"
code=$?
set -e

if [[ "$code" -eq 124 ]]; then
    echo "ERROR: script timeout after 600 seconds"
    exit 124
elif [[ "$code" -ne 0 ]]; then
    echo "ERROR: python script failed, exit=$code"
    exit "$code"
fi

echo "===== PaperFetch END $(date '+%F %T %z') exit=0 ====="
EOF

chmod +x /root/code/PaperFetch/run.sh