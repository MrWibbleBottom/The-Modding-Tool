# The Modding Tool Project Backup & Restore Guide

- **Backup script**: `backup_manager.py`
- **Backup storage**: `./backups/`
- **Automatic watcher**: `.\.venv\Scripts\python.exe backup_manager.py --watch`

## Commands

- List all backup versions:
  ```powershell
  .\.venv\Scripts\python.exe backup_manager.py --list
  ```
- Create a manual incremental snapshot:
  ```powershell
  .\.venv\Scripts\python.exe backup_manager.py --snapshot --reason "Describe change"
  ```
- Restore a specific version (e.g. `v001`) in-place or to a target directory:
  ```powershell
  .\.venv\Scripts\python.exe backup_manager.py --restore v001
  ```
  Or run `restore.ps1` inside any backup folder.
