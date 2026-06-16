#!/usr/bin/env bash
# deploy.sh — one-click build and deploy for the home-network-monitor stack.
#
# Usage:
#   ./deploy.sh           — sync + build + deploy to Pi, then verify
#   ./deploy.sh --local   — build + deploy locally (Mac)
#   ./deploy.sh --check   — show status of Pi containers without deploying
#   ./deploy.sh --help    — show this message

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
PI_HOST="${PI_HOST:-pi@raspberrypi.local}"
PI_DIR="${PI_DIR:-/home/pi/home-network-monitor}"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

# Files synced to Pi (override file and .env are intentionally excluded)
SYNC_DIRS=(monitor lanscan prometheus grafana settings)
SYNC_FILES=(docker-compose.yml)

# Colours
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

log()  { echo -e "${CYAN}▶${NC} $*"; }
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC} $*"; }
fail() { echo -e "${RED}✗${NC} $*"; exit 1; }
header() { echo -e "\n${BOLD}$*${NC}"; }

# ── Helpers ───────────────────────────────────────────────────────────────────
usage() {
  grep '^#' "$0" | head -10 | sed 's/^# \?//'
  exit 0
}

wait_healthy() {
  local host="$1" timeout=120 interval=5 elapsed=0
  log "Waiting for all containers to become healthy (up to ${timeout}s)..."
  while true; do
    local status
    if [[ "$host" == "local" ]]; then
      status=$(docker compose -f "$LOCAL_DIR/docker-compose.yml" \
                              -f "$LOCAL_DIR/docker-compose.override.yml" \
               ps --format json 2>/dev/null | \
               python3 -c "
import sys,json
lines=[l for l in sys.stdin.read().splitlines() if l.strip()]
containers=[json.loads(l) for l in lines]
total=len(containers)
healthy=sum(1 for c in containers if 'healthy' in c.get('Health','').lower() or c.get('State','')=='running')
unhealthy=[c['Name'] for c in containers if 'unhealthy' in c.get('Health','').lower()]
print(f'{healthy}/{total}', ','.join(unhealthy) if unhealthy else '')
" 2>/dev/null || echo "0/0 ")
    else
      status=$(ssh "$PI_HOST" "cd '$PI_DIR' && docker compose ps --format json 2>/dev/null | \
               python3 -c \"
import sys,json
lines=[l for l in sys.stdin.read().splitlines() if l.strip()]
containers=[json.loads(l) for l in lines]
total=len(containers)
healthy=sum(1 for c in containers if 'healthy' in c.get('Health','').lower() or c.get('State','')=='running')
unhealthy=[c['Name'] for c in containers if 'unhealthy' in c.get('Health','').lower()]
print(f'{healthy}/{total}', ','.join(unhealthy) if unhealthy else '')
\"" 2>/dev/null || echo "0/0 ")
    fi
    local counts="${status%% *}"
    local unhealthy="${status#* }"
    local n_healthy="${counts%/*}"
    local n_total="${counts#*/}"
    if [[ "$n_healthy" -eq "$n_total" && "$n_total" -gt 0 ]]; then
      ok "All $n_total containers healthy"
      return 0
    fi
    [[ -n "$unhealthy" ]] && warn "Unhealthy: $unhealthy"
    if [[ $elapsed -ge $timeout ]]; then
      fail "Timed out waiting for healthy containers"
    fi
    sleep $interval
    elapsed=$((elapsed + interval))
    echo -ne "\r  ${YELLOW}${n_healthy}/${n_total} healthy — ${elapsed}s elapsed...${NC}    "
  done
}

show_status() {
  local target="$1"
  header "Container status"
  if [[ "$target" == "local" ]]; then
    docker compose -f "$LOCAL_DIR/docker-compose.yml" \
                   -f "$LOCAL_DIR/docker-compose.override.yml" \
      ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null
    echo ""
    log "Grafana: http://localhost:3000"
    log "Prometheus: http://localhost:9090"
    log "Metrics: http://localhost:8000/metrics"
  else
    ssh "$PI_HOST" "cd '$PI_DIR' && docker compose ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}'" 2>/dev/null
    echo ""
    local host; host=$(echo "$PI_HOST" | cut -d@ -f2)
    log "Grafana: http://${host}:3000"
    log "Prometheus: http://${host}:9090"
    log "Metrics: http://${host}:8000/metrics"
  fi
}

verify_metrics() {
  local target="$1"   # "pi" or "local"
  header "Verifying metrics"
  local ok_count=0

  _metrics() {
    if [[ "$target" == "pi" ]]; then
      ssh "$PI_HOST" "curl -sf --max-time 5 http://localhost:8000/metrics" 2>/dev/null
    else
      curl -sf --max-time 5 http://localhost:8000/metrics 2>/dev/null
    fi
  }

  _prom_query() {
    local q="$1"
    if [[ "$target" == "pi" ]]; then
      ssh "$PI_HOST" "curl -sf --max-time 5 'http://localhost:9090/api/v1/query?query=$q'" 2>/dev/null
    else
      curl -sf --max-time 5 "http://localhost:9090/api/v1/query?query=$q" 2>/dev/null
    fi
  }

  local metrics; metrics=$(_metrics)

  for check in \
    "internet_probe_success|probe_success metric" \
    "internet_target_info|target_info metric" \
    "internet_ping_rtt_avg_ms|RTT metric"
  do
    local pattern="${check%%|*}"; local label="${check##*|}"
    if echo "$metrics" | grep -q "$pattern"; then
      ok "$label"
      ((ok_count++))
    else
      warn "$label not found"
    fi
  done

  local rule_errors
  rule_errors=$(_prom_query "count(internet%3Aping_rtt%3Amean6h)" | \
    python3 -c "
import sys,json
try:
  d=json.load(sys.stdin)
  print('ok' if d.get('status')=='success' else 'error')
except: print('unreachable')" 2>/dev/null || echo "unreachable")
  if [[ "$rule_errors" == "ok" ]]; then
    ok "Prometheus recording rules producing data"
    ((ok_count++))
  else
    warn "Prometheus rules not returning data yet ($rule_errors)"
  fi

  echo ""
  if [[ $ok_count -eq 4 ]]; then
    ok "All 4/4 checks passed — stack is healthy"
  else
    warn "$ok_count/4 checks passed"
  fi
}

# ── Deploy to Pi ──────────────────────────────────────────────────────────────
deploy_pi() {
  header "Deploying to Pi ($PI_HOST)"

  # 1. Reachability
  log "Checking Pi is reachable..."
  ssh -o ConnectTimeout=5 -o BatchMode=yes "$PI_HOST" true 2>/dev/null \
    || fail "Cannot reach $PI_HOST — check SSH and that Pi is online"
  ok "Pi reachable"

  # 2. Sync files
  header "Syncing files"
  for dir in "${SYNC_DIRS[@]}"; do
    local src="$LOCAL_DIR/$dir/"
    local dst="$PI_HOST:$PI_DIR/$dir/"
    log "  $dir/"
    rsync -az --delete \
      --exclude='__pycache__' --exclude='*.pyc' --exclude='.DS_Store' \
      "$src" "$dst"
  done
  for f in "${SYNC_FILES[@]}"; do
    log "  $f"
    scp -q "$LOCAL_DIR/$f" "$PI_HOST:$PI_DIR/$f"
  done
  ok "Files synced"

  # 3. Build + deploy
  header "Building and starting containers"
  ssh "$PI_HOST" "cd '$PI_DIR' && docker compose build --pull 2>&1 | grep -E 'Step|DONE|error|ERROR|Built|failed' || true"
  ssh "$PI_HOST" "cd '$PI_DIR' && docker compose up -d --force-recreate 2>&1"
  ok "Containers started"

  # 4. Wait for healthy
  wait_healthy "pi"

  # 5. Show status + verify
  show_status "pi"
  verify_metrics "pi"
}

# ── Deploy locally (Mac) ──────────────────────────────────────────────────────
deploy_local() {
  header "Building and starting containers locally"
  cd "$LOCAL_DIR"
  docker compose -f docker-compose.yml -f docker-compose.override.yml \
    build --pull 2>&1 | grep -E 'Step|DONE|error|ERROR|Built|failed' || true
  docker compose -f docker-compose.yml -f docker-compose.override.yml \
    up -d --force-recreate 2>&1
  ok "Containers started"
  wait_healthy "local"
  show_status "local"
  verify_metrics "local"
}

# ── Check only ────────────────────────────────────────────────────────────────
check_pi() {
  log "Checking Pi is reachable..."
  ssh -o ConnectTimeout=5 -o BatchMode=yes "$PI_HOST" true 2>/dev/null \
    || fail "Cannot reach $PI_HOST"
  ok "Pi reachable"
  show_status "pi"
  verify_metrics "pi"
}

# ── Entry point ───────────────────────────────────────────────────────────────
case "${1:-}" in
  --help|-h) usage ;;
  --local)   deploy_local ;;
  --check)   check_pi ;;
  "")        deploy_pi ;;
  *) fail "Unknown option: $1  (try --help)" ;;
esac
