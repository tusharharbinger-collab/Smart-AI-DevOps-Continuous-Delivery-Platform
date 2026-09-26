#!/usr/bin/env bash
# Usage: ./load_generator.sh [live-url] [duration-seconds]
URL="${1:-http://smartcd-platform-alb-1806553190.us-east-1.elb.amazonaws.com/api/v1/cicd-test/api/checkout}"
URL="${URL%/}"
[[ "$URL" != */api/checkout ]] && URL="$URL/api/checkout"
DURATION="${2:-180}"
END=$((SECONDS + DURATION))

echo "[*] Starting load generation against: $URL"
echo "[*] Duration: ${DURATION}s"

COUNT=0
while [ $SECONDS -lt $END ]; do
  curl -s -o /dev/null -X POST "$URL" &
  COUNT=$((COUNT + 1))
  if [ $((COUNT % 10)) -eq 0 ]; then
    echo "[*] Sent $COUNT requests..."
  fi
  sleep 0.2
done
wait
echo "[+] Load generation complete. Total requests: $COUNT"
