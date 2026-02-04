#!/bin/bash
# Detect ATS type from application URL
# Usage: ./detect-ats.sh <url>
# Outputs: ashby|greenhouse|lever|workday|workable|unknown

URL="$1"

if [ -z "$URL" ]; then
    echo "usage: detect-ats.sh <url>" >&2
    exit 1
fi

if echo "$URL" | grep -qiE "ashbyhq\.com|jobs\.ashby"; then
    echo "ashby"
elif echo "$URL" | grep -qiE "greenhouse\.io|boards\.greenhouse"; then
    echo "greenhouse"
elif echo "$URL" | grep -qiE "lever\.co|jobs\.lever"; then
    echo "lever"
elif echo "$URL" | grep -qiE "myworkdayjobs\.com|workday\.com"; then
    echo "workday"
elif echo "$URL" | grep -qiE "workable\.com|apply\.workable"; then
    echo "workable"
else
    echo "unknown"
fi
