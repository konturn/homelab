#!/usr/bin/env python3
"""Fetch verification codes from Gmail for job applications.

Usage:
    python3 fetch-email-code.py [--sender SENDER] [--subject SUBJECT] [--wait SECONDS]

Searches recent emails for verification/security codes.
Uses GMAIL_EMAIL and GMAIL_APP_PASSWORD env vars.

Examples:
    # Fetch most recent Greenhouse security code
    python3 fetch-email-code.py --sender greenhouse --subject "security code"
    
    # Wait up to 120s for a code to arrive
    python3 fetch-email-code.py --sender greenhouse --wait 120
"""

import imaplib
import email
import os
import re
import sys
import time
import argparse
from email.header import decode_header
from datetime import datetime, timedelta


def get_credentials():
    addr = os.environ.get('GMAIL_EMAIL', 'konoahko@gmail.com')
    password = os.environ.get('GMAIL_APP_PASSWORD')
    if not password:
        print("ERROR: GMAIL_APP_PASSWORD not set", file=sys.stderr)
        sys.exit(1)
    return addr, password


def decode_subject(msg):
    subject = msg.get('Subject', '')
    decoded = decode_header(subject)
    parts = []
    for part, charset in decoded:
        if isinstance(part, bytes):
            parts.append(part.decode(charset or 'utf-8', errors='replace'))
        else:
            parts.append(part)
    return ' '.join(parts)


def extract_code(body):
    """Try to extract a verification code from email body."""
    # Strip HTML tags for easier matching
    text = re.sub('<[^>]+>', ' ', body)
    text = re.sub(r'\s+', ' ', text)
    
    patterns = [
        # Greenhouse: "paste this code into the security code field ... : CODE"
        r'(?:code\s+(?:field|input)[^:]*:\s*)([a-zA-Z0-9]{6,10})',
        # "your (?:security|verification) code is: CODE"
        r'(?:your\s+)?(?:security|verification)\s+code\s+is[:\s]+([a-zA-Z0-9]{6,10})',
        # "application: CODE" (Greenhouse specific)
        r'application[:\s]+([a-zA-Z0-9]{6,10})',
        # Generic: "code: CODE" but not "security code" itself
        r'(?<!security\s)code[:\s]+([a-zA-Z0-9]{6,10})\b',
        # Bold/strong wrapped code
        r'<strong>([a-zA-Z0-9]{6,10})</strong>',
        # Standalone 8-char alphanumeric on its own line or surrounded by whitespace
        r'(?:^|\n)\s*([a-zA-Z0-9]{8})\s*(?:\n|$)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if match:
            code = match.group(1)
            # Sanity check: don't return common words
            if code.lower() not in ('security', 'verified', 'complete', 'continue', 'password', 'application'):
                return code
    return None


def get_email_body(msg):
    """Extract text body from email message."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == 'text/plain':
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode('utf-8', errors='replace')
            elif ct == 'text/html':
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode('utf-8', errors='replace')
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode('utf-8', errors='replace')
    return ''


def search_emails(sender_filter=None, subject_filter=None, minutes_back=30):
    """Search recent emails and return matching ones."""
    addr, password = get_credentials()
    
    mail = imaplib.IMAP4_SSL('imap.gmail.com')
    mail.login(addr, password)
    mail.select('inbox')
    
    # Search criteria
    since = (datetime.now() - timedelta(minutes=minutes_back)).strftime('%d-%b-%Y')
    criteria = f'(SINCE "{since}")'
    
    if sender_filter:
        criteria = f'(SINCE "{since}" FROM "{sender_filter}")'
    
    status, messages = mail.search(None, criteria)
    if status != 'OK' or not messages[0]:
        mail.logout()
        return []
    
    results = []
    ids = messages[0].split()
    
    # Check most recent first
    for msg_id in reversed(ids[-20:]):
        status, msg_data = mail.fetch(msg_id, '(RFC822)')
        if status != 'OK':
            continue
        
        msg = email.message_from_bytes(msg_data[0][1])
        subject = decode_subject(msg)
        
        # Apply subject filter
        if subject_filter and subject_filter.lower() not in subject.lower():
            continue
        
        body = get_email_body(msg)
        code = extract_code(body)
        
        results.append({
            'subject': subject,
            'from': msg.get('From', ''),
            'date': msg.get('Date', ''),
            'code': code,
            'body_preview': body[:500] if body else ''
        })
    
    mail.logout()
    return results


def main():
    parser = argparse.ArgumentParser(description='Fetch email verification codes')
    parser.add_argument('--sender', default='greenhouse', help='Filter by sender (default: greenhouse)')
    parser.add_argument('--subject', default='security code', help='Filter by subject (default: security code)')
    parser.add_argument('--wait', type=int, default=0, help='Wait up to N seconds for code to arrive')
    parser.add_argument('--minutes', type=int, default=30, help='Search emails from last N minutes')
    args = parser.parse_args()
    
    deadline = time.time() + args.wait if args.wait > 0 else time.time()
    attempt = 0
    
    while True:
        attempt += 1
        results = search_emails(
            sender_filter=args.sender,
            subject_filter=args.subject,
            minutes_back=args.minutes
        )
        
        for r in results:
            if r['code']:
                print(f"CODE: {r['code']}")
                print(f"FROM: {r['from']}")
                print(f"SUBJECT: {r['subject']}")
                print(f"DATE: {r['date']}")
                sys.exit(0)
        
        if time.time() >= deadline:
            break
        
        # Exponential backoff: 5s, 10s, 20s, capped at 30s
        wait = min(5 * (2 ** (attempt - 1)), 30)
        print(f"No code found yet (attempt {attempt}), waiting {wait}s...", file=sys.stderr)
        time.sleep(wait)
    
    # No code found
    if results:
        print("Found emails but couldn't extract code:", file=sys.stderr)
        for r in results[:3]:
            print(f"  - {r['subject']}", file=sys.stderr)
            print(f"    Preview: {r['body_preview'][:200]}", file=sys.stderr)
    else:
        print("No matching emails found", file=sys.stderr)
    
    sys.exit(1)


if __name__ == '__main__':
    main()
