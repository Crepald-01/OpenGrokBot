; Inno Setup script for OpenGrokBot. Build with:  python build_installer.py
; (or compile directly:  ISCC.exe /DAppVersion=1.6.0 installer\OpenGrokBot.iss)

#ifndef AppVersion
  #define AppVersion "1.6.0"
#endif
#ifndef DistDir
  #define DistDir "..\dist\OpenGrokBot"
#endif

#define AppName "OpenGrokBot"
#define AppExe "OpenGrokBot.exe"

[Setup]
; Keep this GUID constant so new versions upgrade the old install in place.
AppId={{6B1D6A52-3F0B-4B6E-9E8A-0C7E2D5F41A9}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=OpenGrokBot contributors
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
OutputDir=..\dist
OutputBaseFilename=OpenGrokBot-Setup-{#AppVersion}
; Per-user by default (no admin prompt); the first wizard page lets the user choose all-users.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Start OpenGrokBot in the system tray when I sign in to Windows"; GroupDescription: "Startup:"; Flags: unchecked
Name: "chromium"; Description: "Download the Chromium browser engine now (about 150 MB, needed for Bots to use a browser)"; GroupDescription: "Browser engine:"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#AppName}"; ValueData: """{app}\{#AppExe}"" --tray"; Tasks: startup; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Parameters: "--install-browsers"; StatusMsg: "Downloading the Chromium browser engine (about 150 MB), please wait..."; Tasks: chromium; Flags: waituntilterminated
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[Code]
// The app and its background service are the same exe, so stop both before files are replaced or removed.
// Unfinished Bot tasks are resumed by the service the next time it starts.
procedure StopApp;
var
  rc: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, rc);
  Sleep(800);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopApp;
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  StopApp;
  Result := True;
end;

function HasParam(const Param: String): Boolean;
var
  i: Integer;
begin
  Result := False;
  for i := 1 to ParamCount do
    if CompareText(ParamStr(i), Param) = 0 then Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data: String;
  Delete: Boolean;
begin
  if CurUninstallStep <> usPostUninstall then Exit;
  Data := ExpandConstant('{userappdata}\OpenGrokBot');
  if not DirExists(Data) then Exit;
  if UninstallSilent then
    Delete := HasParam('/DELETEDATA')
  else
    Delete := MsgBox('Also delete your Bots, conversations, memory, skills and settings?' + #13#10 + #13#10 + Data + #13#10 + #13#10 +
                     'Choose No to keep them for a future install. (API keys saved in Windows Credential Manager are not removed.)',
                     mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
  if Delete then DelTree(Data, True, True, True);
end;
