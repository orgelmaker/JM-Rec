; JM-Rec Inno Setup Script
; Organ Sample Recorder - Standalone Installer

#define MyAppName "JM-Rec"
#define MyAppVersion "1.1"
#define MyAppExeName "JM-Rec.exe"

[Setup]
AppId={{B3F7A1D2-9C4E-4F8B-A6D1-3BM-JMREC-001}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
UninstallDisplayName={#MyAppName} - Organ Sample Recorder
OutputDir=..\output
OutputBaseFilename=JM-Rec-Setup
Compression=lzma2/ultra64
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
WizardStyle=modern
SetupIconFile=jm_rec_icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UsePreviousAppDir=yes
CreateUninstallRegKey=yes

[Languages]
Name: "dutch"; MessagesFile: "compiler:Languages\Dutch.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Snelkoppeling op bureaublad aanmaken"; GroupDescription: "Snelkoppelingen:"; Flags: checkedonce

[Files]
Source: "JM-Rec.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "jm_rec_icon.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autodesktop}\JM-Rec"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--port 5555"; WorkingDir: "{app}"; IconFilename: "{app}\jm_rec_icon.ico"; Comment: "JM-Rec - Organ Sample Recorder"; Tasks: desktopicon
Name: "{group}\JM-Rec"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--port 5555"; WorkingDir: "{app}"; IconFilename: "{app}\jm_rec_icon.ico"; Comment: "JM-Rec - Organ Sample Recorder"
Name: "{group}\JM-Rec verwijderen"; Filename: "{uninstallexe}"; IconFilename: "{app}\jm_rec_icon.ico"

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--port 5555"; Description: "JM-Rec nu starten"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "taskkill"; Parameters: "/IM {#MyAppExeName} /F"; Flags: runhidden; RunOnceId: "KillJMRec"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
var
  ModePageID: Integer;
  ModeRepair, ModeUninstall: TNewRadioButton;
  ModePage: TWizardPage;

function IsUpgrade(): Boolean;
begin
  Result := RegValueExists(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{{B3F7A1D2-9C4E-4F8B-A6D1-3BM-JMREC-001}_is1', 'UninstallString')
         or RegValueExists(HKLM, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{{B3F7A1D2-9C4E-4F8B-A6D1-3BM-JMREC-001}_is1', 'UninstallString');
end;

procedure InitializeWizard;
var
  LblInfo: TNewStaticText;
begin
  if IsUpgrade then
  begin
    ModePage := CreateCustomPage(wpWelcome, 'JM-Rec is al geïnstalleerd', 'Kies wat je wilt doen:');
    ModePageID := ModePage.ID;

    ModeRepair := TNewRadioButton.Create(ModePage);
    ModeRepair.Parent := ModePage.Surface;
    ModeRepair.Top := 20;
    ModeRepair.Left := 0;
    ModeRepair.Width := ModePage.SurfaceWidth;
    ModeRepair.Caption := 'Repareren — bestanden opnieuw installeren en snelkoppelingen herstellen';
    ModeRepair.Checked := True;
    ModeRepair.Font.Style := [fsBold];

    ModeUninstall := TNewRadioButton.Create(ModePage);
    ModeUninstall.Parent := ModePage.Surface;
    ModeUninstall.Top := 60;
    ModeUninstall.Left := 0;
    ModeUninstall.Width := ModePage.SurfaceWidth;
    ModeUninstall.Caption := 'Verwijderen — JM-Rec volledig van deze computer verwijderen';
    ModeUninstall.Font.Style := [fsBold];

    LblInfo := TNewStaticText.Create(ModePage);
    LblInfo.Parent := ModePage.Surface;
    LblInfo.Top := 110;
    LblInfo.Left := 0;
    LblInfo.Width := ModePage.SurfaceWidth;
    LblInfo.WordWrap := True;
    LblInfo.Caption := 'Bij repareren worden alle programmabestanden opnieuw geïnstalleerd en wordt de snelkoppeling op het bureaublad hersteld. Je opnames en projectinstellingen blijven behouden.';
  end else
    ModePageID := -1;
end;

function GetUninstallString(): String;
var
  sUnInstPath: String;
  sUnInstStr: String;
begin
  Result := '';
  sUnInstPath := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{{B3F7A1D2-9C4E-4F8B-A6D1-3BM-JMREC-001}_is1';
  if RegQueryStringValue(HKCU, sUnInstPath, 'UninstallString', sUnInstStr) then
    Result := sUnInstStr
  else
    RegQueryStringValue(HKLM, sUnInstPath, 'UninstallString', sUnInstStr);
  Result := sUnInstStr;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  UninstStr: String;
  ResultCode: Integer;
begin
  Result := True;
  if (ModePageID <> -1) and (CurPageID = ModePageID) then
  begin
    if ModeUninstall.Checked then
    begin
      UninstStr := GetUninstallString();
      if UninstStr <> '' then
      begin
        UninstStr := RemoveQuotes(UninstStr);
        Exec(UninstStr, '/SILENT', '', SW_SHOW, ewWaitUntilTerminated, ResultCode);
      end;
      WizardForm.Close;
      Result := False;
    end;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  Exec('taskkill', '/IM {#MyAppExeName} /F', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(500);
end;
