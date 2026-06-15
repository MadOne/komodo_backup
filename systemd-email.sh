#!/bin/bash

# Path to the shared environment file
ENV_FILE="/etc/komodo-backup.env"

# Fallback mechanism: check if the file exists and source the email variable
if [ -f "$ENV_FILE" ]; then
    # Extract the NOTIFICATION_EMAIL line, cleaning up quotes and carriage returns
    NOTIFICATION_EMAIL=$(grep -E '^NOTIFICATION_EMAIL=' "$ENV_FILE" | cut -d'=' -f2- | tr -d '"'\')
fi

# If the variable is empty or the file is missing, default to root as a failsafe
if [ -z "$NOTIFICATION_EMAIL" ]; then
    NOTIFICATION_EMAIL="root"
fi

UNIT=$1

# Format and send the failure notification alert via sendmail/msmtp
(
  echo "To: $NOTIFICATION_EMAIL"
  echo "Subject: ❌ Systemd Task Failure: $UNIT"
  echo "Content-Type: text/plain; charset=UTF-8"
  echo ""
  echo "Automated Monitoring Alert"
  echo "========================================="
  echo "The systemd unit '$UNIT' has entered the FAILED state."
  echo "Execution Timestamp: $(date)"
  echo "========================================="
  echo ""
  echo "Recent system daemon log output lines:"
  echo "-----------------------------------------"
  /bin/systemctl status --no-pager -n 30 "$UNIT"
) | /usr/sbin/sendmail -t
