#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Setup-Script für dmarc-ingest Virtual Environment

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/venv"
REQUIREMENTS_FILE="${SCRIPT_DIR}/requirements.txt"

echo "=== [dmarc-ingest] Richte Python Virtual Environment ein ==="

# Prüfen, ob python3 verfügbar ist
if ! command -v python3 >/dev/null 2>&1; then
    echo "FEHLER: python3 ist nicht installiert oder nicht im PATH auffindbar." >&2
    exit 1
fi

# Virtuelle Umgebung erstellen, falls nicht vorhanden
if [ ! -d "${VENV_DIR}" ]; then
    echo "--> Erstelle venv unter: ${VENV_DIR}"
    python3 -m venv "${VENV_DIR}"
else
    echo "--> Vorhandenes venv gefunden unter: ${VENV_DIR}"
fi

# Pip aktualisieren
echo "--> Aktualisiere pip..."
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip

# Abhängigkeiten installieren
if [ -f "${REQUIREMENTS_FILE}" ]; then
    echo "--> Installiere Abhängigkeiten aus requirements.txt..."
    "${VENV_DIR}/bin/pip" install --quiet -r "${REQUIREMENTS_FILE}"
else
    echo "HINWEIS: requirements.txt nicht gefunden. Installiere defusedxml direkt..."
    "${VENV_DIR}/bin/pip" install --quiet defusedxml
fi

# Verifikation
echo "--> Verifiziere Installation..."
if "${VENV_DIR}/bin/python3" -c "import defusedxml" >/dev/null 2>&1; then
    echo "=== [dmarc-ingest] Setup erfolgreich abgeschlossen! ==="
    echo "Python Interpreter: ${VENV_DIR}/bin/python3"
    "${VENV_DIR}/bin/python3" --version
else
    echo "FEHLER: Verifikation fehlgeschlagen (defusedxml konnte nicht importiert werden)." >&2
    exit 1
fi
