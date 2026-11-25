; Inno Setup script for Swift Screen Reader. Built by build.bat (or: ISCC /DMyAppVersion=x.y.z installer.iss)

#define MyAppName "Swift Screen Reader"
#define MyAppExeName "SwiftScreenReader.exe"
#define MyCliExeName "SwiftScreenReaderCli.exe"
#define MyAppPublisher "db IMMIX"
#ifndef MyAppVersion
  #define MyAppVersion "0.1.0"
#endif

[Setup]
AppId={{5A565BEB-35EE-4F69-828C-16468DE30079}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppCopyright=Software built by Bikitsos for db IMMIX
VersionInfoVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} Setup
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=dist
OutputBaseFilename=SwiftScreenReader-Setup-{#MyAppVersion}
SetupIconFile=build\app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startup"; Description: "Start the capture service automatically with Windows (in the system tray)"; GroupDescription: "Startup:"

[Dirs]
; Screenshots live next to the exe; let normal users write there even under Program Files. Kept on uninstall.
Name: "{app}\screenshots"; Permissions: users-modify; Flags: uninsneveruninstall

[InstallDelete]
; Drop files from the previous version before copying the new ones.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "dist\SwiftScreenReader\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autoprograms}\{#MyAppName} - Select area"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--select"
Name: "{autoprograms}\{#MyAppName} - Capture"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--capture"
Name: "{autoprograms}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
Root: HKA; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "SwiftScreenReader"; ValueData: """{app}\{#MyAppExeName}"" --minimized"; Flags: uninsdeletevalue; Tasks: startup

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
; Silent installs come from the in-app updater: restart the app as the original (non-elevated) user.
Filename: "{app}\{#MyAppExeName}"; Parameters: "--minimized"; Flags: nowait runasoriginaluser; Check: WizardSilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
; Saved settings (capture area, folder, ...) of the user running the uninstaller. Screenshots are kept.
Type: filesandordirs; Name: "{userappdata}\{#MyAppName}"

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#MyAppExeName}"; Flags: runhidden; RunOnceId: "KillApp"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#MyCliExeName}"; Flags: runhidden; RunOnceId: "KillCli"
