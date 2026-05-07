#!/usr/bin/env bash

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_bootstrap_common.sh"

mkdir -p "${BIN_DIR}"
cat > "${PSQL_WRAPPER}" <<EOF
#!/usr/bin/env bash
set -euo pipefail
container="\${MPBOOT_PG_CONTAINER:-${POSTGRES_CONTAINER}}"
args=()
skip_next=0
file_arg=""
for arg in "\$@"; do
  if [[ "\${skip_next}" -eq 1 ]]; then
    if [[ -n "\${expect_file_arg:-}" ]]; then
      file_arg="\${arg}"
      expect_file_arg=""
    fi
    skip_next=0
    continue
  fi
  case "\${arg}" in
    -h|--host|-p|--port)
      skip_next=1
      ;;
    -h*|-p*)
      ;;
    -f)
      skip_next=1
      expect_file_arg=1
      ;;
    -f*)
      file_arg="\${arg#-f}"
      ;;
    --host=*|--port=*)
      ;;
    --file=*)
      file_arg="\${arg#--file=}"
      ;;
    *)
      args+=("\${arg}")
      ;;
  esac
done
if [[ -n "\${file_arg}" ]]; then
  exec docker exec -i "\${container}" psql "\${args[@]}" < "\${file_arg}"
fi
exec docker exec -i "\${container}" psql "\${args[@]}"
EOF
chmod u+x "${PSQL_WRAPPER}"
