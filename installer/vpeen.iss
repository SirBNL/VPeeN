; =========================================================================
; VPeeN Windows installer (v4.9.0)
; ------------------------------------------------------------------------
; Builds the SETUP executable users asked for - a real installer, not a
; portable zip:
;
;   * per-user install (PrivilegesRequired=lowest) -> {autopf}\VPeeN
;     resolves to %LOCALAPPDATA%\Programs\VPeeN, so NO UAC prompt and NO
;     admin rights are needed to install or upgrade - the same model as
;     VS Code's user setup.  The in-app self-updater keeps working because
;     the install directory stays user-writable (no Program Files lock).
;   * Start Menu group + optional desktop icon + entry in
;     "Apps & Features" (uninstaller) via the standard HKCU key.
;   * the same AppId across versions = clean in-place upgrades; Inno
;     reuses the previous InstallLocation automatically.
;
; Build (CI does exactly this - scripts see dist/VPeeN.exe):
;   ISCC /DAppVersion=4.9.0 installer/vpeen.iss
;   -> dist/VPeeN-Setup-<version>.exe
;
; The release still ships the portable zip alongside; the self-updater
; downloads the ZIP asset (atomic exe swap) and refreshes the uninstall
; registry entry afterwards (selfupdate.refresh_installed_version).
; =========================================================================

#define AppName     "VPeeN"
#define AppExeName  "VPeeN.exe"
#define AppPublisher "SirBNL"
#define AppURL      "https://github.com/SirBNL/VPeeN"

; CI always passes the real version; local/manual builds get a visible
; placeholder so a stale "0.0.0" never ships silently.
#ifndef AppVersion
  #define AppVersion "0.0.0-local"
#endif

[Setup]
AppId={{B486FC8C-815C-4D4C-83D6-C5DFB8E90F6E}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases/latest
; per-user install: no UAC, %LOCALAPPDATA%\Programs\VPeeN, HKCU uninstall
PrivilegesRequired=lowest
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; uninstalls never touch user data (~/.vpeen keeps settings)
UninstallDisplayName={#AppName}
Uninstallable=yes
DirExistsWarning=no
; identity + polish
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
OutputDir=..\dist
OutputBaseFilename=VPeeN-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; closes a running VPeeN via the Restart Manager instead of failing on
; a locked file (manual upgrade over a live app)
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
  GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "runafter"; Description: "Run {#AppName} after installation"; \
  GroupDescription: "Other tasks:"; Flags: checkedonce unchecked

[Files]
; the PyInstaller one-file build (tun2socks.exe + wintun.dll are inside)
Source: "..\dist\VPeeN.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}\{#AppName}"; Filename: "{app}\{#AppExeName}"; \
  WorkingDir: "{app}"; Comment: "VPeeN - free VPN client"
Name: "{autoprograms}\{#AppName}\VPeeN Website"; Filename: "{#AppURL}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; \
  WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; checkbox "Run VPeeN now" at the end of the wizard (skipped in /SILENT)
Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; \
  Description: "Run {#AppName} now"; Flags: nowait postinstall skipifsilent; \
  Tasks: runafter

[UninstallDelete]
; the app writes nothing into {app} except the exe/LICENSE - this is only
; a belt-and-suspenders sweep for PyInstaller temp leftovers.
Type: files; Name: "{app}\*.log"
