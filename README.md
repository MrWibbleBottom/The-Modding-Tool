# The Modding Tool

## Overview

**The Modding Tool** is a portable, local Web UI built with FastAPI and a single-page HTML interface for:
- **Platform- & Engine-Agnostic Game Discovery**: Automatically scans installed games from Steam, Epic Games, GOG Galaxy, and custom folders, and fingerprints engines (`Unity IL2CPP`, `Unity Mono`, `Unreal Engine 4/5`, `Creation Engine`, `Godot`, `Data-Driven`).
- **Non-Destructive Modding & Backups**: Inspects installed mods (`~mods`, `UE4SS`, `BepInEx`, `MelonLoader`, `ReShade`, `OptiScaler`), tracks clean-restore manifests, and creates incremental snapshots before changes.
- **MCP-Driven Runtime Modding**: Connects to live MCP bridges such as `bepinex-mcp` for runtime inspection and safe value/component edits.

---

## Requirements

- **Windows 11** (or Windows 10 64-bit)
- **Python 3.10 or newer** — during installation, make sure **"Add Python to PATH"** is checked.

---

## Quick Start

### Option A: One-Click Launcher (Recommended)
1. Download the latest release `.zip` from GitHub Releases and extract it to a folder (for example, `C:\Tools\TheModdingTool`).
2. Double-click **`start_app.bat`**.
3. On first run, `start_app.bat` creates a local `.venv`, installs required Python packages, opens `http://127.0.0.1:8765` in your browser, and starts the server.

### Option B: Manual Terminal Setup
```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r poc-ui\requirements.txt
.\.venv\Scripts\python.exe -m uvicorn controller:app --app-dir poc-ui --host 127.0.0.1 --port 8765 --reload
```
Then open **http://127.0.0.1:8765** in your browser.

---

## Connecting an MCP Bridge (e.g., Unity / BepInEx)

For live Unity IL2CPP runtime inspection and editing:
1. Open the **Active Game** tab and select a Unity game.
2. In the **Live MCP Inspection & Mutation** section:
   - Set **Python Executable** to your `.venv\Scripts\python.exe` (for example `.\.venv\Scripts\python.exe`).
   - Set **MCP Server Script** to `bepinex-mcp\mcp\ModdersHelperApp.py`.
3. Provision BepInEx + the bridge plugin from **Provision & Clean Restore**, launch the game once, and click **1. Test Bridge** to verify the live connection.

---

## Backups, Logs & Updates

- **Safe Local Storage**: All user configuration, game caches, manifests, app snapshots, and audit logs are stored safely in `%LOCALAPPDATA%\ModToolPoC\` — outside your game folders and outside the app directory.
- **Pulling Updates**: Open the **App Management** tab, enter the repository Owner/Name/Branch under **GitHub Distribution & Restore**, click **Save Repo Settings**, then use **Check GitHub** and **Pull / Restore Latest from GitHub** (a pre-sync snapshot is saved automatically before updating, and you can roll back anytime from the **Local Snapshots** dropdown).

---

## Safety Notice

This tool is strictly intended for **offline and single-player modding**. It automatically scans game directories for kernel and online anti-cheat markers (such as `EasyAntiCheat`, `BattlEye`, `Vanguard`, `GameGuard`, and `nProtect`) and marks protected titles as **Restricted / Blocked** so they are never modified.
