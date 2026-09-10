# MyTools — Right-Click Context Menu Framework

A dynamically extensible cascading menu for the Windows folder right-click context menu.

## Quick Start

```powershell
# Install framework and all bundled tools
.\install-mytools.ps1

# Right-click any folder -> My Tools -> Unlock Folder
```

## Directory Layout

```
win\
├── install-mytools.ps1       # Register menu + auto-register all tools
├── uninstall-mytools.ps1     # Remove menu ([-RemoveFiles] to delete scripts)
├── register-tool.ps1         # Add a tool to the menu
├── unregister-tool.ps1       # Remove a tool from the menu
├── bin\                      # Third-party binaries (handle.exe, etc.)
└── tools\
    └── <ToolName>\
        ├── tool.json         # Tool manifest (required for auto-registration)
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
  "CommandTemplate": "powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"{ToolDir}\\MyScript.ps1\" -Path \"%1\"",
  "AppliesTo": "Directory"
}
```

4. Register it:

```powershell
.\register-tool.ps1 -ToolDir .\tools\MyNewTool
```

The `{ToolDir}` placeholder in `CommandTemplate` is replaced with the absolute
path of the tool directory at registration time. `%1` is the selected folder path.

### Register Without a Manifest

```powershell
.\register-tool.ps1 -Name "quicktool" -DisplayName "Quick Action" `
    -Icon "shell32.dll,0" -Command 'powershell.exe -Command "Write-Host %1"'
```

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

| Tool | Description |
|------|-------------|
| Unlock Folder | Find processes locking a folder via Sysinternals handle.exe; select and terminate them |

## Notes

- **Per-user installation**: Uses `HKCU\Software\Classes`, no admin required.
- **Windows 11**: The menu appears under "Show more options" (Shift+F10).
- **handle.exe**: Auto-downloaded from Sysinternals on first use; stored in `bin\`.
