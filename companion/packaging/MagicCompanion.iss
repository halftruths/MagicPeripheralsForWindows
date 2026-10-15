; Inno Setup 6 script. Compile: ISCC /DAppVersion=0.1.0 MagicCompanion.iss (build.ps1 does this).
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "Magic Companion"
#define AppExe "MagicCompanion.exe"

[Setup]
AppId={{6F0B2D5E-3C1A-4E8B-9A57-0D4C7B1E2F93}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=halftruths
AppPublisherURL=https://github.com/halftruths/MagicPeripheralsForWindows
AppSupportURL=https://github.com/halftruths/MagicPeripheralsForWindows
DefaultDirName={localappdata}\Programs\MagicCompanion
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
OutputDir=dist
OutputBaseFilename=MagicCompanion-Setup-{#AppVersion}
SetupIconFile=magic.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked
Name: "autostart"; Description: "Start Magic Companion when I sign in"

[Files]
Source: "dist\MagicCompanion\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "MagicCompanion"; ValueData: """{app}\{#AppExe}"" --background"; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[Code]
// Ask a running copy to quit gracefully (it restores Windows' touchpad scrolling settings), then force-close as a fallback.
procedure StopApp(Dir: String);
var
  Exe: String;
  Code: Integer;
begin
  Exe := AddBackslash(Dir) + '{#AppExe}';
  if FileExists(Exe) then
    Exec(Exe, '--quit', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopApp(ExpandConstant('{app}'));
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    StopApp(ExpandConstant('{app}'));
    // The tray's "Start with Windows" toggle can add this value even when the install task was unchecked.
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'MagicCompanion');
  end
  else if CurUninstallStep = usPostUninstall then
  begin
    Data := ExpandConstant('{localappdata}\MagicCompanion');
    if DirExists(Data) and not UninstallSilent then
      if MsgBox('Also delete your Magic Companion settings?' + #13#10 + #13#10 + Data + #13#10 + #13#10 +
                'The trackpad drivers are not affected.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(Data, True, True, True);
  end;
end;
