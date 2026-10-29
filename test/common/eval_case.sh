#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 <ord|cds> <case_dir>" >&2
  exit 1
fi

FLOW_KIND="$1"
CASE_DIR="$(cd "$2" && pwd)"

DESIGN_DIR="$(dirname "${CASE_DIR}")"
PLATFORM_DIR="$(dirname "${DESIGN_DIR}")"
TEST_DIR="$(dirname "${PLATFORM_DIR}")"
REPO_ROOT="$(cd "${TEST_DIR}/.." && pwd)"

FLOW_ROOT="${REPO_ROOT}"
if [[ ! -f "${FLOW_ROOT}/env.sh" ]]; then
  echo "ERROR: env.sh not found under ${FLOW_ROOT}" >&2
  exit 1
fi

source "${FLOW_ROOT}/env.sh"

DESIGN_NICKNAME="$(basename "${DESIGN_DIR}")"
ENABLEMENT="$(basename "${PLATFORM_DIR}")"
DESIGN_CONFIG="designs/${ENABLEMENT}/${DESIGN_NICKNAME}/config.mk"

export DESIGN_DIMENSION="3D"
export DESIGN_NICKNAME

case "${FLOW_KIND}" in
  ord)
    export USE_FLOW="${USE_FLOW:-openroad}"
    case "${USE_FLOW}" in
      openroad|OpenROAD) export USE_FLOW="openroad" ;;
      *)
        echo "ERROR: USE_FLOW='${USE_FLOW}' is invalid for ord eval wrapper." >&2
        exit 1
        ;;
    esac
    export FLOW_VARIANT="${FLOW_VARIANT:-openroad}"
    TARGET="ord-final"
    ;;
  cds)
    export USE_FLOW="${USE_FLOW:-cadence}"
    case "${USE_FLOW}" in
      cadence|Cadence) export USE_FLOW="cadence" ;;
      *)
        echo "ERROR: USE_FLOW='${USE_FLOW}' is invalid for cds eval wrapper." >&2
        exit 1
        ;;
    esac
    export FLOW_VARIANT="${FLOW_VARIANT:-cadence}"
    TARGET="cds-restore"
    ;;
  *)
    echo "ERROR: unsupported flow kind '${FLOW_KIND}', expected ord or cds." >&2
    exit 1
    ;;
esac

cd "${FLOW_ROOT}"
exec make DESIGN_CONFIG="${DESIGN_CONFIG}" "${TARGET}"
