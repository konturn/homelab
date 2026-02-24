#!/bin/sh
# Fetch USDC balance on Base for the ClawRouter wallet
# Output: InfluxDB line protocol
#
# Requires CLAWROUTER_WALLET_ADDRESS env var (set via Vault/Ansible)

WALLET="${CLAWROUTER_WALLET_ADDRESS:-}"
[ -z "$WALLET" ] && exit 0
USDC_CONTRACT="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
RPC_URL="https://mainnet.base.org"

# balanceOf(address) selector = 0x70a08231, padded address
PADDED_ADDR="000000000000000000000000$(echo "$WALLET" | sed 's/0x//')"
DATA="0x70a08231${PADDED_ADDR}"

RESULT=$(curl -sf -X POST "$RPC_URL" \
  -H "Content-Type: application/json" \
  -d "{\"jsonrpc\":\"2.0\",\"method\":\"eth_call\",\"params\":[{\"to\":\"${USDC_CONTRACT}\",\"data\":\"${DATA}\"},\"latest\"],\"id\":1}" \
  | grep -o '"result":"[^"]*"' | sed 's/"result":"//;s/"//')

if [ -z "$RESULT" ] || [ "$RESULT" = "0x" ]; then
  echo "clawrouter_balance balance=0 $(date +%s)000000000"
  exit 0
fi

# Convert hex to decimal, then divide by 1e6 (USDC has 6 decimals)
HEX=$(echo "$RESULT" | sed 's/0x//')
DECIMAL=$(printf '%d' "0x${HEX}" 2>/dev/null || echo 0)
INTEGER=$((DECIMAL / 1000000))
FRACTION=$((DECIMAL % 1000000))

printf "clawrouter_balance balance=%d.%06d %s000000000\n" "$INTEGER" "$FRACTION" "$(date +%s)"
