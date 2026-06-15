#!/bin/bash
# --- KONFIGURATION ---
EMPFAENGER="madone85@googlemail.com"  # <- Hier Ihre echte E-Mail-Adresse

SERVICE_NAME=$1
LOG_EXTRACT=$(journalctl -u "$SERVICE_NAME" -n 20 --no-pager)

# Betreff und Inhalt sauber aufbereiten
BETREFF="❌ FEHLER: Systemd Service [$SERVICE_NAME] fehlgeschlagen"
INHALT="Hallo,

der Systemd-Service '$SERVICE_NAME' ist auf dem OMV-Server fehlgeschlagen.

Hier sind die letzten 20 Log-Zeilen zur Fehlersuche:
-----------------------------------------------------------------
$LOG_EXTRACT
-----------------------------------------------------------------

Bitte den Server prüfen."

printf "Subject: %s\r\nTo: %s\r\n\r\n%s" "$BETREFF" "$EMPFAENGER" "$INHALT" | /usr/sbin/sendmail -t




