# MyTools — Right-Click Context Menu Framework

A dynamically extensible cascading menu for Windows right-click context menus. Tools declare which scenarios they appear in via `tool.json`.

## Quick Start

```powershell
# Install framework and all bundled tools
.\install-mytools.ps1

# Right-click any folder, empty space in a folder, file, drive, or desktop -> My Tools
```

## Supported Right-Click Scenarios

| Context ID | Where it appears | Path parameter | Submenu |
|---|---|---|---|
| `Folder` | Right-click a folder icon | `%V` | MyToolsMenu |
| `FolderBackground` | Right-click empty space inside a folder | `%V` | MyToolsMenu |
| `Desktop` | Right-click empty space on the desktop | `%V` | MyToolsMenu |
| `File` | Right-click any file | `%1` | MyToolsMenuFile |
| `Drive` | Right-click a drive in My Computer | `%1` | MyToolsMenuDrive |

## Directory Layout

```
win\
├── install-mytools.ps1       # Register all entry points + auto-register tools
├── uninstall-mytools.ps1     # Remove menu ([-RemoveFiles] to delete scripts)
├── register-tool.ps1         # Register a tool from tool.json
├── unregister-tool.ps1       # Remove a tool from all submenus
├── bin\                      # Third-party binaries (handle.exe, etc.)
└── tools\
    └── <ToolName>\
        ├── tool.json         # Tool manifest (required)
        └── <script>.ps1      # Tool implementation
```

## Adding a New Tool

1. Create a folder under `tools\`, e.g. `tools\MyNewTool\`
2. Place your script(s) inside
3. Create a `tool.json` manifest:

```json
{
  "Name": "mynewtool",
  "DisplayName": "Do Something",
  "Icon": "shell32.dll,5",
  "CommandTemplate": "powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"{ToolDir}\\MyScript.ps1\" -Path \"{PathParam}\"",
  "Contexts": ["Folder", "FolderBackground", "File"]
}
```

4. Register it:

```powershell
.\register-tool.ps1 -ToolDir .\tools\MyNewTool
```

### Placeholders in CommandTemplate

| Placeholder | Replaced with | When |
|---|---|---|
| `{ToolDir}` | Absolute path of the tool directory | At registration time |
| `{PathParam}` | `%V` (folder-type) or `%1` (item-type) | At registration time, per context |
| `%V` | The right-clicked folder path (or current folder for background) | At runtime by Shell |
| `%1` | The right-clicked file/drive path | At runtime by Shell |

### Contexts Field

Declare which right-click scenarios show this tool. If omitted, defaults to `["Folder", "FolderBackground", "File"]`.

A tool may declare multiple contexts. It is registered in each relevant submenu with the correct path parameter automatically.

## Removing a Tool

```powershell
.\unregister-tool.ps1 -Name mynewtool
```

## Uninstall Everything

```powershell
.\uninstall-mytools.ps1              # Remove registry only
.\uninstall-mytools.ps1 -RemoveFiles # Also delete scripts and tools
```

## Included Tools

| Tool | Contexts | Description |
|------|----------|-------------|
| Unlock | Folder, FolderBackground, File | Find processes locking a file/folder; terminate; fallback to delete-on-reboot |
| Git Sync | Folder, FolderBackground, Desktop | git add -A && commit && push |

## Notes

- **Per-user installation**: Uses `HKCU\Software\Classes`, no admin required.
- **Windows 11**: The menu appears under "Show more options" (Shift+F10).
- **Folder background**: Uses `%V` to pass the current folder path. Works in File Explorer and Desktop.
- **handle.exe**: Auto-detected in `bin\`; download from Sysinternals if missing.
