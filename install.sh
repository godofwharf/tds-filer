#!/usr/bin/env bash
# tds-filer installer: dependencies (uv, Node.js, Java check) + the tds-filing agent skill.
#
#   curl -fsSL https://raw.githubusercontent.com/godofwharf/tds-filer/master/install.sh | bash
#
# Environment overrides:
#   TDS_SKILL_SCOPE=global|project   where to install the skill (default: global)
#   TDS_AGENTS="claude-code opencode" agents to install the skill for
#   TDS_REF=<branch|tag|sha>          git ref of tds-filer to use (default: master)
#   DO_NOT_TRACK=1                    disable telemetry of the `skills` CLI
set -euo pipefail

REPO="godofwharf/tds-filer"
REF="${TDS_REF:-master}"
SCOPE="${TDS_SKILL_SCOPE:-global}"
AGENTS="${TDS_AGENTS:-claude-code opencode}"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

[ "$(uname -s)" = "Darwin" ] || die "tds-filer needs macOS (it reads challan PDFs with PDFKit)."
case "$SCOPE" in global|project) ;; *) die "TDS_SKILL_SCOPE must be global or project" ;; esac

say "Checking dependencies"
if have uv; then
  ok "uv $(uv --version | awk '{print $2}')"
else
  if have brew; then brew install uv; else curl -LsSf https://astral.sh/uv/install.sh | bash; fi
  PATH="$HOME/.local/bin:$PATH"; export PATH
  have uv || die "uv installation failed; see https://docs.astral.sh/uv/"
  ok "installed uv"
fi

if have npx; then
  ok "node $(node --version)"
else
  have brew || die "Node.js (npx) is required; install it from https://nodejs.org or via Homebrew."
  brew install node
  ok "installed node"
fi

if java -version >/dev/null 2>&1; then
  ok "java $(java -version 2>&1 | awk -F'"' 'NR==1{print $2}')"
else
  warn "Java not found. Protean's FVU needs it: brew install --cask temurin"
fi

say "Checking access to $REPO"
git ls-remote "https://github.com/$REPO" HEAD >/dev/null 2>&1 \
  || die "cannot reach https://github.com/$REPO (network/proxy?)"
ok "repository reachable"

say "Installing the tds CLI (cached by uvx)"
uvx --refresh --from "git+https://github.com/$REPO@$REF" tds --help >/dev/null
ok "tds runs via: uvx --from git+https://github.com/$REPO tds ..."

say "Installing the tds-filing skill ($SCOPE) for: $AGENTS"
args=(add "$REPO" --skill tds-filing -y)
[[ "$SCOPE" == "global" ]] && args+=(-g)
for a in $AGENTS; do args+=(-a "$a"); done
npx -y skills "${args[@]}"

cat <<EOF

Done. Next steps:
  1. Download Protean's FVU (TDS_STANDALONE_FVU_<ver>.zip) and the Form 144 format xlsx from
     tinpan.proteantech.in > Downloads > e-TDS > Regular; unzip, then: chmod -R go-w <folder>
  2. Create and fill in your config (stays on this machine, mode 0600):
       uvx --from git+https://github.com/$REPO tds init
  3. Ask your agent: "prepare my TDS filing for <tax year> <quarter>"
EOF
