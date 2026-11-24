; ItemFinder installer (Inno Setup 6)
; Build:  ISCC.exe /DMyAppVersion=1.2.3 installer\ItemFinder.iss
; Per-user install (no admin prompt) so updates can install silently.

#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#define MyAppName      "ItemFinder"
#define MyAppPublisher "Temple & Pixel"
#define MyAppExe       "ItemFinder.exe"
#define MyAppMutex     "ItemFinder_AppMutex"

[Setup]
; Never change AppId - Windows uses it to recognise upgrades.
AppId={{8F2A6C1E-5B7D-4E3A-9C21-7A4D2E6B9F13}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoVersion={#MyAppVersion}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=Output
OutputBaseFilename={#MyAppName}-{#MyAppVersion}-Setup
SetupIconFile=..\assets\itemfinder.ico
UninstallDisplayIcon={app}\{#MyAppExe}
UninstallDisplayName={#MyAppName}
Compression=lzma2/max
SolidCompression=yes
LZMANumBlockThreads=4
WizardStyle=modern
CloseApplications=no
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; Remove the previous version's program files so old libraries don't linger.
; (Your photo index and AI model live in %LOCALAPPDATA%\ItemFinder and are kept.)
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\ItemFinder\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon

[Run]
; Normal install: offer to launch at the end.
Filename: "{app}\{#MyAppExe}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
; Silent auto-update: reopen the app automatically.
Filename: "{app}\{#MyAppExe}"; Flags: nowait; Check: WizardSilent

[Code]
// Wait for ItemFinder to close before replacing its files.
function InitializeSetup(): Boolean;
var
  Tries: Integer;
begin
  Result := True;
  Tries := 0;
  while CheckForMutexes('{#MyAppMutex}') do
  begin
    if WizardSilent then
    begin
      if Tries >= 120 then
      begin
        Result := False;  // gave up after ~60 s
        Exit;
      end;
      Sleep(500);
      Tries := Tries + 1;
    end
    else
    begin
      if MsgBox('ItemFinder is currently running.' #13#10 'Please close it, then click OK to continue.',
                mbInformation, MB_OKCANCEL) = IDCANCEL then
      begin
        Result := False;
        Exit;
      end;
    end;
  end;
end;

function InitializeUninstall(): Boolean;
begin
  Result := True;
  while CheckForMutexes('{#MyAppMutex}') do
  begin
    if UninstallSilent or
       (MsgBox('Please close ItemFinder, then click OK to continue.', mbInformation, MB_OKCANCEL) = IDCANCEL) then
    begin
      Result := False;
      Exit;
    end;
  end;
end;

// Optionally remove the photo index + AI model when uninstalling.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and (not UninstallSilent) then
    if MsgBox('Also delete ItemFinder''s photo index and downloaded AI model?' #13#10 #13#10
              'Your photos are NOT affected either way.' #13#10
              'Choose No if you plan to reinstall later (saves re-indexing).',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(ExpandConstant('{localappdata}\ItemFinder'), True, True, True);
end;
