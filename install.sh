#!/usr/bin/env bash
# Installs the SyncLight driver as a macOS Login Item (LaunchAgent).
# Run once: ./install.sh
# To uninstall: ./install.sh --uninstall

set -euo pipefail

LABEL="com.robobloq.synclight"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$SCRIPT_DIR/synclight.py"
PYTHON="$( (command -v /opt/homebrew/bin/python3.11 || command -v python3) 2>/dev/null | head -1 )"
LOG="$HOME/Library/Logs/SyncLight.log"
BIN_DIR="$HOME/.local/bin"

# ── uninstall ──────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--uninstall" ]]; then
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    rm -f "$BIN_DIR/sl" "$BIN_DIR/sl-gui"
    echo "SyncLight driver uninstalled."
    exit 0
fi

# ── install dependency ─────────────────────────────────────────────────────────
echo "Installing Python dependencies..."
"$PYTHON" -m pip install --quiet --user \
    hid \
    mss \
    'pyobjc-core==10.3.2' \
    'pyobjc-framework-Cocoa==10.3.2' \
    rumps

# ── create LaunchAgent plist ───────────────────────────────────────────────────
mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$HOME/Library/Logs"

cat > "$PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$SCRIPT</string>
    </array>

    <!-- Start at login and keep alive if it crashes -->
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>$LOG</string>
    <key>StandardErrorPath</key>
    <string>$LOG</string>
</dict>
</plist>
EOF

# ── CLI + GUI helpers ──────────────────────────────────────────────────────────
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/sl" << EOF
#!/bin/bash
exec "$PYTHON" "$SCRIPT_DIR/sl.py" "\$@"
EOF
chmod +x "$BIN_DIR/sl"

cat > "$BIN_DIR/sl-gui" << EOF
#!/bin/bash
exec "$PYTHON" "$SCRIPT_DIR/gui.py" "\$@"
EOF
chmod +x "$BIN_DIR/sl-gui"

# ── load the agent ─────────────────────────────────────────────────────────────
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load -w "$PLIST"

# Ensure ~/.local/bin is on PATH for interactive shells
for rc in "$HOME/.zprofile" "$HOME/.zshrc"; do
    if [[ -f "$rc" ]] || [[ "$rc" == "$HOME/.zprofile" ]]; then
        touch "$rc"
        if ! grep -q '\.local/bin' "$rc" 2>/dev/null; then
            echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$rc"
        fi
    fi
done

echo ""
echo "SyncLight driver installed and running."
echo "  Script : $SCRIPT"
echo "  CLI    : $BIN_DIR/sl       (sl on | sl off | sl color warm)"
echo "  GUI    : $BIN_DIR/sl-gui   (menu bar app)"
echo "  Logs   : $LOG"
echo ""
echo "To stop:      launchctl unload $PLIST"
echo "To uninstall: ./install.sh --uninstall"
