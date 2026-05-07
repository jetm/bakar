#!/usr/bin/env bash
# VARIS-18 patch-ablation matrix runner.
#
# Drives 10 cells (5 patch configs A-E x 2 Python columns) at n=20 each,
# tagging every summary.json with `blog-py<ver>-<config>` so
# scripts/blog-ablation-table.py can aggregate the run group.
#
# Configs:
#   A. Stock baseline                  -> ablation/baseline,        PYTHONMALLOC=default
#   B. gc.freeze only                  -> ablation/gc-freeze-only,  PYTHONMALLOC=default
#   C. PYTHONMALLOC only               -> ablation/baseline,        PYTHONMALLOC=malloc
#   D. Minimal (gc.freeze + malloc)    -> ablation/gc-freeze-only,  PYTHONMALLOC=malloc
#   E. Full stack                      -> br-2.12,                  PYTHONMALLOC=malloc
#
# Python columns:
#   py3.12kas - kas-container's bundled 3.12.10 (production)
#   py3.15b1  - host-mode 3.15.0b1 (newest CPython data)
#
# Each cell reuses --runs N from --runs-per-cell (default 20). Pass
# --runs-per-cell 2 for a quick smoke before the full matrix.
#
# Usage:
#     scripts/run-ablation-matrix.sh                       # full matrix, n=20 per cell
#     scripts/run-ablation-matrix.sh --runs-per-cell 5     # smaller sample, faster turnaround
#     scripts/run-ablation-matrix.sh --only-kas            # skip 3.15b1 column
#     scripts/run-ablation-matrix.sh --only-host           # skip 3.12kas column
#     scripts/run-ablation-matrix.sh --configs A,D,E       # subset of configs
#
# Aggregation after the run:
#     varis/scripts/blog-ablation-table.py
set -euo pipefail

BSPCTL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BITBAKE_REPO="${HOME}/repos/personal/yocto/bitbake"
PY315B1="${HOME}/.local/share/uv/python/cpython-3.15.0b1-linux-x86_64-gnu/bin/python3"

RUNS_PER_CELL=20
ONLY_KAS=0
ONLY_HOST=0
CONFIGS="A,B,C,D,E"
MANIFEST="imx-6.12.49-2.2.0.xml"
MACHINE="imx95-var-dart"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --runs-per-cell) RUNS_PER_CELL="$2"; shift 2 ;;
        --only-kas)      ONLY_KAS=1; shift ;;
        --only-host)     ONLY_HOST=1; shift ;;
        --configs)       CONFIGS="$2"; shift 2 ;;
        -h|--help)       sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

# Sanity checks before any runs.
[[ -d "${BITBAKE_REPO}" ]] || { echo "missing bitbake repo at ${BITBAKE_REPO}" >&2; exit 2; }
[[ -x "${PY315B1}" ]] || { echo "missing Python 3.15.0b1 at ${PY315B1}" >&2; exit 2; }
[[ -x "${BSPCTL_DIR}/.venv-test/bin/bspctl" ]] || { echo "missing .venv-test/bin/bspctl" >&2; exit 2; }

git -C "${BITBAKE_REPO}" rev-parse ablation/baseline >/dev/null \
    || { echo "missing branch ablation/baseline in ${BITBAKE_REPO}" >&2; exit 2; }
git -C "${BITBAKE_REPO}" rev-parse ablation/gc-freeze-only >/dev/null \
    || { echo "missing branch ablation/gc-freeze-only in ${BITBAKE_REPO}" >&2; exit 2; }
git -C "${BITBAKE_REPO}" rev-parse br-2.12 >/dev/null \
    || { echo "missing branch br-2.12 in ${BITBAKE_REPO}" >&2; exit 2; }

# ---------------------------------------------------------------------------
# Cell definitions
# ---------------------------------------------------------------------------
# Each cell: <config-letter>:<bitbake-branch>:<pythonmalloc-mode>
# pythonmalloc-mode is "default" (CPython pymalloc) or "malloc" (libc malloc).
#
# The label format is `blog-py<col>-<config>` so the aggregator can group.
# `<col>` is the Python column id; `<config>` is A|B|C|D|E.
declare -A CELL_BRANCH=(
    [A]=ablation/baseline
    [B]=ablation/gc-freeze-only
    [C]=ablation/baseline
    [D]=ablation/gc-freeze-only
    [E]=br-2.12
)
declare -A CELL_MALLOC=(
    [A]=default
    [B]=default
    [C]=malloc
    [D]=malloc
    [E]=malloc
)

run_cell() {
    local col="$1"            # py3.12kas | py3.15b1
    local config="$2"         # A | B | C | D | E
    local host_mode="$3"      # 0 | 1
    local label="blog-py${col#py}-${config}"
    local branch="${CELL_BRANCH[$config]}"
    local malloc="${CELL_MALLOC[$config]}"

    echo
    echo "========================================================================"
    echo "Cell ${label}: branch=${branch}  pythonmalloc=${malloc}  host=${host_mode}"
    echo "========================================================================"

    # The override mechanism does git-checkout itself; we just pin the target
    # branch via env so it picks our ablation branch instead of auto-deriving.
    local cmd_env=(
        env
        "VARIS_BITBAKE_OVERRIDE_BRANCH=${branch}"
        "PYTHONMALLOC=${malloc}"
    )

    local varis_args=(
        stress-parse
        -f "${MANIFEST}"
        -m "${MACHINE}"
        --runs "${RUNS_PER_CELL}"
        --label "${label}"
    )
    if [[ "${host_mode}" == "1" ]]; then
        varis_args+=(--host)
    fi

    if [[ "${col}" == "py3.15b1" ]]; then
        # Host mode + Python 3.15.0b1: run varis itself under 3.15.0b1
        # so sys.executable forwards into BB_PYTHON3 inside `kas shell`.
        # uv run installs varis editable into a 3.15.0b1 ephemeral env.
        "${cmd_env[@]}" \
            uv run --python "${PY315B1}" --with-editable "${BSPCTL_DIR}" \
                varis "${varis_args[@]}" || {
            echo "CELL ${label} FAILED (rc=$?)" >&2
        }
    else
        # kas-container mode: varis runs under whatever its venv carries
        # (3.14.4); kas-container's bundled 3.12.10 runs bitbake.
        "${cmd_env[@]}" \
            "${BSPCTL_DIR}/.venv-test/bin/bspctl" "${varis_args[@]}" || {
            echo "CELL ${label} FAILED (rc=$?)" >&2
        }
    fi
}

# Iterate the configured cells.
IFS=',' read -ra config_list <<< "${CONFIGS}"
for config in "${config_list[@]}"; do
    [[ -n "${CELL_BRANCH[$config]:-}" ]] || {
        echo "unknown config: ${config}" >&2; exit 2
    }
done

if [[ "${ONLY_HOST}" == "0" ]]; then
    echo "=========================="
    echo "Column 1: kas-container Python 3.12.10"
    echo "=========================="
    for config in "${config_list[@]}"; do
        run_cell py3.12kas "${config}" 0
    done
fi

if [[ "${ONLY_KAS}" == "0" ]]; then
    echo "=========================="
    echo "Column 2: host-mode Python 3.15.0b1"
    echo "=========================="
    for config in "${config_list[@]}"; do
        run_cell py3.15b1 "${config}" 1
    done
fi

echo
echo "Matrix complete. Aggregate with:"
echo "    ${BSPCTL_DIR}/.venv-test/bin/python ${BSPCTL_DIR}/scripts/blog-ablation-table.py"
